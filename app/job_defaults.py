from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULTS_PATH = PROJECT_ROOT / "data" / "job_defaults.json"
PROMPT_PATH = PROJECT_ROOT / "config" / "Description_Cleaning_Prompt.txt"


def _default_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return "Clean the video description and return only its factual content."


def _base_defaults() -> dict[str, Any]:
    return {
        "channels": [],
        "video_urls": [],
        "max_videos_per_channel": 2,
        "max_comment_threads_per_video": 25,
        "include_replies": True,
        "description_prompt": _default_prompt(),
    }


def _normalise(value: Any) -> dict[str, Any]:
    defaults = _base_defaults()
    if not isinstance(value, dict):
        return defaults

    for key in ("channels", "video_urls"):
        candidate = value.get(key)
        if isinstance(candidate, list):
            defaults[key] = [item.strip() for item in candidate if isinstance(item, str) and item.strip()]

    for key in ("max_videos_per_channel", "max_comment_threads_per_video"):
        candidate = value.get(key)
        if isinstance(candidate, int) and candidate >= 0:
            defaults[key] = candidate

    if isinstance(value.get("include_replies"), bool):
        defaults["include_replies"] = value["include_replies"]
    if isinstance(value.get("description_prompt"), str) and value["description_prompt"].strip():
        defaults["description_prompt"] = value["description_prompt"].strip()
    return defaults


def load_job_defaults() -> dict[str, Any]:
    try:
        value = json.loads(DEFAULTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _base_defaults()
    return _normalise(value)


def save_job_defaults(values: dict[str, Any]) -> None:
    """Persist job fields without ever storing the YouTube API key."""
    DEFAULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFAULTS_PATH.write_text(
        json.dumps(_normalise(values), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
