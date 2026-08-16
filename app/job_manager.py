from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .scraper import DescriptionCleaningAbort, ScrapeCancelled, ScrapeConfig, Scraper


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = PROJECT_ROOT / "data" / "logs"
JOB_DIR = PROJECT_ROOT / "data" / "jobs"
DATABASE_PATH = str(PROJECT_ROOT / "data" / "sql" / "comment_scraper.sqlite3")


@dataclass
class JobState:
    job_id: str
    status: str = "starting"
    stage: str = "initializing"
    current_message: str = ""
    started_at: str = field(default_factory=lambda: utc_now())
    finished_at: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    logs: list[dict[str, str]] = field(default_factory=list)
    config_summary: dict[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


class JobManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._current: JobState | None = None
        self._history: dict[str, JobState] = self._load_history()
        self._pause_requested = threading.Event()
        self._cancel_requested = threading.Event()

    def create_and_start(self, *, api_key: str, config: ScrapeConfig) -> JobState:
        with self._lock:
            if self._current and self._current.status in {"starting", "running", "paused"}:
                raise RuntimeError("Another scraping job is already running.")
            if not api_key.strip():
                raise ValueError("A YouTube API key is required.")
            job_id = uuid.uuid4().hex
            self._pause_requested.clear()
            self._cancel_requested.clear()
            state = JobState(
                job_id=job_id,
                config_summary={
                    "channels": config.channels,
                    "video_urls": config.video_urls,
                    "max_videos_per_channel": config.max_videos_per_channel,
                    "max_comment_threads_per_video": config.max_comment_threads_per_video,
                    "include_replies": config.include_replies,
                },
            )
            self._current = state
            self._history[job_id] = state
            self._append_log_locked(state, "info", "Scraping job created.")
            thread = threading.Thread(
                target=self._run,
                args=(state, api_key, config),
                name=f"scrape-{job_id[:8]}",
                daemon=True,
            )
            thread.start()
            return self._copy_state_locked(state)

    def current(self) -> JobState | None:
        with self._lock:
            return self._copy_state_locked(self._current) if self._current else None

    def list_jobs(self) -> list[JobState]:
        with self._lock:
            states = sorted(self._history.values(), key=lambda item: item.started_at, reverse=True)
            return [self._copy_state_locked(state) for state in states if state is not None]

    def get(self, job_id: str) -> JobState:
        with self._lock:
            return self._copy_state_locked(self._require_history_locked(job_id))

    def delete(self, job_id: str) -> None:
        with self._lock:
            state = self._require_history_locked(job_id)
            if state.status in {"starting", "running", "paused"}:
                raise RuntimeError("Active jobs cannot be deleted. Cancel the job first.")
            self._history.pop(job_id, None)
            if self._current and self._current.job_id == job_id:
                self._current = None
            self._job_path(job_id).unlink(missing_ok=True)
            (LOG_DIR / f"{job_id}.log").unlink(missing_ok=True)

    def pause(self, job_id: str) -> JobState:
        with self._lock:
            state = self._require_current_locked(job_id)
            if state.status == "running":
                self._pause_requested.set()
                state.status = "paused"
                self._append_log_locked(state, "info", "Pause requested; the current request will finish first.")
            return self._copy_state_locked(state)

    def resume(self, job_id: str) -> JobState:
        with self._lock:
            state = self._require_current_locked(job_id)
            if state.status == "paused":
                self._pause_requested.clear()
                state.status = "running"
                self._append_log_locked(state, "info", "Scraping job resumed.")
            return self._copy_state_locked(state)

    def cancel(self, job_id: str) -> JobState:
        with self._lock:
            state = self._require_current_locked(job_id)
            if state.status in {"starting", "running", "paused"}:
                self._cancel_requested.set()
                self._pause_requested.clear()
                self._append_log_locked(state, "warning", "Cancellation requested; the job will stop at the next safe checkpoint.")
            return self._copy_state_locked(state)

    def logs(self, job_id: str) -> list[dict[str, str]]:
        with self._lock:
            state = self._require_history_locked(job_id)
            return list(state.logs)

    def _run(self, state: JobState, api_key: str, config: ScrapeConfig) -> None:
        try:
            with self._lock:
                state.status = "running"
                state.stage = "crawling"
                self._append_log_locked(state, "info", "Crawler started.")

            scraper = Scraper(
                api_key=api_key,
                config=config,
                log=lambda level, message: self._log(state, level, message),
                checkpoint=self._checkpoint,
                database_path=DATABASE_PATH,
            )
            summary = scraper.run()
            with self._lock:
                state.status = "completed"
                state.stage = "complete"
                state.summary = summary
                state.finished_at = utc_now()
                self._append_log_locked(state, "info", "Scraping job completed successfully.")
        except ScrapeCancelled:
            with self._lock:
                state.status = "cancelled"
                state.stage = "cancelled"
                state.finished_at = utc_now()
                self._append_log_locked(state, "warning", "Scraping job cancelled. Data saved so far remains available.")
        except DescriptionCleaningAbort as exc:
            with self._lock:
                state.status = "failed"
                state.stage = "description_cleaning"
                state.error = str(exc)
                state.finished_at = utc_now()
                self._append_log_locked(state, "error", f"Job stopped because description cleaning failed: {exc}")
        except Exception as exc:  # pragma: no cover - safety boundary for worker threads
            with self._lock:
                state.status = "failed"
                state.stage = "failed"
                state.error = str(exc)
                state.finished_at = utc_now()
                self._append_log_locked(state, "error", f"Unexpected error: {exc}")

    def _checkpoint(self) -> None:
        while self._pause_requested.is_set():
            if self._cancel_requested.is_set():
                raise ScrapeCancelled()
            time.sleep(0.25)
        if self._cancel_requested.is_set():
            raise ScrapeCancelled()

    def _log(self, state: JobState, level: str, message: str) -> None:
        with self._lock:
            self._append_log_locked(state, level, message)

    def _append_log_locked(self, state: JobState, level: str, message: str) -> None:
        timestamp = utc_now()
        entry = {"timestamp": timestamp, "level": level, "message": message}
        state.logs.append(entry)
        state.current_message = message
        if level == "error":
            state.stage = "error"
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOG_DIR / f"{state.job_id}.log"
        with log_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._persist_locked(state)


    def _require_history_locked(self, job_id: str) -> JobState:
        if job_id not in self._history:
            raise KeyError(f"Unknown scraping job: {job_id}")
        return self._history[job_id]

    @staticmethod
    def _job_path(job_id: str) -> Path:
        return JOB_DIR / f"{job_id}.json"

    def _persist_locked(self, state: JobState) -> None:
        JOB_DIR.mkdir(parents=True, exist_ok=True)
        self._job_path(state.job_id).write_text(
            json.dumps(state.public_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _require_current_locked(self, job_id: str) -> JobState:
        if not self._current or self._current.job_id != job_id:
            raise KeyError(f"Unknown scraping job: {job_id}")
        return self._current

    def _load_history(self) -> dict[str, JobState]:
        history: dict[str, JobState] = {}
        if not JOB_DIR.exists():
            return history
        for path in JOB_DIR.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                state = JobState(
                    job_id=str(data["job_id"]),
                    status=str(data.get("status", "failed")),
                    stage=str(data.get("stage", "failed")),
                    current_message=str(data.get("current_message", "")),
                    started_at=str(data.get("started_at", utc_now())),
                    finished_at=data.get("finished_at"),
                    summary=dict(data.get("summary") or {}),
                    error=data.get("error"),
                    logs=list(data.get("logs") or []),
                    config_summary=dict(data.get("config_summary") or {}),
                )
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if state.status in {"starting", "running", "paused"}:
                state.status = "failed"
                state.stage = "failed"
                state.error = "Job was interrupted because the crawler service was restarted."
                state.finished_at = utc_now()
                state.logs.append(
                    {
                        "timestamp": state.finished_at,
                        "level": "error",
                        "message": state.error,
                    }
                )
                self._persist_locked(state)
            history[state.job_id] = state
        return history

    @staticmethod
    def _copy_state_locked(state: JobState | None) -> JobState | None:
        if state is None:
            return None
        copied = JobState(**{key: value for key, value in asdict(state).items()})
        copied.logs = list(state.logs)
        return copied


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
