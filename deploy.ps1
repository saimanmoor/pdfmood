# =============================================================================
#  deploy.ps1 — автоматическое развёртывание локального PDF-сервиса на Windows
#
#  Делает всё сама:
#    1. Ищет Python 3.9+ (или скачивает и устанавливает его через winget,
#       а если winget нет — через официальный установитель python.org)
#    2. Устанавливает Tesseract OCR (с русским языковым пакетом) — ядро OCR
#       уровня ABBYY FineReader (winget -> прямой загрузчик UB-Mannheim)
#    3. Скачивает утилиты, нужные OCRmyPDF: Ghostscript, jbig2enc, qpdf
#    4. Создаёт виртуальное окружение .venv, ставит requirements.txt
#    5. Прописывает всё в PATH текущей сессии и запускает сервис:
#       http://127.0.0.1:8000  (веб-интерфейс — /, Swagger — /docs)
#
#  Запуск (PowerShell от обычного пользователя):
#    powershell -ExecutionPolicy Bypass -File .\deploy.ps1
#  Опции:
#    .\deploy.ps1 -Port 9000        # другой порт
#    .\deploy.ps1 -NoRun            # только установить, не запускать
# =============================================================================

param(
    [int]$Port = 8000,
    [switch]$NoRun
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = `
    [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Root      = Split-Path -Parent $MyInvocation.MyCommand.Path
$ToolsDir  = Join-Path $Root "tools"          # сюда кладём переносные утилиты
$VenvDir   = Join-Path $Root ".venv"

function Log($msg)  { Write-Host "[deploy] $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "[deploy] $msg" -ForegroundColor Yellow }

function Ensure-Dir($p) { if (-not (Test-Path $p)) { New-Item -ItemType Directory -Force -Path $p | Out-Null } }

function Download-File($Url, $Out) {
    Ensure-Dir (Split-Path $Out)
    Log "Скачиваю: $Url"
    try {
        Invoke-WebRequest -Uri $Url -OutFile $Out -UseBasicParsing
    } catch {
        Warn "Invoke-WebRequest не смог, пробую curl.exe..."
        & curl.exe -L --retry 3 -o $Out $Url
        if ($LASTEXITCODE -ne 0) { throw "Не удалось скачать $Url" }
    }
}

function Try-Winget($Id) {
    $w = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $w) { return $false }
    Log "winget install $Id ..."
    & winget install --id $Id -e --accept-package-agreements --accept-source-agreements --silent
    return ($LASTEXITCODE -eq 0 -or $LASTEXITCODE -eq -1978335189)  # уже установлен
}

# -----------------------------------------------------------------------------
# 1. Python 3.9+
# -----------------------------------------------------------------------------
function Find-Python {
    foreach ($cand in @("python", "py")) {
        $g = Get-Command $cand -ErrorAction SilentlyContinue
        if (-not $g) { continue }
        $args_ = if ($cand -eq "py") { @("-3") } else { @() }
        try {
            $ver = & $cand $args_ -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $ver) { continue }
            $parts = $ver.Split(".")
            if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 9) {
                $exe = (& $cand $args_ -c "import sys;print(sys.executable)").Trim()
                return $exe
            }
        } catch { }
    }
    return $null
}

$PythonExe = Find-Python
if (-not $PythonExe) {
    Warn "Python 3.9+ не найден — устанавливаем автоматически..."
    $ok = Try-Winget "Python.Python.3.12"
    if (-not $ok) {
        $inst = Join-Path $Env:TEMP "python-installer.exe"
        Download-File "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe" $inst
        Log "Запускаю установщик Python (тихо, для текущего пользователя)..."
        Start-Process -FilePath $inst -ArgumentList "/quiet","InstallAllUsers=0","PrependPath=1","Include_pip=1","Include_test=0" -Wait
    }
    # Обновим PATH текущей сессии из реестра
    $userPath = (Get-ItemProperty -Path "HKCU:\Environment" -Name Path -ErrorAction SilentlyContinue).Path
    if ($userPath) { $Env:Path = $userPath + ";" + $Env:Path }
    $machinePath = (Join-Path $Env:ProgramFiles "Python312"); $machinePath2 = (Join-Path $Env:ProgramFiles "Python312\Scripts")
    if (Test-Path $machinePath) { $Env:Path += ";$machinePath;$machinePath2" }

    $PythonExe = Find-Python
    if (-not $PythonExe) { throw "Python так и не найден после установки. Перезапустите PowerShell и повторите." }
}
Log "Python: $PythonExe"
& $PythonExe --version

# -----------------------------------------------------------------------------
# 2. Tesseract OCR (+ русский язык)
# -----------------------------------------------------------------------------
function Locate-Tesseract {
    $candidates = @()
    $g = Get-Command tesseract -ErrorAction SilentlyContinue
    if ($g) { $candidates += $g.Source }
    foreach ($rf in @("${Env:ProgramFiles}", "${Env:ProgramFiles(x86)}", "$Env:LOCALAPPDATA")) {
        if ($rf) {
            $candidates += Join-Path $rf "Tesseract-OCR\tesseract.exe"
            $candidates += Join-Path $rf "Programs\Tesseract-OCR\tesseract.exe"
        }
    }
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    return $null
}

$Tesseract = Locate-Tesseract
if (-not $Tesseract) {
    Warn "Tesseract не найден — устанавливаем автоматически..."
    $ok = Try-Winget "UBM.Tesseract"
    if (-not $ok) {
        # Официальный установщик UB-Mannheim (включает rus/eng языки)
        Ensure-Dir $ToolsDir
        $tInst = Join-Path $ToolsDir "tesseract-setup.exe"
        Download-File "https://github.com/UB-Mannheim/tesseract/releases/download/v5.5.0.20241030/tesseract-ocr-w64-setup-5.5.0.20241030.exe" $tInst
        $tDest = Join-Path ${Env:ProgramFiles} "Tesseract-OCR"
        if (-not (Test-Path $tDest)) { $tDest = Join-Path $Env:LOCALAPPDATA "Tesseract-OCR" }
        Log "Тихая установка Tesseract в $tDest ..."
        Start-Process -FilePath $tInst -ArgumentList "/S","/D=$tDest" -Wait
    }
    $Tesseract = Locate-Tesseract
    if (-not $Tesseract) { throw "Не удалось установить Tesseract OCR." }
}
Log "Tesseract: $Tesseract"
$TessDir = Split-Path $Tesseract
$Env:Path += ";$TessDir"
$Env:TESSDATA_PREFIX = $TessDir

# Проверка русского языка
$langs = & $Tesseract --list-langs 2>$null
if (-not ($langs -match "(?i)^rus`$")) {
    Warn "Язык 'rus' не найден — скачиваем языковые данные..."
    $tessdata = Join-Path $TessDir "tessdata"
    if (-not (Test-Path $tessdata)) { $tessdata = Join-Path $TessDir "share\tessdata" }
    Ensure-Dir $tessdata
    Download-File "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/rus.traineddata" (Join-Path $tessdata "rus.traineddata")
    Download-File "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/eng.traineddata" (Join-Path $tessdata "eng.traineddata")
}

# -----------------------------------------------------------------------------
# 3. Ghostscript / jbig2enc / qpdf (нужны OCRmyPDF)
# -----------------------------------------------------------------------------
Ensure-Dir $ToolsDir
$ToolsBin = Join-Path $ToolsDir "bin"
Ensure-Dir $ToolsBin

function Tool-Present($name) {
    if (Get-Command $name -ErrorAction SilentlyContinue) { return $true }
    if (Test-Path (Join-Path $ToolsBin "$name.exe")) { return $true }
    return $false
}

# --- Ghostscript ---
if (-not (Tool-Present "gswin64c") -and -not (Tool-Present "gs")) {
    Warn "Ghostscript не найден — устанавливаем..."
    $ok = Try-Winget "Artifex.Ghostscript"
    if (-not $ok) {
        $gsInst = Join-Path $ToolsDir "ghostscript-setup.exe"
        Download-File "https://netcologne.dl.sourceforge.net/project/ghostscript/Ghostscript%2010.04.0/gs10.04.0-win64.exe" $gsInst
        Start-Process -FilePath $gsInst -ArgumentList "/S" -Wait
    }
    foreach ($rf in @("${Env:ProgramFiles}", "${Env:ProgramFiles(x86)}")) {
        if ($rf) {
            Get-ChildItem -Path $rf -Filter "gs*" -Directory -ErrorAction SilentlyContinue | ForEach-Object {
                $bin = Join-Path $_.FullName "bin"
                if (Test-Path (Join-Path $bin "gswin64c.exe")) { $Env:Path += ";$bin" }
            }
        }
    }
}
if (Tool-Present "gswin64c" -or (Get-Command gswin64c -ErrorAction SilentlyContinue)) {
    Log "Ghostscript OK."
} else { Warn "Ghostscript может быть недоступен в PATH — перезапустите терминал при проблемах с OCR." }

# --- jbig2enc + qpdf (переносимые сборки) ---
if (-not (Tool-Present "jbig2enc")) {
    Warn "Скачиваю переносные jbig2enc/qpdf/leptonica/tiff (сборка @Wilkoon для Windows)..."
    $zip = Join-Path $ToolsDir "windows_binaries.zip"
    $urls = @(
        "https://github.com/Wilkoon/OCRmyPDF-Windows-Binaries/releases/latest/download/windows_binaries.zip",
        "https://github.com/Wilkoon/OCRmyPDF-Windows-Binaries/releases/download/1.0.0/windows_binaries.zip"
    )
    $downloaded = $false
    foreach ($u in $urls) {
        try { Download-File $u $zip; $downloaded = $true; break } catch { Warn "Не получилось с $u" }
    }
    if ($downloaded) {
        try {
            Expand-Archive -Path $zip -DestinationPath $ToolsBin -Force
        } catch {
            Warn "Expand-Archive не справился, пробую tar.exe..."
            & tar.exe -xf $zip -C $ToolsBin
        }
        Remove-Item $zip -ErrorAction SilentlyContinue
        # Если внутри есть подпапка с .exe — добавим её в PATH
        Get-ChildItem -Path $ToolsBin -Recurse -Filter "*.exe" -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty DirectoryName -Unique |
            ForEach-Object { $Env:Path += ";$_" }
    } else {
        Warn "Переносные утилиты скачать не удалось — OCR всё равно будет работать (чуть медленнее)."
    }
}
$Env:Path += ";$ToolsBin"

if (Tool-Present "jbig2enc") { Log "jbig2enc OK." }  else { Warn "jbig2enc не найден — OCR будет медленнее (качество не пострадает)." }
if (Tool-Present "qpdf")     { Log "qpdf OK." }      else { Warn "qpdf не найден — часть функций может не работать." }

# -----------------------------------------------------------------------------
# 4. Виртуальное окружение + python-зависимости
# -----------------------------------------------------------------------------
Set-Location $Root

if (-not (Test-Path (Join-Path $VenvDir "Scripts\python.exe"))) {
    Log "Создаю виртуальное окружение .venv ..."
    & $PythonExe -m venv $VenvDir
}
$VPy = Join-Path $VenvDir "Scripts\python.exe"

Log "Ставлю python-зависимости из requirements.txt ..."
& $VPy -m pip install --upgrade pip wheel setuptools
& $VPy -m pip install -r "requirements.txt"
if ($LASTEXITCODE -ne 0) { throw "pip install завершился с ошибкой." }

# -----------------------------------------------------------------------------
# 5. Самопроверка импортов
# -----------------------------------------------------------------------------
& $VPy -c @"
import importlib.util, sys
mods = ['fastapi','uvicorn','multipart','ocrmypdf','pdfplumber','pypdf','img2pdf','reportlab']
bad = [m for m in mods if importlib.util.find_spec(m) is None]
print('OK: все модули на месте' if not bad else 'НЕ ХВАТАЕТ: ' + ', '.join(bad))
sys.exit(1 if bad else 0)
"@
if ($LASTEXITCODE -ne 0) { throw "Проверка зависимостей не пройдена." }

# -----------------------------------------------------------------------------
# 6. Запуск сервиса
# -----------------------------------------------------------------------------
if ($NoRun) {
    Log "Установка завершена (-NoRun). Запуск: .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port $Port"
    return
}

Log "Запускаю PDF-сервис на http://127.0.0.1:$Port"
Log "  Веб-интерфейс: http://127.0.0.1:$Port/     API-документация: http://127.0.0.1:$Port/docs"
Log "  Остановка: Ctrl+C"
& $VPy -m uvicorn app.main:app --host 127.0.0.1 --port $Port
