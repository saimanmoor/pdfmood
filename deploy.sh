#!/usr/bin/env bash
# =============================================================================
#  deploy.sh — автоматическое развёртывание локального PDF-сервиса на Linux/macOS
#
#  Делает всё сама:
#    1. Проверяет/устанавливает Python 3.9+ (если нужно)
#    2. Устанавливает системные зависимости:
#         - Tesseract OCR (rus+eng языковые пакеты, ~уровень ABBYY FineReader)
#         - Ghostscript, JBIG2, QPDF (нужны для OCRmyPDF)
#         - libmagic
#       Пакетный менеджер определяется автоматически:
#       apt / dnf / yum / pacman / zypper / brew
#    3. Создаёт виртуальное окружение .venv и ставит requirements.txt
#    4. Запускает сервис uvicorn на http://127.0.0.1:8000
#       (веб-интерфейс — /, Swagger — /docs)
#
#  Запуск:   bash deploy.sh            (или: chmod +x deploy.sh && ./deploy.sh)
#  Опции:   PORT=9000 bash deploy.sh   — другой порт
#           NO_RUN=1 bash deploy.sh    — только установить, не запускать
# =============================================================================
set -euo pipefail

PORT="${PORT:-8000}"
NO_RUN="${NO_RUN:-0}"
HOST="127.0.0.1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

log()  { echo -e "\033[1;32m[deploy]\033[0m $*"; }
warn() { echo -e "\033[1;33m[deploy]\033[0m $*"; }
die()  { echo -e "\033[1;31m[deploy]\033[0m $*" >&2; exit 1; }

# -----------------------------------------------------------------------------
# 0. Привилегии для установки системных пакетов
# -----------------------------------------------------------------------------
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  else
    warn "Не root и sudo нет — пропустим установку системных пакетов."
  fi
fi

have() { command -v "$1" >/dev/null 2>&1; }

# -----------------------------------------------------------------------------
# 1. Поиск подходящего Python
# -----------------------------------------------------------------------------
find_python() {
  for p in python3.12 python3.11 python3.10 python3.9 python3 python; do
    if have "$p"; then
      if "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then
        echo "$p"; return 0
      fi
    fi
  done
  return 1
}

PY="$(find_python || true)"

# -----------------------------------------------------------------------------
# 2. Системные зависимости в зависимости от дистрибутива
# -----------------------------------------------------------------------------
PKG_INSTALLED=0

install_with_apt() {
  $SUDO apt-get update -y
  DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y \
    python3 python3-venv python3-pip \
    tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng \
    ghostscript jbig2enc qpdf libmagic1 unpaper curl
  PKG_INSTALLED=1
}

install_with_dnf() {
  $SUDO dnf install -y \
    python3 python3-pip \
    tesseract tesseract-langpack-rus tesseract-langpack-eng \
    ghostscript jbig2enc qpdf file-devel libcurl-devel
  PKG_INSTALLED=1
}

install_with_yum() {
  $SUDO yum install -y epel-release || true
  $SUDO yum install -y \
    python3 python3-pip \
    tesseract tesseract-langpack-rus \
    ghostscript jbig2enc qpdf file-devel
  PKG_INSTALLED=1
}

install_with_pacman() {
  $SUDO pacman -Sy --noconfirm \
    python python-pip \
    tesseract tesseract-data-rus tesseract-data-eng \
    ghostscript jbig2enc qpdf file
  PKG_INSTALLED=1
}

install_with_zypper() {
  $SUDO zypper --non-interactive install \
    python3 python3-pip \
    tesseract-ocr tesseract-ocr_rus \
    ghostscript jbig2enc qpdf libmagic1 unpaper
  PKG_INSTALLED=1
}

install_with_brew() {
  brew install python3 tesseract ghostscript jbig2enc qpdf libmagic unpaper || true
  # Языковые данные Tesseract через brew уже включены в формулу tesseract
  PKG_INSTALLED=1
}

log "Определяем пакетный менеджер и ставим системные зависимости..."
if have apt-get;       then install_with_apt     || warn "apt установил не всё — пробуем продолжить."
elif have dnf;        then install_with_dnf      || warn "dnf: часть пакетов могла не установиться."
elif have yum;        then install_with_yum      || warn "yum: часть пакетов могла не установиться."
elif have pacman;     then install_with_pacman   || warn "pacman: часть пакетов могла не установиться."
elif have zypper;     then install_with_zypper   || warn "zypper: часть пакетов могла не установиться."
elif have brew;       then install_with_brew     || warn "brew: часть пакетов могла не установиться."
else warn "Пакетный менеджер не найден — системные пакеты надо поставить вручную:"
     warn "  tesseract-ocr (+rus), ghostscript, jbig2enc, qpdf, libmagic, python3-venv"
fi

# Если Python так и не нашли, а пакеты ставились через apt — попробуем ещё раз
if [ -z "$PY" ]; then
  PY="$(find_python || true)"
fi
[ -n "$PY" ] || die "Python 3.9+ не найден. Установите Python и повторите запуск."
log "Используем Python: $($PY --version 2>&1) ($(command -v $PY))"

# -----------------------------------------------------------------------------
# 3. Проверка Tesseract (OCR-ядро уровня FineReader)
# -----------------------------------------------------------------------------
if have tesseract; then
  log "Tesseract: $(tesseract --version 2>&1 | head -1)"
  LANGS="$(tesseract --list-langs 2>/dev/null || true)"
  echo "$LANGS" | grep -qi "rus" || warn "Русский языковой пакет Tesseract не найден — OCR для русского может не работать."
else
  warn "tesseract не найден в PATH! OCR работать не будет."
  warn "Установите вручную: https://github.com/tesseract-ocr/tesseract"
fi

# -----------------------------------------------------------------------------
# 4. Виртуальное окружение + python-зависимости
# -----------------------------------------------------------------------------
VENV=".venv"
if [ ! -d "$VENV" ]; then
  log "Создаём виртуальное окружение $VENV ..."
  $PY -m venv "$VENV" || die "Не удалось создать venv (попробуйте установить python3-venv)."
fi
VPY="$VENV/bin/python"

log "Обновляем pip и ставим зависимости из requirements.txt ..."
"$VPY" -m pip install --upgrade pip wheel setuptools
"$VPY" -m pip install -r requirements.txt

# -----------------------------------------------------------------------------
# 5. Быстрая самопроверка импортов
# -----------------------------------------------------------------------------
"$VPY" - <<'EOF'
import importlib.util, sys
mods = ["fastapi", "uvicorn", "ocrmypdf", "pdfplumber", "pypdf", "img2pdf", "reportlab", "magic"]
bad = [m for m in mods if importlib.util.find_spec(m) is None]
if bad:
    print("Не хватает модулей:", ", ".join(bad)); sys.exit(1)
print("Все python-модули на месте.")
EOF

# -----------------------------------------------------------------------------
# 6. Запуск сервиса
# -----------------------------------------------------------------------------
if [ "$NO_RUN" = "1" ]; then
  log "Установка завершена (NO_RUN=1). Запуск: $VPY -m uvicorn app.main:app --host $HOST --port $PORT"
  exit 0
fi

log "Запускаем PDF-сервис на http://$HOST:$PORT"
log "  Веб-интерфейс: http://$HOST:$PORT/     API-документация: http://$HOST:$PORT/docs"
log "  Остановка: Ctrl+C"
exec "$VPY" -m uvicorn app.main:app --host "$HOST" --port "$PORT"
