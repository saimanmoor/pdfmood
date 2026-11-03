@echo off
rem =====================================================================
rem  PDF Service launcher (Windows). Double-click to start.
rem  - If the service is already installed (.venv exists) -> just starts it
rem  - If not installed yet -> runs deploy.ps1 which downloads and
rem    installs everything (Python, Tesseract OCR + rus, Ghostscript...),
rem    then starts the service.
rem  The web UI opens automatically at http://127.0.0.1:8000/
rem  Close this window (or press Ctrl+C) to stop the service.
rem =====================================================================
setlocal
cd /d "%~dp0"

if "%PORT%"=="" set PORT=8000

if exist ".venv\Scripts\python.exe" goto run

echo.
echo [.venv not found] First-time setup is starting...
echo This will download and install all components automatically.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1" -Port %PORT%
goto end

:run
echo.
echo ============================================================
echo   PDF Service  ^|  Web UI:  http://127.0.0.1:%PORT%/
echo   API docs:      http://127.0.0.1:%PORT%/docs
echo   Stop: close this window or press Ctrl+C
echo ============================================================
echo.

rem Open the browser shortly after the server starts
start "" /min cmd /c "timeout /t 3 >nul & start http://127.0.0.1:%PORT%/"

".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%

:end
endlocal
