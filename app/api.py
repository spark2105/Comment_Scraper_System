from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, SecretStr, field_validator

from .database import get_video, list_channels, list_videos, statistics
from .job_manager import JobManager
from .scraper import ScrapeConfig


app = FastAPI(title="Comment Scraper Worker", version="1.0.0")
manager = JobManager()


class JobRequest(BaseModel):
    api_key: SecretStr
    channels: list[str] = Field(default_factory=list)
    video_urls: list[str] = Field(default_factory=list)
    max_videos_per_channel: int = Field(default=0, ge=0)
    max_comment_threads_per_video: int = Field(default=0, ge=0)
    include_replies: bool = True
    description_prompt: str = Field(min_length=1)

    @field_validator("channels", "video_urls")
    @classmethod
    def clean_values(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            cleaned = value.strip()
            if cleaned and cleaned not in seen:
                result.append(cleaned)
                seen.add(cleaned)
        return result

    @field_validator("description_prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("The description-cleaning prompt must not be empty.")
        return value.strip()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/jobs", status_code=202)
async def create_job(request: JobRequest) -> dict[str, Any]:
    if not request.channels and not request.video_urls:
        raise HTTPException(status_code=422, detail="At least one channel or video URL is required.")
    config = ScrapeConfig(
        channels=request.channels,
        video_urls=request.video_urls,
        max_videos_per_channel=request.max_videos_per_channel,
        max_comment_threads_per_video=request.max_comment_threads_per_video,
        include_replies=request.include_replies,
        description_prompt=request.description_prompt,
    )
    try:
        state = manager.create_and_start(
            api_key=request.api_key.get_secret_value(),
            config=config,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return state.public_dict()


@app.get("/jobs")
async def list_jobs() -> dict[str, list[dict[str, Any]]]:
    jobs = []
    for state in manager.list_jobs():
        data = state.public_dict()
        data["logs"] = []
        jobs.append(data)
    return {"jobs": jobs}

@app.get("/jobs/current")
async def current_job() -> dict[str, Any]:
    state = manager.current()
    return state.public_dict() if state else {"status": "idle"}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    try:
        return manager.get(job_id).public_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Scraping job not found.") from exc


@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str) -> dict[str, str]:
    try:
        manager.delete(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Scraping job not found.") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "deleted"}

@app.get("/jobs/{job_id}/logs")
async def get_job_logs(job_id: str) -> dict[str, Any]:
    try:
        return {"job_id": job_id, "logs": manager.logs(job_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/jobs/{job_id}/pause")
async def pause_job(job_id: str) -> dict[str, Any]:
    try:
        return manager.pause(job_id).public_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/jobs/{job_id}/resume")
async def resume_job(job_id: str) -> dict[str, Any]:
    try:
        return manager.resume(job_id).public_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict[str, Any]:
    try:
        return manager.cancel(job_id).public_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/data/statistics")
async def data_statistics() -> dict[str, int]:
    return statistics()


@app.get("/data/channels")
async def data_channels() -> dict[str, list[str]]:
    return {"channels": list_channels()}


@app.get("/data/videos")
async def data_videos(
    channel: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=250),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    return {"videos": list_videos(channel=channel, limit=limit, offset=offset)}


@app.get("/data/videos/{video_id}")
async def data_video(video_id: str) -> dict[str, Any]:
    video = get_video(video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found.")
    return video
