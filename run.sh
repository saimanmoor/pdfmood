#!/usr/bin/env bash
# =====================================================================
#  PDF Service launcher (Linux/macOS).
#  ./run.sh            — установить (если нужно) и запустить сервис
#  PORT=9000 ./run.sh  — запуск на другом порту
# =====================================================================
set -e
cd "$(dirname "$0")"
PORT="${PORT:-8000}"

if [ ! -x ".venv/bin/python" ]; then
    echo "[.venv not found] First-time setup is starting..."
    NO_RUN=1 PORT="$PORT" bash deploy.sh
fi

echo "============================================================"
echo "  PDF Service | Web UI: http://127.0.0.1:${PORT}/"
echo "  API docs:   http://127.0.0.1:${PORT}/docs"
echo "  Stop: Ctrl+C"
echo "============================================================"

# открыть браузер, когда сервер поднимется
( sleep 3
  command -v xdg-open >/dev/null 2>&1 && xdg-open "http://127.0.0.1:${PORT}/" &
  command -v open     >/dev/null 2>&1 && open     "http://127.0.0.1:${PORT}/" &
) >/dev/null 2>&1 &

exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
