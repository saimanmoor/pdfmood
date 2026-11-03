# =============================================================================
#  deploy.ps1 - automated deployment of the local PDF service on Windows
#
#  Does everything by itself:
#    1. Finds Python 3.9+ (or installs it via winget, fallback: python.org)
#    2. Installs Tesseract OCR with Russian language pack (OCR core)
#    3. Downloads utilities needed by OCRmyPDF: Ghostscript, jbig2enc, qpdf
#    4. Creates virtualenv .venv and installs requirements.txt
#    5. Starts the service: http://127.0.0.1:8000  (UI: /, Swagger: /docs)
#
#  Run (normal PowerShell):
#    powershell -ExecutionPolicy Bypass -File .\deploy.ps1
#  Options:
#    .\deploy.ps1 -Port 9000        # another port
#    .\deploy.ps1 -NoRun            # install only, do not start
#
#  NOTE: this file is intentionally pure ASCII so that Windows PowerShell 5.1
#  can parse it regardless of the system codepage. UI messages are in Russian
#  inside the web app itself (app/static/index.html).
# =============================================================================

param(
    [int]$Port = 8000,
    [switch]$NoRun
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = `
    [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

try { chcp 65001 | Out-Null } catch { }

$Root      = Split-Path -Parent $MyInvocation.MyCommand.Path
$ToolsDir  = Join-Path $Root "tools"          # portable utilities go here
$VenvDir   = Join-Path $Root ".venv"

function Log($msg)  { Write-Host "[deploy] $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "[deploy] $msg" -ForegroundColor Yellow }

function Ensure-Dir($p) { if (-not (Test-Path $p)) { New-Item -ItemType Directory -Force -Path $p | Out-Null } }

function Download-File($Url, $Out) {
    Ensure-Dir (Split-Path $Out)
    Log "Downloading: $Url"
    try {
        Invoke-WebRequest -Uri $Url -OutFile $Out -UseBasicParsing
    } catch {
        Warn "Invoke-WebRequest failed, trying curl.exe..."
        & curl.exe -L --retry 3 -o $Out $Url
        if ($LASTEXITCODE -ne 0) { throw "Failed to download $Url" }
    }
}

function Try-Winget($Id) {
    $w = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $w) { return $false }
    Log "winget install $Id ..."
    & winget install --id $Id -e --accept-package-agreements --accept-source-agreements --silent
    return ($LASTEXITCODE -eq 0 -or $LASTEXITCODE -eq -1978335189)  # 0 or already installed
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
            $parts = "$ver".Trim().Split(".")
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
    Warn "Python 3.9+ not found - installing automatically..."
    $ok = Try-Winget "Python.Python.3.12"
    if (-not $ok) {
        $inst = Join-Path $Env:TEMP "python-installer.exe"
        Download-File "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe" $inst
        Log "Running Python installer (silent, current user)..."
        Start-Process -FilePath $inst -ArgumentList "/quiet","InstallAllUsers=0","PrependPath=1","Include_pip=1","Include_test=0" -Wait
    }
    # Refresh PATH of the current session from the registry
    $userPath = (Get-ItemProperty -Path "HKCU:\Environment" -Name Path -ErrorAction SilentlyContinue).Path
    if ($userPath) { $Env:Path = $userPath + ";" + $Env:Path }
    $machinePath  = Join-Path $Env:ProgramFiles "Python312"
    $machinePath2 = Join-Path $Env:ProgramFiles "Python312\Scripts"
    if (Test-Path $machinePath) { $Env:Path += ";$machinePath;$machinePath2" }

    $PythonExe = Find-Python
    if (-not $PythonExe) { throw "Python still not found after install. Restart PowerShell and retry." }
}
Log "Python: $PythonExe"
& $PythonExe --version

# -----------------------------------------------------------------------------
# 2. Tesseract OCR (+ Russian language)
# -----------------------------------------------------------------------------
function Locate-Tesseract {
    $candidates = @()
    $g = Get-Command tesseract -ErrorAction SilentlyContinue
    if ($g) { $candidates += $g.Source }
    $pf86 = ${Env:ProgramFiles(x86)}
    foreach ($rf in @("${Env:ProgramFiles}", "${Env:LOCALAPPDATA}", $pf86)) {
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
    Warn "Tesseract not found - installing automatically..."
    $ok = Try-Winget "UBM.Tesseract"
    if (-not $ok) {
        # Official UB-Mannheim installer (includes rus/eng languages)
        Ensure-Dir $ToolsDir
        $tInst = Join-Path $ToolsDir "tesseract-setup.exe"
        Download-File "https://github.com/UB-Mannheim/tesseract/releases/download/v5.5.0.20241030/tesseract-ocr-w64-setup-5.5.0.20241030.exe" $tInst
        $tDest = Join-Path "${Env:ProgramFiles}" "Tesseract-OCR"
        if (-not (Test-Path "${Env:ProgramFiles}")) { $tDest = Join-Path "${Env:LOCALAPPDATA}" "Tesseract-OCR" }
        Log "Silent Tesseract install into: $tDest ..."
        Start-Process -FilePath $tInst -ArgumentList "/S","/D=$tDest" -Wait
    }
    $Tesseract = Locate-Tesseract
    if (-not $Tesseract) { throw "Failed to install Tesseract OCR." }
}
Log "Tesseract: $Tesseract"
$TessDir = Split-Path $Tesseract
$Env:Path += ";$TessDir"
$Env:TESSDATA_PREFIX = $TessDir

# Make sure Russian language data is present
$langs = & $Tesseract --list-langs 2>$null
$langText = ($langs | Out-String)
if ($langText -notmatch "(?m)^\s*rus\s*$") {
    Warn "Language 'rus' missing - downloading language data..."
    $tessdata = Join-Path $TessDir "tessdata"
    if (-not (Test-Path $tessdata)) { $tessdata = Join-Path $TessDir "share\tessdata" }
    Ensure-Dir $tessdata
    Download-File "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/rus.traineddata" (Join-Path $tessdata "rus.traineddata")
    Download-File "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/eng.traineddata" (Join-Path $tessdata "eng.traineddata")
}

# -----------------------------------------------------------------------------
# 3. Ghostscript / jbig2enc / qpdf (needed by OCRmyPDF)
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
    Warn "Ghostscript not found - installing..."
    $ok = Try-Winget "Artifex.Ghostscript"
    if (-not $ok) {
        $gsInst = Join-Path $ToolsDir "ghostscript-setup.exe"
        Download-File "https://netcologne.dl.sourceforge.net/project/ghostscript/Ghostscript%2010.04.0/gs10.04.0-win64.exe" $gsInst
        Start-Process -FilePath $gsInst -ArgumentList "/S" -Wait
    }
    $pf86b = ${Env:ProgramFiles(x86)}
    foreach ($rf in @("${Env:ProgramFiles}", $pf86b)) {
        if ($rf) {
            Get-ChildItem -Path $rf -Filter "gs*" -Directory -ErrorAction SilentlyContinue | ForEach-Object {
                $bin = Join-Path $_.FullName "bin"
                if (Test-Path (Join-Path $bin "gswin64c.exe")) { $Env:Path += ";$bin" }
            }
        }
    }
}
if (Tool-Present "gswin64c" -or (Tool-Present "gs")) {
    Log "Ghostscript OK."
} else {
    Warn "Ghostscript may not be in PATH yet - restart the terminal if OCR fails."
}

# --- jbig2enc + qpdf (portable builds) ---
if (-not (Tool-Present "jbig2enc")) {
    Warn "Downloading portable jbig2enc/qpdf/leptonica/tiff (Wilkoon Windows build)..."
    $zip = Join-Path $ToolsDir "windows_binaries.zip"
    $urls = @(
        "https://github.com/Wilkoon/OCRmyPDF-Windows-Binaries/releases/latest/download/windows_binaries.zip",
        "https://github.com/Wilkoon/OCRmyPDF-Windows-Binaries/releases/download/1.0.0/windows_binaries.zip"
    )
    $downloaded = $false
    foreach ($u in $urls) {
        try { Download-File $u $zip; $downloaded = $true; break } catch { Warn "Could not download from $u" }
    }
    if ($downloaded) {
        try {
            Expand-Archive -Path $zip -DestinationPath $ToolsBin -Force
        } catch {
            Warn "Expand-Archive failed, trying tar.exe..."
            & tar.exe -xf $zip -C $ToolsBin
        }
        Remove-Item $zip -ErrorAction SilentlyContinue
        # If exes live in subfolders, add those folders to PATH
        Get-ChildItem -Path $ToolsBin -Recurse -Filter "*.exe" -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty DirectoryName -Unique |
            ForEach-Object { $Env:Path += ";$_" }
    } else {
        Warn "Could not download portable utils - OCR will still work (a bit slower)."
    }
}
$Env:Path += ";$ToolsBin"

if (Tool-Present "jbig2enc") { Log "jbig2enc OK." } else { Warn "jbig2enc missing - OCR will be slower (quality unaffected)." }
if (Tool-Present "qpdf")     { Log "qpdf OK." }     else { Warn "qpdf missing - some features may not work." }

# -----------------------------------------------------------------------------
# 4. Virtualenv + python dependencies
# -----------------------------------------------------------------------------
Set-Location $Root

if (-not (Test-Path (Join-Path $VenvDir "Scripts\python.exe"))) {
    Log "Creating virtualenv .venv ..."
    & $PythonExe -m venv $VenvDir
}
$VPy = Join-Path $VenvDir "Scripts\python.exe"

Log "Installing python dependencies from requirements.txt ..."
& $VPy -m pip install --upgrade pip wheel setuptools
& $VPy -m pip install -r "requirements.txt"
if ($LASTEXITCODE -ne 0) { throw "pip install failed." }

# -----------------------------------------------------------------------------
# 5. Self-check of imports
# -----------------------------------------------------------------------------
& $VPy -c @'
import importlib.util, sys
mods = ["fastapi","uvicorn","multipart","ocrmypdf","pdfplumber","pypdf","img2pdf","reportlab"]
bad = [m for m in mods if importlib.util.find_spec(m) is None]
print("OK: all modules present" if not bad else "MISSING: " + ", ".join(bad))
sys.exit(1 if bad else 0)
'@
if ($LASTEXITCODE -ne 0) { throw "Dependency self-check failed." }

# -----------------------------------------------------------------------------
# 6. Start the service
# -----------------------------------------------------------------------------
if ($NoRun) {
    Log "Installation finished (-NoRun). Start manually:"
    Log "  .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port $Port"
    exit 0
}

Log "Starting PDF service at http://127.0.0.1:$Port"
Log "  Web UI: http://127.0.0.1:$Port/   API docs: http://127.0.0.1:$Port/docs"
Log "  Stop: Ctrl+C"
& $VPy -m uvicorn app.main:app --host 127.0.0.1 --port $Port
