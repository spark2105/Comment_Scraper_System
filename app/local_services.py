from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_LOG_DIR = PROJECT_ROOT / "data" / "logs"
CRAWLER_URL = "http://127.0.0.1:8000"
OLLAMA_URL = "http://127.0.0.1:11434"
OLLAMA_MODEL = "llama3.2"

_crawler_process: subprocess.Popen[Any] | None = None
_ollama_process: subprocess.Popen[Any] | None = None
_log_handles: list[Any] = []


def _is_available(url: str, path: str, timeout: float = 1.5) -> bool:
    try:
        response = httpx.get(f"{url}{path}", timeout=timeout)
        return response.is_success
    except httpx.HTTPError:
        return False


def _wait_for_service(url: str, path: str, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_available(url, path):
            return True
        time.sleep(0.25)
    return False


def _ollama_status() -> tuple[bool, str]:
    try:
        response = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=2)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return False, "Ollama is not responding"

    models = payload.get("models", []) if isinstance(payload, dict) else []
    available = any(
        isinstance(model, dict)
        and (
            model.get("name") == OLLAMA_MODEL
            or str(model.get("name", "")).split(":", 1)[0] == OLLAMA_MODEL
        )
        for model in models
    )
    if available:
        return True, f"Ollama ready ({OLLAMA_MODEL})"
    return False, f"Ollama is running, but model {OLLAMA_MODEL} is missing; run app.ensure_model"


def _open_service_log(name: str) -> Any:
    DATA_LOG_DIR.mkdir(parents=True, exist_ok=True)
    handle = (DATA_LOG_DIR / name).open("a", encoding="utf-8")
    _log_handles.append(handle)
    return handle


def _start_crawler() -> tuple[bool, str]:
    global _crawler_process
    if _is_available(CRAWLER_URL, "/health"):
        return True, "Crawler already running"
    if _crawler_process is not None and _crawler_process.poll() is None:
        return _wait_for_service(CRAWLER_URL, "/health"), "Crawler startup in progress"

    log_handle = _open_service_log("crawler_service.log")
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    _crawler_process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.api:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ],
        cwd=PROJECT_ROOT,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=creation_flags,
    )
    if _wait_for_service(CRAWLER_URL, "/health"):
        return True, "Crawler started automatically"
    return False, "Crawler did not become ready; see data/logs/crawler_service.log"


def _find_ollama_executable() -> str | None:
    executable = shutil.which("ollama")
    if executable:
        return executable
    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        candidate = Path(local_app_data) / "Programs" / "Ollama" / "ollama.exe"
        if candidate.exists():
            return str(candidate)
    return None


def _start_ollama() -> tuple[bool, str]:
    global _ollama_process
    if _is_available(OLLAMA_URL, "/api/tags"):
        return _ollama_status()
    if _ollama_process is not None and _ollama_process.poll() is None:
        ready = _wait_for_service(OLLAMA_URL, "/api/tags")
        return ready, "Ollama startup in progress" if ready else "Ollama did not become ready"

    executable = _find_ollama_executable()
    if not executable:
        return False, "Ollama is not running and was not found locally"

    log_handle = _open_service_log("ollama_service.log")
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    _ollama_process = subprocess.Popen(
        [executable, "serve"],
        cwd=PROJECT_ROOT,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=creation_flags,
    )
    if _wait_for_service(OLLAMA_URL, "/api/tags"):
        return _ollama_status()
    return False, "Ollama did not become ready; see data/logs/ollama_service.log"


def ensure_local_services() -> dict[str, tuple[bool, str]]:
    return {
        "crawler": _start_crawler(),
        "ollama": _start_ollama(),
    }


def _stop_owned_processes() -> None:
    for process in (_crawler_process, _ollama_process):
        if process is not None and process.poll() is None:
            process.terminate()
    for handle in _log_handles:
        try:
            handle.close()
        except OSError:
            pass


atexit.register(_stop_owned_processes)
