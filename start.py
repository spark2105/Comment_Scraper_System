from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
FRONTEND_URL = "http://127.0.0.1:8503"
FRONTEND_COMMAND = [
    "-m",
    "streamlit",
    "run",
    "app/frontend.py",
    "--server.headless=true",
    "--server.port=8503",
]


def _venv_python() -> Path:
    candidate = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    return candidate if candidate.exists() else Path(sys.executable)


def _restart_with_venv() -> None:
    interpreter = _venv_python().resolve()
    current = Path(sys.executable).resolve()
    if interpreter == current:
        return
    os.execv(str(interpreter), [str(interpreter), str(Path(__file__).resolve()), *sys.argv[1:]])


def _url_is_available() -> bool:
    try:
        with urllib.request.urlopen(FRONTEND_URL, timeout=1) as response:
            return 200 <= response.status < 500
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _wait_for_frontend(process: subprocess.Popen[str], timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        if _url_is_available():
            return True
        time.sleep(0.5)
    return False


def _ensure_components() -> None:
    from app.local_services import ensure_local_services

    services = ensure_local_services()
    for name, (available, message) in services.items():
        print(f"{name.capitalize()}: {message}", flush=True)
        if name == "crawler" and not available:
            raise RuntimeError(message)

    model_result = subprocess.run(
        [sys.executable, "-m", "app.ensure_model"],
        cwd=PROJECT_ROOT,
        check=False,
    )
    if model_result.returncode != 0:
        raise RuntimeError("Ollama model llama3.2 could not be prepared.")

    services = ensure_local_services()
    if not all(available for available, _ in services.values()):
        details = "; ".join(f"{name}: {message}" for name, (_, message) in services.items())
        raise RuntimeError(f"A local service is not ready: {details}")


def main() -> int:
    _restart_with_venv()
    os.chdir(PROJECT_ROOT)

    if _url_is_available():
        print(f"Frontend is already running at {FRONTEND_URL}", flush=True)
        webbrowser.open_new(FRONTEND_URL)
        return 0

    try:
        _ensure_components()
    except (OSError, RuntimeError) as exc:
        print(f"Startup failed: {exc}", file=sys.stderr, flush=True)
        return 1

    print("Starting Streamlit frontend ...", flush=True)
    process = subprocess.Popen(
        [sys.executable, *FRONTEND_COMMAND],
        cwd=PROJECT_ROOT,
        text=True,
    )
    if not _wait_for_frontend(process):
        if process.poll() is not None and _url_is_available():
            print(f"A frontend is already running at {FRONTEND_URL}", flush=True)
            webbrowser.open_new(FRONTEND_URL)
            return 0
        print("Streamlit did not become ready within 60 seconds.", file=sys.stderr, flush=True)
        if process.poll() is None:
            process.terminate()
        return 1

    print(f"Opening {FRONTEND_URL}", flush=True)
    webbrowser.open_new(FRONTEND_URL)
    try:
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
