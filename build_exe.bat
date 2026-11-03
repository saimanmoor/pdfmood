@echo off
rem =====================================================================
rem  Build PDFservice.exe (Windows, one-file launcher).
rem  Run this ON Windows:  build_exe.bat
rem  Result: dist\PDFservice.exe  - double-click to start the service
rem          (it will run deploy.ps1 automatically on first launch).
rem =====================================================================
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Installing project dependencies first...
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1" -NoRun
)

echo Installing PyInstaller...
".venv\Scripts\python.exe" -m pip install --upgrade pyinstaller

echo Building PDFservice.exe ...
".venv\Scripts\python.exe" -m PyInstaller --onefile --console ^
    --name PDFservice ^
    --add-data "app;app" ^
    --add-data "deploy.ps1;." ^
    --add-data "requirements.txt;." ^
    --collect-all uvicorn ^
    --noconfirm ^
    launcher\launcher.py

if exist "dist\PDFservice.exe" (
    echo.
    echo ============================================================
    echo   OK!  dist\PDFservice.exe is ready.
    echo   Copy it into the project folder and double-click to run.
    echo   First launch installs everything automatically.
    echo ============================================================
) else (
    echo BUILD FAILED. See messages above.
    exit /b 1
)
endlocal
