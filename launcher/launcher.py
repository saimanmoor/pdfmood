"""
PDFservice.exe launcher (built by build_exe.bat via PyInstaller).

Behaviour:
  * If a project folder with app/main.py is found next to the exe ->
    starts uvicorn from .venv there.
  * If .venv is missing -> runs deploy.ps1 (downloads & installs Python,
    Tesseract OCR + rus, Ghostscript, pip deps), then starts the service.
Pure ASCII output - safe for any Windows console codepage.
"""
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser


def is_frozen():
    return hasattr(sys, "_MEIPASS")


def resource_path(rel):
    base = getattr(sys, "_MEIPASS", None) or exe_dir()
    return os.path.join(base, rel)


def exe_dir():
    return os.path.dirname(os.path.abspath(sys.argv[0]))


def project_root():
    # 1) real project next to the exe
    cand = os.path.join(exe_dir(), "app", "main.py")
    if os.path.exists(cand):
        return exe_dir()
    # 2) unpacked copy in user profile
    return os.path.join(os.path.expanduser("~"), "pdf-service-project")


def unpack_project(root):
    os.makedirs(root, exist_ok=True)
    src_app = resource_path("app")
    dst_app = os.path.join(root, "app")
    if os.path.isdir(src_app):
        if os.path.isdir(dst_app):
            shutil.rmtree(dst_app)
        shutil.copytree(src_app, dst_app)
    for fname in ("deploy.ps1", "requirements.txt"):
        src = resource_path(fname)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(root, fname))


def venv_python(root):
    return os.path.join(root, ".venv", "Scripts", "python.exe")


def run_deploy(root):
    print("[setup] First launch: installing everything automatically...")
    print("[setup] Python + Tesseract OCR (rus) + Ghostscript + pip deps.")
    print("[setup] This takes several minutes, do not close the window.")
    print("[setup] Detailed log: " + os.path.join(root, "deploy.log"))
    # Show progress in a separate console window so the user sees it live
    ps = os.path.join(root, "deploy.ps1")
    log = os.path.join(root, "deploy.log")
    cmd = ('powershell -NoProfile -ExecutionPolicy Bypass -File "%s" -NoRun'
           ' > "%s" 2>&1') % (ps, log)
    subprocess.call(["cmd", "/c", "start", "PDF service setup", cmd], cwd=root)
    # Wait until deploy finishes (venv python appears or timeout ~40 min)
    deadline = time.time() + 40 * 60
    while time.time() < deadline:
        if os.path.exists(venv_python(root)):
            time.sleep(5)  # let deploy.ps1 finish its last steps
            break
        time.sleep(5)
    return os.path.exists(venv_python(root))


def start_server(root, port=8000):
    url = f"http://127.0.0.1:{port}/"
    print("=" * 60)
    print(f"  PDF Service | Web UI: {url}")
    print(f"  API docs:   http://127.0.0.1:{port}/docs")
    print("  Stop: close this window or press Ctrl+C")
    print("=" * 60)
    args = [venv_python(root), "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(port)]
    env = dict(os.environ, PYTHONPATH=root)
    proc = None
    threading.Timer(4.0, lambda: webbrowser.open(url)).start()
    try:
        os.chdir(root)
        proc = subprocess.Popen(args, env=env)
        proc.wait()
    except KeyboardInterrupt:
        if proc:
            proc.terminate()


if __name__ == "__main__":
    root = project_root()
    if not os.path.exists(os.path.join(root, "app", "main.py")) and is_frozen():
        unpack_project(root)
    if not os.path.exists(os.path.join(root, "app", "main.py")):
        print("[error] Project files not found next to the exe.")
        input("Press Enter to exit...")
        sys.exit(1)
    if not os.path.exists(venv_python(root)):
        if not run_deploy(root):
            print("[error] Setup failed. See deploy.log in this folder:")
            try:
                tail = open(os.path.join(root, "deploy.log"),
                            encoding="utf-8", errors="replace").read()[-1500:]
                print(tail)
            except OSError:
                pass
            input("Press Enter to exit...")
            sys.exit(1)
    start_server(root, int(os.environ.get("PORT", "8000")))
