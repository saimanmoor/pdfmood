"""Интеграционные тесты PDF-сервиса (FastAPI TestClient)."""

import io
import zipfile

from fastapi.testclient import TestClient

from app import pdf_tools as pt
from app.main import app

client = TestClient(app)


def make_text_pdf() -> bytes:
    return pt.text_to_pdf(
        "Уважаемый клиент!\nВаш счёт 123456 открыт на сумму 10 000 рублей.\n"
        "Dear client, your account is active.",
        title="Тестовый документ",
    )


def make_scanned_pdf() -> bytes:
    """Создаём 'скан': рендерим текстовый PDF в картинку и обратно в PDF без текста."""
    text_pdf = pt.text_to_pdf("Распознанный текст для проверки OCR quality.\nHello OCR world 12345")
    zipped = pt.pdf_to_images_zip(text_pdf, dpi=300, fmt="png")
    with zipfile.ZipFile(io.BytesIO(zipped)) as zf:
        img = zf.read(zf.namelist()[0])
    return pt.images_to_pdf([("scan.png", img)])


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    env = r.json()["environment"]
    assert "rus" in env.get("tesseract_languages", []), env
    print("health:", r.json()["status"], "| langs:", env["tesseract_languages"])


def test_analyze():
    data = make_scanned_pdf()
    r = client.post("/api/analyze", files={"file": ("s.pdf", data, "application/pdf")})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["pages"] >= 1 and j["scanned_pages"] == [1], j
    print("analyze OK:", {k: j[k] for k in ("pages", "scanned_pages", "text_pages")})


def test_text_extraction():
    data = make_text_pdf()
    r = client.post("/api/text", files={"file": ("a.pdf", data, "application/pdf")},
                    data={"layout": "false"})
    assert r.status_code == 200, r.text
    text = r.json()["text"]
    assert "123456" in text and "Уважаемый" in text, text
    print("text extraction OK, len =", len(text))


def test_ocr_searchable_pdf():
    data = make_scanned_pdf()
    r = client.post("/api/ocr/searchable-pdf",
                    files={"file": ("scan.pdf", data, "application/pdf")},
                    data={"language": "rus+eng", "dpi": "300", "force_ocr": "true"})
    assert r.status_code == 200, r.text[:500]
    assert r.content[:5] == b"%PDF-"
    # проверяем, что текстовый слой появился
    info = pt.analyze_pdf(r.content)
    assert not info["scanned_pages"], f"страницы остались без текста: {info}"
    extracted = pt.extract_text(r.content)
    assert "OCR" in extracted or "12345" in extracted, extracted
    print("OCR searchable PDF OK. Текст после распознавания:")
    print("---\n" + extracted + "\n---")


def test_ocr_text_endpoint():
    data = make_scanned_pdf()
    r = client.post("/api/ocr/text",
                    files={"file": ("scan.pdf", data, "application/pdf")},
                    data={"language": "rus+eng", "dpi": "300"})
    assert r.status_code == 200, r.text[:500]
    text = r.json()["text"]
    assert len(text.strip()) > 10, text
    print("OCR->text OK, sample:", repr(text[:80]))


def test_structural_ops():
    a, b = make_text_pdf(), make_text_pdf()
    # merge
    r = client.post("/api/merge",
                    files=[("files", ("a.pdf", a, "application/pdf")),
                           ("files", ("b.pdf", b, "application/pdf"))])
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"
    merged = r.content
    assert pt.analyze_pdf(merged)["pages"] == 2
    # split
    r = client.post("/api/split", files={"file": ("m.pdf", merged, "application/pdf")})
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert len(zf.namelist()) == 2
    # rotate / delete / reorder / watermark
    r = client.post("/api/rotate", files={"file": ("m.pdf", merged, "application/pdf")},
                    data={"angle": "90", "pages": "1"})
    assert r.status_code == 200
    r = client.post("/api/delete-pages", files={"file": ("m.pdf", merged, "application/pdf")},
                    data={"pages": "2"})
    assert r.status_code == 200 and pt.analyze_pdf(r.content)["pages"] == 1
    r = client.post("/api/reorder", files={"file": ("m.pdf", merged, "application/pdf")},
                    data={"order": "2,1"})
    assert r.status_code == 200
    r = client.post("/api/watermark", files={"file": ("m.pdf", merged, "application/pdf")},
                    data={"text": "КОПИЯ"})
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"
    # encrypt / decrypt
    r = client.post("/api/encrypt", files={"file": ("m.pdf", merged, "application/pdf")},
                    data={"password": "secret123"})
    assert r.status_code == 200
    enc = r.content
    assert pt.analyze_pdf(enc)["is_encrypted"]
    r = client.post("/api/decrypt", files={"file": ("e.pdf", enc, "application/pdf")},
                    data={"password": "secret123"})
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"
    # compress
    r = client.post("/api/compress", files={"file": ("m.pdf", merged, "application/pdf")})
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"
    print("structural ops OK (merge/split/rotate/delete/reorder/watermark/encrypt/decrypt/compress)")


def test_conversions():
    data = make_text_pdf()
    # pdf -> images zip
    r = client.post("/api/convert/pdf-to-images",
                    files={"file": ("a.pdf", data, "application/pdf")},
                    data={"dpi": "150", "format": "png"})
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert any(n.endswith(".png") for n in zf.namelist())
    # images -> pdf
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        png = zf.read([n for n in zf.namelist() if n.endswith(".png")][0])
    r = client.post("/api/convert/images-to-pdf", files=[("files", ("p.png", png, "image/png"))])
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"
    # text -> pdf
    r = client.post("/api/convert/text-to-pdf", data={"text": "Привет, мир!", "title": "T"})
    assert r.status_code == 200
    assert "Привет" in pt.extract_text(r.content)
    print("conversions OK (pdf->images, images->pdf, text->pdf)")


def test_tables():
    # генерируем PDF с таблицей через reportlab (юникод-шрифт из pdf_tools)
    import io as _io

    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Table

    font = pt._register_unicode_font()
    buf = _io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    t = Table([["Товар", "Цена", "Кол-во"],
               ["Молоко", "99.50", "2"],
               ["Хлеб", "45.00", "3"]], hAlign="LEFT")
    from reportlab.platypus import TableStyle
    t.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), font)]))
    doc.build([t])
    data = buf.getvalue()
    r = client.post("/api/tables", files={"file": ("t.pdf", data, "application/pdf")},
                    data={"format": "json"})
    assert r.status_code == 200, r.text
    tables = r.json()["tables"]
    assert tables and tables[0]["table"][0][0] == "Товар", tables
    r = client.post("/api/tables", files={"file": ("t.pdf", data, "application/pdf")},
                    data={"format": "csv"})
    assert "Молоко" in r.text
    print("tables OK:", tables[0]["table"])


if __name__ == "__main__":
    for fn in [test_health, test_analyze, test_text_extraction, test_tables,
               test_structural_ops, test_conversions, test_ocr_searchable_pdf,
               test_ocr_text_endpoint]:
        print(f"\n=== {fn.__name__} ===")
        fn()
    print("\nВСЕ ТЕСТЫ ПРОЙДЕНЫ ✅")
