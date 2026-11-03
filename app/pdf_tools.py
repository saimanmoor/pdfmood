"""Утилиты для работы с PDF: OCR, извлечение текста/таблиц, конвертация."""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import img2pdf
import magic
import ocrmypdf
import pdfplumber
from PIL import Image
from pypdf import PdfReader, PdfWriter

# ---------------------------------------------------------------------------
# Проверки окружения
# ---------------------------------------------------------------------------


def check_environment() -> dict:
    """Возвращает информацию о доступных компонентах OCR-конвейера."""
    info: dict = {}
    try:
        out = subprocess.run(
            ["tesseract", "--list-langs"], capture_output=True, text=True, timeout=30
        )
        langs = [l.strip() for l in out.stdout.splitlines()[1:] if l.strip()]
        info["tesseract_languages"] = langs
    except Exception as e:  # noqa: BLE001
        info["tesseract_error"] = str(e)
    info["ocrmypdf_version"] = getattr(ocrmypdf, "__version__", "unknown")
    for tool in ("pdftoppm", "jbig2enc", "pngquant"):
        info[f"{tool}_available"] = shutil.which(tool) is not None
    return info


# ---------------------------------------------------------------------------
# Общие помощники
# ---------------------------------------------------------------------------

PDF_MAGIC = b"%PDF-"


def ensure_pdf(data: bytes, name: str = "") -> None:
    """Проверяет, что данные действительно PDF (или изображение-скан)."""
    if data[:5] == PDF_MAGIC:
        return
    mime = magic.from_buffer(data, mime=True)
    if mime and mime.startswith("image/"):
        return
    raise ValueError(f"Файл {name!r} не является PDF (определён тип: {mime}). Ожидался application/pdf.")


def _open_reader(data: bytes) -> PdfReader:
    return PdfReader(io.BytesIO(data))


# ---------------------------------------------------------------------------
# Диагностика страниц
# ---------------------------------------------------------------------------


def page_is_scanned(page: pdfplumber.page.Page) -> bool:
    """Страница считается сканом, если на ней почти нет извлекаемого текста."""
    text = (page.extract_text() or "").strip()
    return len(text) < 10


def analyze_pdf(data: bytes, password: str = "") -> dict:
    """Сводка по документу: кол-во страниц, текстовые/скан страницы, метаданные."""
    reader = _open_reader(data)
    if reader.is_encrypted and password:
        reader.decrypt(password)
    meta = {}
    try:
        if reader.metadata:
            meta = {k.lstrip("/"): str(v) for k, v in reader.metadata.items()}
    except Exception:  # noqa: BLE001
        pass
    scanned_pages, text_pages, sizes = [], [], []
    try:
        with pdfplumber.open(io.BytesIO(data), password=password) as pdf:
            total = len(pdf.pages)
            for i, page in enumerate(pdf.pages, start=1):
                sizes.append({"width": round(page.width, 1), "height": round(page.height, 1)})
                (scanned_pages if page_is_scanned(page) else text_pages).append(i)
    except Exception:  # noqa: BLE001 — защищённый документ и т.п.
        try:
            total = len(reader.pages)
        except Exception:  # noqa: BLE001
            total = -1
    return {
        "pages": total,
        "scanned_pages": scanned_pages,
        "text_pages": text_pages,
        "page_sizes": sizes,
        "metadata": meta,
        "is_encrypted": reader.is_encrypted,
    }


# ---------------------------------------------------------------------------
# OCR (движок уровня ABBYY FineReader: Tesseract + OCRmyPDF)
# ---------------------------------------------------------------------------

DEFAULT_OCR_ARGS = dict(
    language="rus+eng",
    dpi=300,
    output_type="textonly",        # изображение + невидимый текстовый слой
    remove_background=True,        # очистка фона (шум «грязного» сканера)
    deskew=True,                   # автоматическое выпрямление страниц
    clean=True,                    # морфологическая чистка изображения
    clean_level="clean",
    oversize=4_000_000,            # растеризация векторных страниц в высокое разрешение
    optimize=0,
    jobs=2,
)


def run_ocr(
    data: bytes,
    *,
    language: str = "rus+eng",
    dpi: int = 300,
    force_ocr: bool = False,
) -> bytes:
    """
    Распознаёт PDF и возвращает PDF с невидимым текстовым слоем.

    - force_ocr=False: страницы со встроенным текстом остаются как есть,
      сканированные распознаются (output_type=textonly).
    - force_ocr=True: все страницы растеризуются и распознаются заново
      (output_type=none — только распознанный текст поверх изображения).
    """
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "input.pdf"
        dst = Path(td) / "output.pdf"
        src.write_bytes(data)
        kwargs = dict(DEFAULT_OCR_ARGS)
        kwargs["language"] = language
        kwargs["dpi"] = dpi
        kwargs["force_ocr"] = force_ocr
        kwargs["skip_text"] = not force_ocr
        if force_ocr:
            kwargs["output_type"] = "none"   # всегда наложить слой на все страницы
        ocrmypdf.ocr(str(src), str(dst), **kwargs)
        return dst.read_bytes()


def ocr_to_text(data: bytes, *, language: str = "rus+eng", dpi: int = 300,
                force_ocr: bool = True) -> str:
    """Полное распознавание -> plain text (страницы разделены form feed)."""
    searched = run_ocr(data, language=language, dpi=dpi, force_ocr=force_ocr)
    parts = []
    with pdfplumber.open(io.BytesIO(searched)) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    return "\n\f\n".join(parts)


# ---------------------------------------------------------------------------
# Извлечение текста / таблиц / изображений
# ---------------------------------------------------------------------------


def _select(pages, pages_arg):
    if not pages_arg:
        return list(pages)
    wanted = set(pages_arg)
    return [p for i, p in enumerate(pages, start=1) if i in wanted]


def extract_text(data: bytes, pages=None, layout: bool = False) -> str:
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        selected = _select(pdf.pages, pages)
        chunks = [(p.extract_text(layout=layout) or "") for p in selected]
    return "\n\f\n".join(chunks)


def extract_tables(data: bytes, pages=None) -> list[dict]:
    """Извлечение таблиц: сначала по линиям сетки, затем — по тексту (для таблиц
    без рамок, как в FineReader)."""
    result = []
    seen: set[tuple[int, tuple]] = set()
    strategies = [
        {"vertical_strategy": "lines", "horizontal_strategy": "lines"},
        {"vertical_strategy": "text", "horizontal_strategy": "text"},
    ]
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        selected = _select(pdf.pages, pages)
        idx_iter = sorted(set(pages)) if pages else range(1, len(pdf.pages) + 1)
        for idx, page in zip(idx_iter, selected):
            page_has_result = False
            for strat in strategies:
                try:
                    found = page.extract_tables(strat)
                except Exception:  # noqa: BLE001
                    continue
                for t in found:
                    cleaned = [[(c if c is not None else "").strip() for c in row] for row in t]
                    # отбрасываем вырожденные «таблицы» из одной ячейки-строки
                    if len(cleaned) < 2 or max(len(r) for r in cleaned) < 2:
                        continue
                    key = (idx, tuple(tuple(r) for r in cleaned))
                    if key in seen:
                        continue
                    seen.add(key)
                    result.append({"page": idx, "table": cleaned})
                    page_has_result = True
                if page_has_result:  # линии сработали — текстовая эвристика не нужна
                    break
    return result


def extract_images_zip(data: bytes) -> bytes:
    """Все встроенные изображения документа в ZIP-архиве."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for pnum, page in enumerate(pdf.pages, start=1):
                for inum, img in enumerate(page.images, start=1):
                    x0, top, x1, bottom = img["bbox"]
                    crop = page.crop((max(0, x0), max(0, top),
                                      min(page.width, x1), min(page.height, bottom)))
                    im = crop.to_image(resolution=200).original
                    out = io.BytesIO()
                    im.save(out, format="PNG")
                    zf.writestr(f"page{pnum}_img{inum}.png", out.getvalue())
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Структурные операции
# ---------------------------------------------------------------------------


def merge_pdfs(list_of_bytes: list[bytes]) -> bytes:
    writer = PdfWriter()
    for data in list_of_bytes:
        reader = _open_reader(data)
        for page in reader.pages:
            writer.add_page(page)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def split_pdf(data: bytes, ranges=None) -> list[bytes]:
    """ranges — список (начало, конец) включительно, 1-based.
    Без ranges — по одной странице на файл."""
    reader = _open_reader(data)
    n = len(reader.pages)
    if not ranges:
        ranges = [(i, i) for i in range(1, n + 1)]
    out = []
    for a, b in ranges:
        a, b = max(1, a), min(n, b)
        writer = PdfWriter()
        for i in range(a, b + 1):
            writer.add_page(reader.pages[i - 1])
        buf = io.BytesIO()
        writer.write(buf)
        out.append(buf.getvalue())
    return out


def rotate_pages(data: bytes, angle: int, pages=None) -> bytes:
    reader = _open_reader(data)
    writer = PdfWriter()
    targets = set(pages) if pages else set(range(1, len(reader.pages) + 1))
    for i, page in enumerate(reader.pages, start=1):
        if i in targets:
            page.rotate(angle)
        writer.add_page(page)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def delete_pages(data: bytes, pages: list[int]) -> bytes:
    reader = _open_reader(data)
    writer = PdfWriter()
    drop = set(pages)
    for i, page in enumerate(reader.pages, start=1):
        if i not in drop:
            writer.add_page(page)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def reorder_pages(data: bytes, order: list[int]) -> bytes:
    reader = _open_reader(data)
    writer = PdfWriter()
    for p in order:
        writer.add_page(reader.pages[p - 1])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def watermark_pdf(data: bytes, text: str, opacity: float = 0.15) -> bytes:
    """Текстовый водяной знак по диагонали на каждой странице."""
    from reportlab.lib.colors import Color
    from reportlab.pdfgen import canvas

    reader = _open_reader(data)
    writer = PdfWriter()

    def make_overlay(w: float, h: float, path: str) -> PdfReader:
        c = canvas.Canvas(path, pagesize=(w, h))
        c.setFillColor(Color(0.5, 0.5, 0.5, alpha=opacity))
        c.setFont("Helvetica-Bold", 60)
        c.translate(w / 2, h / 2)
        c.rotate(45)
        c.drawCentredString(0, 0, text)
        c.save()
        return PdfReader(path)

    with tempfile.TemporaryDirectory() as td:
        cache: dict[tuple[int, int], PdfReader] = {}
        for page_obj in reader.pages:
            pw = float(page_obj.mediabox.width)
            ph = float(page_obj.mediabox.height)
            key = (round(pw), round(ph))
            if key not in cache:
                cache[key] = make_overlay(pw, ph, str(Path(td) / f"wm_{key[0]}_{key[1]}.pdf"))
            page_obj.merge_page(cache[key].pages[0])
            writer.add_page(page_obj)
        buf = io.BytesIO()
        writer.write(buf)
    return buf.getvalue()


def encrypt_pdf(data: bytes, user_pw: str, owner_pw: str | None = None) -> bytes:
    reader = _open_reader(data)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(user_password=user_pw, owner_password=owner_pw or user_pw)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def decrypt_pdf(data: bytes, password: str) -> bytes:
    reader = _open_reader(data)
    if not reader.is_encrypted:
        return data
    if not reader.decrypt(password):
        raise ValueError("Неверный пароль или документ защищён от чтения")
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def compress_pdf(data: bytes) -> bytes:
    """Сжатие: downsampling изображений через оптимизатор OCRmyPDF."""
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.pdf"
        dst = Path(td) / "out.pdf"
        src.write_bytes(data)
        try:
            ocrmypdf.ocr(
                str(src), str(dst),
                optimize=2, jpeg_quality=60, png_quality=60,
                skip_text=True, remove_background=False,
                clean=False, jobs=2,
            )
            result = dst.read_bytes()
        except Exception:  # noqa: BLE001
            result = _naive_compress(data)
        naive = _naive_compress(data)
        best = min([data, result, naive], key=len)
        return best if len(best) < len(data) else naive


def _naive_compress(data: bytes) -> bytes:
    writer = PdfWriter(clone_from=io.BytesIO(data))
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Конвертации
# ---------------------------------------------------------------------------


def images_to_pdf(image_files: list[tuple[str, bytes]]) -> bytes:
    """Список (имя, байты) изображений -> один PDF."""
    paths = []
    tmpdir = tempfile.mkdtemp()
    try:
        for name, blob in image_files:
            ext = Path(name).suffix.lower()
            if ext not in (".jpg", ".jpeg", ".png", ".tif", ".tiff"):
                im = Image.open(io.BytesIO(blob))
                if im.mode in ("RGBA", "P", "LA"):
                    im = im.convert("RGB")
                p = Path(tmpdir) / (Path(name).stem + ".jpg")
                im.save(p, "JPEG", quality=92)
            else:
                p = Path(tmpdir) / Path(name).name
                p.write_bytes(blob)
            paths.append(str(p))
        return img2pdf.convert(paths)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def pdf_to_images_zip(data: bytes, dpi: int = 200, fmt: str = "png") -> bytes:
    """Каждая страница -> изображение, всё в ZIP."""
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.pdf"
        src.write_bytes(data)
        prefix = Path(td) / "page"
        subprocess.run(
            ["pdftoppm", "-r", str(dpi), "-png", str(src), str(prefix)],
            check=True, capture_output=True,
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            files = sorted(Path(td).glob("page-*.png"))
            if not files:
                raise ValueError("Не удалось растеризовать PDF (пустой документ?)")
            for f in files:
                if fmt == "png":
                    zf.write(f, f.name)
                else:
                    im = Image.open(f).convert("RGB")
                    out = io.BytesIO()
                    im.save(out, "JPEG", quality=90)
                    zf.writestr(f.with_suffix(".jpg").name, out.getvalue())
    return buf.getvalue()


def text_to_pdf(text: str, title: str = "") -> bytes:
    """Plain text -> PDF (кириллица/юникод через шрифт DejaVu Sans)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    font_name = _register_unicode_font()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=50, bottomMargin=50)
    styles = getSampleStyleSheet()
    body = ParagraphStyle("bodyru", parent=styles["Normal"], fontName=font_name,
                          fontSize=11, leading=15)
    story = []
    if title:
        story.append(Paragraph(_xml_escape(title), body))
        story.append(Spacer(1, 12))
    for line in (text.splitlines() or [""]):
        safe = _xml_escape(line) or "&nbsp;"
        story.append(Paragraph(safe, body))
    doc.build(story)
    return buf.getvalue()


_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]


def _register_unicode_font() -> str:
    """Регистрирует TTF-шрифт с полной поддержкой юникода (для извлекаемости текста)."""
    from reportlab.pdfbase import pdfmetrics

    if "PdfUnicode" in pdfmetrics.getRegisteredFontNames():
        return "PdfUnicode"
    from reportlab.pdfbase.ttfonts import TTFont
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            pdfmetrics.registerFont(TTFont("PdfUnicode", path))
            return "PdfUnicode"
    # fallback: CID-шрифт (текст может извлекаться как cid-коды в старых viewers)
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    return "STSong-Light"


def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
