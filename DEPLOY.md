# Local PDF Service — развёртывание в один запуск

Локальный веб-сервис для полноценной работы с PDF: OCR сканов на уровне
ABBYY FineReader (Tesseract 5 + OCRmyPDF, rus/eng, 300 dpi, deskew),
извлечение текста/таблиц/изображений, merge/split/rotate/watermark/encrypt/compress,
конвертации. Веб-интерфейс — `/`, Swagger — `/docs`.

## Быстрый старт (всё скачивается и ставится само)

### Linux / macOS
```bash
bash deploy.sh
```
Скрипт сам определит пакетный менеджер (apt / dnf / yum / pacman / zypper / brew),
установит Python (если нужен), Tesseract OCR с русским языком, Ghostscript,
qpdf, jbig2enc, создаст `.venv`, поставит pip-зависимости и запустит сервис
на **http://127.0.0.1:8000**.

Опции: `PORT=9000 bash deploy.sh` — другой порт; `NO_RUN=1 bash deploy.sh` — только установка.

### Windows (PowerShell)
```powershell
powershell -ExecutionPolicy Bypass -File .\deploy.ps1
```
Скрипт сам:
1. найдёт или скачает и установит Python 3.12 (winget → официальный установщик);
2. установит Tesseract OCR 5 с русским языком (winget → тихий NSIS-установщик UB-Mannheim;
   при необходимости докачает `rus.traineddata`/`eng.traineddata`);
3. установит Ghostscript и скачает переносные jbig2enc/qpdf (нужны OCRmyPDF);
4. создаст `.venv`, поставит зависимости из `requirements.txt`;
5. запустит сервис на **http://127.0.0.1:8000**.

Опции: `.\deploy.ps1 -Port 9000` — другой порт; `.\deploy.ps1 -NoRun` — только установка.

## После запуска
- Веб-интерфейс: http://127.0.0.1:8000/
- API-документация: http://127.0.0.1:8000/docs
- Проверка окружения: http://127.0.0.1:8000/api/health (языки Tesseract, версии)

## Ручной запуск (если окружение уже установлено)
```bash
# Linux/macOS
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# Windows
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Тесты
```bash
.venv/bin/python -m pytest tests -q        # Linux/macOS
.venv\Scripts\python.exe -m pytest tests -q  # Windows
```
