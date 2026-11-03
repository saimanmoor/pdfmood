"""PDF-сервис: полноценная работа с PDF + OCR уровня ABBYY FineReader.

Запуск (локально):
    uvicorn app.main:app --host 127.0.0.1 --port 8000

Документация API: http://127.0.0.1:8000/docs
Веб-интерфейс:   http://127.0.0.1:8000/
"""

from __future__ import annotations

import io
import json
import re
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import pdf_tools as pt

app = FastAPI(
    title="Local PDF Service",
    description=(
        "Локальный сервис для полноценной работы с PDF: "
        "OCR (Tesseract/OCRmyPDF — распознавание рус./англ. сканов на уровне "
        "ABBYY FineReader), извлечение текста и таблиц, объединение, разбиение, "
        "поворот, удаление/перестановка страниц, водяные знаки, шифрование, "
        "сжатие и конвертации."
    ),
    version="1.0.0",
)


# ---------------------------------------------------------------------------
# Помощники
# ---------------------------------------------------------------------------

async def read_upload(u: UploadFile) -> tuple[str, bytes]:
    data = await u.read()
    if not data:
        raise HTTPException(400, f"Пустой файл {u.filename!r}")
    return u.filename or "file", data


def parse_pages(spec: str | None) -> list[int] | None:
    """'1,3,5-8' -> [1,3,5,6,7,8]; None/'' -> None."""
    if not spec:
        return None
    out: list[int] = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                a, b = b, a
            out.extend(range(a, b + 1))
        elif token.isdigit():
            out.append(int(token))
        else:
            raise HTTPException(400, f"Некорректная спецификация страниц: {token!r}")
    return sorted(set(out)) or None


def pdf_response(data: bytes, filename: str) -> Response:
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
    }
    return Response(content=data, media_type="application/pdf", headers=headers)


def zip_response(data: bytes, filename: str) -> Response:
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
    }
    return Response(content=data, media_type="application/zip", headers=headers)


def require_pdf(name: str, data: bytes) -> None:
    try:
        pt.ensure_pdf(data, name)
    except ValueError as e:
        raise HTTPException(415, str(e)) from e


# ---------------------------------------------------------------------------
# Диагностика / окружение
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health():
    env = pt.check_environment()
    ok = "tesseract_languages" in env
    return {"status": "ok" if ok else "degraded", "environment": env}


@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...)):
    """Диагностика PDF: страницы, наличие текста, сканы, метаданные."""
    name, data = await read_upload(file)
    require_pdf(name, data)
    if data[:5] != pt.PDF_MAGIC:
        data = pt.images_to_pdf([(name, data)])
    try:
        return pt.analyze_pdf(data)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Не удалось прочитать PDF: {e}") from e


# ---------------------------------------------------------------------------
# OCR — распознавание (уровень ABBYY FineReader)
# ---------------------------------------------------------------------------


@app.post("/api/ocr/searchable-pdf")
async def ocr_searchable_pdf(
    file: UploadFile = File(...),
    language: str = Form("rus+eng"),
    dpi: int = Form(300),
    force_ocr: bool = Form(False),
):
    """Распознавание сканов -> PDF с невидимым текстовым слоем (поиск/копирование).

    Вход: PDF со сканированными страницами ИЛИ одиночное изображение (JPG/PNG/TIFF).
    Конвейер: растеризация 300 dpi, deskew, очистка фона/шума, Tesseract 5 LSTM.
    """
    name, data = await read_upload(file)
    mime_ok = data[:5] == pt.PDF_MAGIC or (
        (m := __import__("magic").from_buffer(data, mime=True) or "").startswith("image/")
    )
    if not mime_ok:
        raise HTTPException(415, "Ожидается PDF или изображение")
    if data[:5] != pt.PDF_MAGIC:
        data = pt.images_to_pdf([(name, data)])
    try:
        result = pt.run_ocr(data, language=language, dpi=dpi, force_ocr=force_ocr)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Ошибка OCR: {e}") from e
    base = re.sub(r"\.pdf$", "", name, flags=re.I)
    return pdf_response(result, f"{base}.searchable.pdf")


@app.post("/api/ocr/text")
async def ocr_text(
    file: UploadFile = File(...),
    language: str = Form("rus+eng"),
    dpi: int = Form(300),
    pages: str | None = Form(None),
):
    """Полное распознавание документа в plain text (все страницы, включая сканы)."""
    name, data = await read_upload(file)
    if data[:5] != pt.PDF_MAGIC:
        require_pdf(name, data)
        data = pt.images_to_pdf([(name, data)])
    try:
        text = pt.ocr_to_text(data, language=language, dpi=dpi)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Ошибка OCR: {e}") from e
    sel = parse_pages(pages)
    if sel:
        parts = text.split("\n\f\n")
        text = "\n\f\n".join(parts[i - 1] for i in sel if i <= len(parts))
    return JSONResponse({"filename": name, "text": text})


# ---------------------------------------------------------------------------
# Извлечение содержимого
# ---------------------------------------------------------------------------


@app.post("/api/text")
async def extract_text_ep(
    file: UploadFile = File(...),
    pages: str | None = Form(None),
    layout: bool = Form(False),
):
    """Извлечение встроенного текста из PDF (без OCR)."""
    name, data = await read_upload(file)
    require_pdf(name, data)
    try:
        text = pt.extract_text(data, pages=parse_pages(pages), layout=layout)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Ошибка извлечения текста: {e}") from e
    return JSONResponse({"filename": name, "text": text})


@app.post("/api/tables")
async def extract_tables_ep(
    file: UploadFile = File(...),
    pages: str | None = Form(None),
    format: str = Form("json"),
):
    """Извлечение таблиц. format=json|csv|markdown"""
    name, data = await read_upload(file)
    require_pdf(name, data)
    try:
        tables = pt.extract_tables(data, pages=parse_pages(pages))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Ошибка извлечения таблиц: {e}") from e

    if format == "json":
        return JSONResponse({"filename": name, "tables": tables})

    chunks = []
    for t in tables:
        rows = t["table"]
        if format == "csv":
            buf = io.StringIO()
            import csv as _csv
            w = _csv.writer(buf)
            for r in rows:
                w.writerow(r)
            body = buf.getvalue()
        else:  # markdown
            if not rows:
                continue
            head, *rest = rows
            body = "| " + " | ".join(head) + " |\n"
            body += "|" + "---|" * len(head) + "\n"
            body += "".join("| " + " | ".join(r) + " |\n" for r in rest)
        chunks.append(f"--- Стр. {t['page']} ---\n{body}")
    media = "text/csv" if format == "csv" else "text/markdown"
    return Response(content="\n".join(chunks) or "(таблицы не найдены)",
                    media_type=f"{media}; charset=utf-8")


@app.post("/api/images")
async def extract_images_ep(file: UploadFile = File(...)):
    """Все изображения документа в ZIP-архиве."""
    name, data = await read_upload(file)
    require_pdf(name, data)
    try:
        blob = pt.extract_images_zip(data)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Ошибка извлечения изображений: {e}") from e
    base = re.sub(r"\.pdf$", "", name, flags=re.I)
    return zip_response(blob, f"{base}_images.zip")


# ---------------------------------------------------------------------------
# Структурные операции
# ---------------------------------------------------------------------------


@app.post("/api/merge")
async def merge(files: list[UploadFile] = File(...)):
    """Объединение нескольких PDF в один (в порядке загрузки)."""
    blobs = []
    for u in files:
        name, data = await read_upload(u)
        require_pdf(name, data)
        blobs.append(data)
    try:
        result = pt.merge_pdfs(blobs)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Ошибка объединения: {e}") from e
    return pdf_response(result, "merged.pdf")


@app.post("/api/split")
async def split(
    file: UploadFile = File(...),
    ranges: str | None = Form(None),
):
    """Разбиение PDF. ranges='1-3,4-6' -> ZIP с частями; без ranges — постранично."""
    name, data = await read_upload(file)
    require_pdf(name, data)
    parsed_ranges = None
    if ranges:
        parsed_ranges = []
        for tok in ranges.split(","):
            m = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", tok)
            if not m:
                raise HTTPException(400, f"Некорректный диапазон: {tok!r}")
            parsed_ranges.append((int(m.group(1)), int(m.group(2))))
    try:
        parts = pt.split_pdf(data, parsed_ranges)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Ошибка разбиения: {e}") from e
    if len(parts) == 1 and not parsed_ranges:
        return pdf_response(parts[0], re.sub(r"\.pdf$", "", name, flags=re.I) + "_p1.pdf")
    buf = io.BytesIO()
    import zipfile
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        base = re.sub(r"\.pdf$", "", name, flags=re.I)
        for i, part in enumerate(parts, start=1):
            zf.writestr(f"{base}_part{i:03d}.pdf", part)
    return zip_response(buf.getvalue(), f"{base}_split.zip")


@app.post("/api/rotate")
async def rotate(
    file: UploadFile = File(...),
    angle: int = Form(90),
    pages: str | None = Form(None),
):
    """Поворот страниц на angle (90|180|270)."""
    if angle % 90 != 0:
        raise HTTPException(400, "Угол должен быть кратен 90°")
    name, data = await read_upload(file)
    require_pdf(name, data)
    try:
        result = pt.rotate_pages(data, angle % 360, parse_pages(pages))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Ошибка поворота: {e}") from e
    return pdf_response(result, name)


@app.post("/api/delete-pages")
async def delete_pages_ep(file: UploadFile = File(...), pages: str = Form(...)):
    """Удаление страниц, spec='2,5-7'."""
    name, data = await read_upload(file)
    require_pdf(name, data)
    sel = parse_pages(pages)
    if not sel:
        raise HTTPException(400, "Укажите страницы для удаления")
    try:
        result = pt.delete_pages(data, sel)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Ошибка: {e}") from e
    return pdf_response(result, name)


@app.post("/api/reorder")
async def reorder(file: UploadFile = File(...), order: str = Form(...)):
    """Перестановка страниц, order='3,1,2' (должны быть указаны все страницы)."""
    name, data = await read_upload(file)
    require_pdf(name, data)
    try:
        seq = [int(x) for x in order.replace("-", ",").split(",") if x.strip()]
    except ValueError as e:
        raise HTTPException(400, "order — список номеров через запятую") from e
    try:
        result = pt.reorder_pages(data, seq)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Ошибка перестановки: {e}") from e
    return pdf_response(result, name)


@app.post("/api/watermark")
async def watermark(
    file: UploadFile = File(...),
    text: str = Form("CONFIDENTIAL"),
    opacity: float = Form(0.15),
):
    name, data = await read_upload(file)
    require_pdf(name, data)
    try:
        result = pt.watermark_pdf(data, text, min(max(opacity, 0.02), 1.0))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Ошибка наложения знака: {e}") from e
    return pdf_response(result, name)


@app.post("/api/encrypt")
async def encrypt(
    file: UploadFile = File(...),
    password: str = Form(...),
    owner_password: str | None = Form(None),
):
    name, data = await read_upload(file)
    require_pdf(name, data)
    try:
        result = pt.encrypt_pdf(data, password, owner_password)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Ошибка шифрования: {e}") from e
    return pdf_response(result, name)


@app.post("/api/decrypt")
async def decrypt(file: UploadFile = File(...), password: str = Form(...)):
    name, data = await read_upload(file)
    require_pdf(name, data)
    try:
        result = pt.decrypt_pdf(data, password)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Ошибка расшифровки: {e}") from e
    return pdf_response(result, name)


@app.post("/api/compress")
async def compress(file: UploadFile = File(...)):
    """Сжатие PDF (оптимизация изображений, переупаковка объектов)."""
    name, data = await read_upload(file)
    require_pdf(name, data)
    try:
        result = pt.compress_pdf(data)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Ошибка сжатия: {e}") from e
    saved = max(0, round((1 - len(result) / len(data)) * 100))
    resp = pdf_response(result, name)
    resp.headers["X-Compression-Saved-Percent"] = str(saved)
    return resp


# ---------------------------------------------------------------------------
# Конвертации
# ---------------------------------------------------------------------------


@app.post("/api/convert/images-to-pdf")
async def images_to_pdf_ep(files: list[UploadFile] = File(...)):
    """Несколько изображений (JPG/PNG/TIFF…) -> один PDF."""
    items = []
    for u in files:
        name, data = await read_upload(u)
        mime = __import__("magic").from_buffer(data, mime=True) or ""
        if not (mime.startswith("image/") or
                name.lower().endswith((".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp"))):
            raise HTTPException(415, f"{name}: ожидается изображение ({mime})")
        items.append((name, data))
    try:
        result = pt.images_to_pdf(items)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Ошибка конвертации: {e}") from e
    return pdf_response(result, "images.pdf")


@app.post("/api/convert/pdf-to-images")
async def pdf_to_images(
    file: UploadFile = File(...),
    dpi: int = Form(200),
    format: str = Form("png"),
):
    """PDF -> ZIP с изображениями страниц (png|jpeg)."""
    name, data = await read_upload(file)
    require_pdf(name, data)
    if format not in ("png", "jpeg", "jpg"):
        raise HTTPException(400, "format: png или jpeg")
    try:
        blob = pt.pdf_to_images_zip(data, dpi=min(max(dpi, 72), 600),
                                    fmt="png" if format == "png" else "jpeg")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Ошибка растеризации: {e}") from e
    base = re.sub(r"\.pdf$", "", name, flags=re.I)
    return zip_response(blob, f"{base}_images_{dpi}dpi.zip")


@app.post("/api/convert/text-to-pdf")
async def text_to_pdf_ep(
    text: str = Form(...),
    title: str = Form(""),
):
    try:
        result = pt.text_to_pdf(text, title)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Ошибка создания PDF: {e}") from e
    return pdf_response(result, "text.pdf")


# ---------------------------------------------------------------------------
# Веб-интерфейс
# ---------------------------------------------------------------------------

STATIC_DIR = __import__("pathlib").Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
