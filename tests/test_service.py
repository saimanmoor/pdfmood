"""Интеграционные тесты PDF-сервиса (Flask test_client)."""

import io
import zipfile

from app import pdf_tools as pt
from app.main import app


def _encode_multipart(fields):
    """fields: list of (name, value) where value is str or (FileStorage-tuple)."""
    import uuid as _uuid
    boundary = "----pytest" + _uuid.uuid4().hex
    parts = []
    for name, value in fields:
        if isinstance(value, tuple):
            stream, fname, mime = value
            data = stream.read() if hasattr(stream, "read") else value[0]
            part = (f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"; filename="{fname}"\r\n'
                    f"Content-Type: {mime or 'application/octet-stream'}\r\n\r\n").encode()
            part += data + b"\r\n"
        else:
            part = (f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n").encode("utf-8")
        parts.append(part)
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


class _ClientShim:
    """Flask test_client wrapper accepting fastapi-style `files=` kwarg."""

    def __init__(self, c):
        self._c = c

    def get(self, url, **kw):
        return self._c.get(url, **kw)

    def post(self, url, data=None, files=None, **kw):
        fields = []
        for k, v in (data or {}).items():
            if isinstance(v, list):
                fields.extend((k, item) for item in v)
            else:
                fields.append((k, v))
        if files:
            entries = files.items() if isinstance(files, dict) else files
            for field, entry in entries:
                fields.append((field, entry))
        body, ctype = _encode_multipart(fields)
        kw.setdefault("headers", {})["Content-Type"] = ctype
        return self._c.post(url, data=body, **kw)


client = _ClientShim(app.test_client())


def _json(r):
    return r.get_json()


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
    env = _json(r)["environment"]
    assert "rus" in env.get("tesseract_languages", []), env
    print("health:", _json(r)["status"], "| langs:", env["tesseract_languages"])


def test_analyze():
    data = make_scanned_pdf()
    r = client.post("/api/analyze", files={"file": (io.BytesIO(data), "s.pdf", "application/pdf")})
    assert r.status_code == 200, r.text
    j = _json(r)
    assert j["pages"] >= 1 and j["scanned_pages"] == [1], j
    print("analyze OK:", {k: j[k] for k in ("pages", "scanned_pages", "text_pages")})


def test_text_extraction():
    data = make_text_pdf()
    r = client.post("/api/text", files={"file": (io.BytesIO(data), "a.pdf", "application/pdf")},
                    data={"layout": "false"})
    assert r.status_code == 200, r.text
    text = _json(r)["text"]
    assert "123456" in text and "Уважаемый" in text, text
    print("text extraction OK, len =", len(text))


def test_ocr_searchable_pdf():
    data = make_scanned_pdf()
    r = client.post("/api/ocr/searchable-pdf",
                    files={"file": (io.BytesIO(data), "scan.pdf", "application/pdf")},
                    data={"language": "rus+eng", "dpi": "300", "force_ocr": "true"})
    assert r.status_code == 200, r.text[:500]
    assert r.data[:5] == b"%PDF-"
    # проверяем, что текстовый слой появился
    info = pt.analyze_pdf(r.data)
    assert not info["scanned_pages"], f"страницы остались без текста: {info}"
    extracted = pt.extract_text(r.data)
    assert "OCR" in extracted or "12345" in extracted, extracted
    print("OCR searchable PDF OK. Текст после распознавания:")
    print("---\n" + extracted + "\n---")


def test_ocr_text_endpoint():
    data = make_scanned_pdf()
    r = client.post("/api/ocr/text",
                    files={"file": (io.BytesIO(data), "scan.pdf", "application/pdf")},
                    data={"language": "rus+eng", "dpi": "300"})
    assert r.status_code == 200, r.text[:500]
    text = _json(r)["text"]
    assert len(text.strip()) > 10, text
    print("OCR->text OK, sample:", repr(text[:80]))


def test_structural_ops():
    a, b = make_text_pdf(), make_text_pdf()
    # merge
    r = client.post("/api/merge",
                    files=[("files", (io.BytesIO(a), "a.pdf", "application/pdf")),
                           ("files", (io.BytesIO(b), "b.pdf", "application/pdf"))])
    assert r.status_code == 200 and r.data[:5] == b"%PDF-"
    merged = r.data
    assert pt.analyze_pdf(merged)["pages"] == 2
    # split
    r = client.post("/api/split", files={"file": (io.BytesIO(merged), "m.pdf", "application/pdf")})
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.data)) as zf:
        assert len(zf.namelist()) == 2
    # rotate / delete / reorder / watermark
    r = client.post("/api/rotate", files={"file": (io.BytesIO(merged), "m.pdf", "application/pdf")},
                    data={"angle": "90", "pages": "1"})
    assert r.status_code == 200
    r = client.post("/api/delete-pages", files={"file": (io.BytesIO(merged), "m.pdf", "application/pdf")},
                    data={"pages": "2"})
    assert r.status_code == 200 and pt.analyze_pdf(r.data)["pages"] == 1
    r = client.post("/api/reorder", files={"file": (io.BytesIO(merged), "m.pdf", "application/pdf")},
                    data={"order": "2,1"})
    assert r.status_code == 200
    r = client.post("/api/watermark", files={"file": (io.BytesIO(merged), "m.pdf", "application/pdf")},
                    data={"text": "КОПИЯ"})
    assert r.status_code == 200 and r.data[:5] == b"%PDF-"
    # encrypt / decrypt
    r = client.post("/api/encrypt", files={"file": (io.BytesIO(merged), "m.pdf", "application/pdf")},
                    data={"password": "secret123"})
    assert r.status_code == 200
    enc = r.data
    assert pt.analyze_pdf(enc)["is_encrypted"]
    r = client.post("/api/decrypt", files={"file": (io.BytesIO(enc), "e.pdf", "application/pdf")},
                    data={"password": "secret123"})
    assert r.status_code == 200 and r.data[:5] == b"%PDF-"
    # compress
    r = client.post("/api/compress", files={"file": (io.BytesIO(merged), "m.pdf", "application/pdf")})
    assert r.status_code == 200 and r.data[:5] == b"%PDF-"
    print("structural ops OK (merge/split/rotate/delete/reorder/watermark/encrypt/decrypt/compress)")


def test_conversions():
    data = make_text_pdf()
    # pdf -> images zip
    r = client.post("/api/convert/pdf-to-images",
                    files={"file": (io.BytesIO(data), "a.pdf", "application/pdf")},
                    data={"dpi": "150", "format": "png"})
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.data)) as zf:
        assert any(n.endswith(".png") for n in zf.namelist())
    # images -> pdf
    with zipfile.ZipFile(io.BytesIO(r.data)) as zf:
        png = zf.read([n for n in zf.namelist() if n.endswith(".png")][0])
    r = client.post("/api/convert/images-to-pdf", files=[("files", (io.BytesIO(png), "p.png", "image/png"))])
    assert r.status_code == 200 and r.data[:5] == b"%PDF-"
    # text -> pdf
    r = client.post("/api/convert/text-to-pdf", data={"text": "Привет, мир!", "title": "T"})
    assert r.status_code == 200
    assert "Привет" in pt.extract_text(r.data)
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
    r = client.post("/api/tables", files={"file": (io.BytesIO(data), "t.pdf", "application/pdf")},
                    data={"format": "json"})
    assert r.status_code == 200, r.text
    tables = _json(r)["tables"]
    assert tables and tables[0]["table"][0][0] == "Товар", tables
    r = client.post("/api/tables", files={"file": (io.BytesIO(data), "t.pdf", "application/pdf")},
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
