"""PDF-сервис на Flask: полноценная работа с PDF + OCR уровня ABBYY FineReader.

Запуск (локально):
    python -m app.main            # simple, без внешних серверов
    flask --app app.main run      # то же самое через flask CLI
    gunicorn -w 2 'app.main:app'  # опционально (Linux)

Веб-интерфейс: http://127.0.0.1:8000/
Список API:    http://127.0.0.1:8000/api/endpoints (JSON)
"""

from __future__ import annotations

import csv as csv_mod
import io
import os
import re
import zipfile
from pathlib import Path
from urllib.parse import quote

import magic
from flask import Flask, Response, jsonify, request, send_file

from . import pdf_tools as pt

STATIC_DIR = Path(__file__).parent / "static"

app = Flask(
    __name__,
    static_folder=str(STATIC_DIR),
    static_url_path="/static",
)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024  # 512 МБ


# ---------------------------------------------------------------------------
# Помощники
# ---------------------------------------------------------------------------

class ApiError(Exception):
    """Ошибка API с HTTP-кодом."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@app.errorhandler(ApiError)
def _handle_api_error(e: ApiError):
    return jsonify({"error": e.message}), e.code


@app.errorhandler(413)
def _handle_too_large(_e):
    return jsonify({"error": "Файл слишком большой (лимит 512 МБ)"}), 413


def get_upload(name_field: str = "file"):
    """Возвращает (filename, bytes) из multipart-запроса; ошибки -> ApiError."""
    f = request.files.get(name_field)
    if f is None or not f.filename:
        raise ApiError(400, f"Не передан файл в поле '{name_field}'")
    data = f.read()
    if not data:
        raise ApiError(400, f"Пустой файл {f.filename!r}")
    return (f.filename or "file"), data


def get_uploads(field: str = "files"):
    files = request.files.getlist(field)
    if not files:
        files = request.files.getlist("file[]")
    if not files:
        raise ApiError(400, f"Не переданы файлы в поле '{field}'")
    out = []
    for f in files:
        if not f.filename:
            raise ApiError(400, "Пустое имя файла")
        data = f.read()
        if not data:
            raise ApiError(400, f"Пустой файл {f.filename!r}")
        out.append((f.filename, data))
    return out


def form_str(key: str, default: str = "") -> str:
    v = request.form.get(key)
    return v if v is not None else default


def form_int(key: str, default: int) -> int:
    v = request.form.get(key)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except ValueError:
        raise ApiError(400, f"Поле {key!r} должно быть числом") from None


def form_bool(key: str, default: bool = False) -> bool:
    v = request.form.get(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


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
            raise ApiError(400, f"Некорректная спецификация страниц: {token!r}")
    return sorted(set(out)) or None


def attachment(data: bytes, filename: str, mime: str) -> Response:
    resp = Response(data, mimetype=mime)
    resp.headers["Content-Disposition"] = (
        f"attachment; filename*=UTF-8''{quote(filename)}"
    )
    return resp


def pdf_response(data: bytes, filename: str) -> Response:
    return attachment(data, filename, "application/pdf")


def zip_response(data: bytes, filename: str) -> Response:
    return attachment(data, filename, "application/zip")


def require_pdf(name: str, data: bytes) -> None:
    try:
        pt.ensure_pdf(data, name)
    except ValueError as e:
        raise ApiError(415, str(e)) from e


def base_name(name: str) -> str:
    return re.sub(r"\.(pdf|jpe?g|png|tiff?|webp|bmp)$", "", name, flags=re.I)


# ---------------------------------------------------------------------------
# Диагностика / окружение
# ---------------------------------------------------------------------------

@app.route("/api/health", methods=["GET"])
def health():
    """Диагностика окружения: tesseract, языки, ocrmypdf, ghostscript."""
    env = pt.check_environment()
    ok = "tesseract_languages" in env
    return jsonify({"status": "ok" if ok else "degraded", "environment": env})


@app.route("/api/endpoints", methods=["GET"])
def endpoints():
    """Список всех эндпоинтов (аналог Swagger из FastAPI)."""
    items = []
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: str(r)):
        methods = sorted(m for m in rule.methods if m not in ("HEAD", "OPTIONS"))
        if not methods or rule.endpoint == "static":
            continue
        fn = app.view_functions[rule.endpoint]
        first_doc = (fn.__doc__ or "").strip().splitlines()
        items.append({
            "path": str(rule),
            "methods": methods,
            "summary": first_doc[0] if first_doc else "",
        })
    return jsonify({"service": "Local PDF Service (Flask)",
                    "version": "1.0.0", "endpoints": items})


@app.route("/api/analyze", methods=["POST"])
def analyze():
    """Диагностика PDF: страницы, наличие текста, сканы, метаданные."""
    name, data = get_upload()
    require_pdf(name, data)
    if data[:5] != pt.PDF_MAGIC:
        data = pt.images_to_pdf([(name, data)])
    try:
        return jsonify(pt.analyze_pdf(data))
    except Exception as e:  # noqa: BLE001
        raise ApiError(400, f"Не удалось прочитать PDF: {e}") from e


# ---------------------------------------------------------------------------
# OCR — распознавание (уровень ABBYY FineReader)
# ---------------------------------------------------------------------------

@app.route("/api/ocr/searchable-pdf", methods=["POST"])
def ocr_searchable_pdf():
    """Распознавание сканов -> PDF с невидимым текстовым слоем (поиск/копирование).

    Вход: PDF со сканированными страницами ИЛИ одиночное изображение (JPG/PNG/TIFF).
    Конвейер: растеризация 300 dpi, deskew, очистка фона/шума, Tesseract 5 LSTM.
    """
    name, data = get_upload()
    is_pdf = data[:5] == pt.PDF_MAGIC
    is_img = (magic.from_buffer(data, mime=True) or "").startswith("image/")
    if not (is_pdf or is_img):
        raise ApiError(415, "Ожидается PDF или изображение")
    if not is_pdf:
        data = pt.images_to_pdf([(name, data)])
    try:
        result = pt.run_ocr(
            data,
            language=form_str("language", "rus+eng"),
            dpi=form_int("dpi", 300),
            force_ocr=form_bool("force_ocr"),
        )
    except Exception as e:  # noqa: BLE001
        raise ApiError(500, f"Ошибка OCR: {e}") from e
    return pdf_response(result, f"{base_name(name)}.searchable.pdf")


@app.route("/api/ocr/text", methods=["POST"])
def ocr_text():
    """Полное распознавание документа в plain text (все страницы, включая сканы)."""
    name, data = get_upload()
    if data[:5] != pt.PDF_MAGIC:
        require_pdf(name, data)
        data = pt.images_to_pdf([(name, data)])
    try:
        text = pt.ocr_to_text(
            data,
            language=form_str("language", "rus+eng"),
            dpi=form_int("dpi", 300),
        )
    except Exception as e:  # noqa: BLE001
        raise ApiError(500, f"Ошибка OCR: {e}") from e
    sel = parse_pages(request.form.get("pages"))
    if sel:
        parts = text.split("\n\f\n")
        text = "\n\f\n".join(parts[i - 1] for i in sel if i <= len(parts))
    return jsonify({"filename": name, "text": text})


# ---------------------------------------------------------------------------
# Извлечение содержимого
# ---------------------------------------------------------------------------

@app.route("/api/text", methods=["POST"])
def extract_text_ep():
    """Извлечение встроенного текста из PDF (без OCR)."""
    name, data = get_upload()
    require_pdf(name, data)
    try:
        text = pt.extract_text(data, pages=parse_pages(request.form.get("pages")),
                               layout=form_bool("layout"))
    except Exception as e:  # noqa: BLE001
        raise ApiError(400, f"Ошибка извлечения текста: {e}") from e
    return jsonify({"filename": name, "text": text})


@app.route("/api/tables", methods=["POST"])
def extract_tables_ep():
    """Извлечение таблиц. format=json|csv|markdown"""
    name, data = get_upload()
    require_pdf(name, data)
    try:
        tables = pt.extract_tables(data, pages=parse_pages(request.form.get("pages")))
    except Exception as e:  # noqa: BLE001
        raise ApiError(400, f"Ошибка извлечения таблиц: {e}") from e

    fmt = form_str("format", "json").lower()
    if fmt == "json":
        return jsonify({"filename": name, "tables": tables})

    chunks = []
    for t in tables:
        rows = t["table"]
        if fmt == "csv":
            buf = io.StringIO()
            w = csv_mod.writer(buf)
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
    media = "text/csv" if fmt == "csv" else "text/markdown"
    return Response(content="\n".join(chunks) or "(таблицы не найдены)",
                    content_type=f"{media}; charset=utf-8")


@app.route("/api/images", methods=["POST"])
def extract_images_ep():
    """Все изображения документа в ZIP-архиве."""
    name, data = get_upload()
    require_pdf(name, data)
    try:
        blob = pt.extract_images_zip(data)
    except Exception as e:  # noqa: BLE001
        raise ApiError(400, f"Ошибка извлечения изображений: {e}") from e
    return zip_response(blob, f"{base_name(name)}_images.zip")


# ---------------------------------------------------------------------------
# Структурные операции
# ---------------------------------------------------------------------------

@app.route("/api/merge", methods=["POST"])
def merge():
    """Объединение нескольких PDF в один (в порядке загрузки)."""
    items = get_uploads("files")
    blobs = []
    for name, data in items:
        require_pdf(name, data)
        blobs.append(data)
    try:
        result = pt.merge_pdfs(blobs)
    except Exception as e:  # noqa: BLE001
        raise ApiError(400, f"Ошибка объединения: {e}") from e
    return pdf_response(result, "merged.pdf")


@app.route("/api/split", methods=["POST"])
def split():
    """Разбиение PDF. ranges='1-3,4-6' -> ZIP с частями; без ranges — постранично."""
    name, data = get_upload()
    require_pdf(name, data)
    ranges = request.form.get("ranges")
    parsed_ranges = None
    if ranges:
        parsed_ranges = []
        for tok in ranges.split(","):
            m = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", tok)
            if not m:
                raise ApiError(400, f"Некорректный диапазон: {tok!r}")
            parsed_ranges.append((int(m.group(1)), int(m.group(2))))
    try:
        parts = pt.split_pdf(data, parsed_ranges)
    except Exception as e:  # noqa: BLE001
        raise ApiError(400, f"Ошибка разбиения: {e}") from e
    if len(parts) == 1 and not parsed_ranges:
        return pdf_response(parts[0], base_name(name) + "_p1.pdf")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        bn = base_name(name)
        for i, part in enumerate(parts, start=1):
            zf.writestr(f"{bn}_part{i:03d}.pdf", part)
    return zip_response(buf.getvalue(), f"{bn}_split.zip")


@app.route("/api/rotate", methods=["POST"])
def rotate():
    """Поворот страниц на angle (90|180|270)."""
    angle = form_int("angle", 90)
    if angle % 90 != 0:
        raise ApiError(400, "Угол должен быть кратен 90°")
    name, data = get_upload()
    require_pdf(name, data)
    try:
        result = pt.rotate_pages(data, angle % 360, parse_pages(request.form.get("pages")))
    except Exception as e:  # noqa: BLE001
        raise ApiError(400, f"Ошибка поворота: {e}") from e
    return pdf_response(result, name)


@app.route("/api/delete-pages", methods=["POST"])
def delete_pages_ep():
    """Удаление страниц, spec='2,5-7'."""
    name, data = get_upload()
    require_pdf(name, data)
    sel = parse_pages(form_str("pages"))
    if not sel:
        raise ApiError(400, "Укажите страницы для удаления")
    try:
        result = pt.delete_pages(data, sel)
    except Exception as e:  # noqa: BLE001
        raise ApiError(400, f"Ошибка: {e}") from e
    return pdf_response(result, name)


@app.route("/api/reorder", methods=["POST"])
def reorder():
    """Перестановка страниц, order='3,1,2' (должны быть указаны все страницы)."""
    name, data = get_upload()
    require_pdf(name, data)
    try:
        seq = [int(x) for x in form_str("order").replace("-", ",").split(",") if x.strip()]
    except ValueError as e:
        raise ApiError(400, "order — список номеров через запятую") from e
    try:
        result = pt.reorder_pages(data, seq)
    except Exception as e:  # noqa: BLE001
        raise ApiError(400, f"Ошибка перестановки: {e}") from e
    return pdf_response(result, name)


@app.route("/api/watermark", methods=["POST"])
def watermark():
    """Водяной знак поверх всех страниц."""
    name, data = get_upload()
    require_pdf(name, data)
    try:
        opacity = float(form_str("opacity", "0.15"))
    except ValueError as e:
        raise ApiError(400, "opacity — число от 0 до 1") from e
    try:
        result = pt.watermark_pdf(data, form_str("text", "CONFIDENTIAL"),
                                  min(max(opacity, 0.02), 1.0))
    except Exception as e:  # noqa: BLE001
        raise ApiError(400, f"Ошибка наложения знака: {e}") from e
    return pdf_response(result, name)


@app.route("/api/encrypt", methods=["POST"])
def encrypt():
    """Шифрование паролем."""
    name, data = get_upload()
    require_pdf(name, data)
    password = form_str("password")
    if not password:
        raise ApiError(400, "Укажите пароль")
    try:
        result = pt.encrypt_pdf(data, password, request.form.get("owner_password") or None)
    except Exception as e:  # noqa: BLE001
        raise ApiError(400, f"Ошибка шифрования: {e}") from e
    return pdf_response(result, name)


@app.route("/api/decrypt", methods=["POST"])
def decrypt():
    """Снятие пароля."""
    name, data = get_upload()
    require_pdf(name, data)
    try:
        result = pt.decrypt_pdf(data, form_str("password"))
    except ValueError as e:
        raise ApiError(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise ApiError(400, f"Ошибка расшифровки: {e}") from e
    return pdf_response(result, name)


@app.route("/api/compress", methods=["POST"])
def compress():
    """Сжатие PDF (оптимизация изображений, переупаковка объектов)."""
    name, data = get_upload()
    require_pdf(name, data)
    try:
        result = pt.compress_pdf(data)
    except Exception as e:  # noqa: BLE001
        raise ApiError(400, f"Ошибка сжатия: {e}") from e
    saved = max(0, round((1 - len(result) / len(data)) * 100))
    resp = pdf_response(result, name)
    resp.headers["X-Compression-Saved-Percent"] = str(saved)
    return resp


# ---------------------------------------------------------------------------
# Конвертации
# ---------------------------------------------------------------------------

@app.route("/api/convert/images-to-pdf", methods=["POST"])
def images_to_pdf_ep():
    """Несколько изображений (JPG/PNG/TIFF…) -> один PDF."""
    items = []
    for name, data in get_uploads("files"):
        mime = magic.from_buffer(data, mime=True) or ""
        if not (mime.startswith("image/") or
                name.lower().endswith((".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp"))):
            raise ApiError(415, f"{name}: ожидается изображение ({mime})")
        items.append((name, data))
    try:
        result = pt.images_to_pdf(items)
    except Exception as e:  # noqa: BLE001
        raise ApiError(400, f"Ошибка конвертации: {e}") from e
    return pdf_response(result, "images.pdf")


@app.route("/api/convert/pdf-to-images", methods=["POST"])
def pdf_to_images_ep():
    """PDF -> ZIP с изображениями страниц (png|jpeg)."""
    name, data = get_upload()
    require_pdf(name, data)
    fmt = form_str("format", "png").lower()
    if fmt not in ("png", "jpeg", "jpg"):
        raise ApiError(400, "format: png или jpeg")
    dpi = min(max(form_int("dpi", 200), 72), 600)
    try:
        blob = pt.pdf_to_images_zip(data, dpi=dpi, fmt="png" if fmt == "png" else "jpeg")
    except Exception as e:  # noqa: BLE001
        raise ApiError(400, f"Ошибка растеризации: {e}") from e
    return zip_response(blob, f"{base_name(name)}_images_{dpi}dpi.zip")


@app.route("/api/convert/text-to-pdf", methods=["POST"])
def text_to_pdf_ep():
    """Текст (форма 'text') -> аккуратный PDF."""
    text = form_str("text")
    if not text.strip():
        raise ApiError(400, "Пустой текст")
    try:
        result = pt.text_to_pdf(text, form_str("title", ""))
    except Exception as e:  # noqa: BLE001
        raise ApiError(400, f"Ошибка создания PDF: {e}") from e
    return pdf_response(result, "text.pdf")


# ---------------------------------------------------------------------------
# Веб-интерфейс
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    """Веб-интерфейс сервиса."""
    return send_file(STATIC_DIR / "index.html")


def main():
    port = int(os.environ.get("PORT", "8000"))
    print("=" * 60)
    print(f"  PDF Service (Flask) | Web UI: http://127.0.0.1:{port}/")
    print(f"  API list:           http://127.0.0.1:{port}/api/endpoints")
    print("  Stop: Ctrl+C")
    print("=" * 60)
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
