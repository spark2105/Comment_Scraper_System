from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from .cleaner import DescriptionCleaner
from .database import (
    DEFAULT_DATABASE_PATH,
    known_video_ids,
    save_video_with_comments,
    update_description_clean,
    videos_requiring_description_cleaning,
)


class YouTubeFetchError(RuntimeError):
    """Raised for an unsuccessful YouTube Data API request."""


class DescriptionCleaningAbort(RuntimeError):
    """Raised when description cleaning must abort the current run."""


class ScrapeCancelled(RuntimeError):
    """Raised when the user cancels a run at a safe checkpoint."""


@dataclass(frozen=True)
class ScrapeConfig:
    channels: list[str]
    video_urls: list[str]
    max_videos_per_channel: int
    max_comment_threads_per_video: int
    include_replies: bool
    description_prompt: str
    timeout: int = 30
    delay: float = 0.1


class YouTubeClient:
    base_url = "https://www.googleapis.com/youtube/v3"

    def __init__(self, api_key: str, *, timeout: int = 30) -> None:
        if not api_key.strip():
            raise YouTubeFetchError("No YouTube API key was provided.")
        self._client = httpx.Client(timeout=timeout, follow_redirects=True)
        self._api_key = api_key.strip()

    def close(self) -> None:
        self._client.close()

    def get(self, endpoint: str, **params: Any) -> dict[str, Any]:
        params = {key: value for key, value in params.items() if value is not None}
        params["key"] = self._api_key
        try:
            response = self._client.get(f"{self.base_url}/{endpoint.lstrip('/')}", params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:1000]
            raise YouTubeFetchError(
                f"YouTube API HTTP {exc.response.status_code} at {endpoint}: {detail}"
            ) from exc
        except httpx.HTTPError as exc:
            raise YouTubeFetchError(f"YouTube API is unreachable: {exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise YouTubeFetchError("YouTube API returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise YouTubeFetchError("YouTube API returned an unexpected data format.")
        return payload

    def channel(self, identifier: tuple[str, str]) -> dict[str, Any]:
        kind, value = identifier
        params = {"part": "snippet,contentDetails,statistics"}
        params["id" if kind == "id" else "forHandle" if kind == "handle" else "forUsername"] = value
        payload = self.get("channels", **params)
        items = payload.get("items", [])
        if not items:
            raise YouTubeFetchError(f"YouTube-Channel nicht gefunden: {value}")
        return items[0]

    def video(self, video_id: str) -> dict[str, Any]:
        payload = self.get("videos", part="snippet,status", id=video_id)
        items = payload.get("items", [])
        if not items:
            raise YouTubeFetchError(f"YouTube-Video nicht gefunden: {video_id}")
        return items[0]

    def playlist_items(self, playlist_id: str, *, page_token: str | None, max_results: int = 50) -> dict[str, Any]:
        return self.get(
            "playlistItems",
            part="snippet,contentDetails,status",
            playlistId=playlist_id,
            pageToken=page_token,
            maxResults=max(1, min(max_results, 50)),
        )

    def comment_threads(
        self,
        video_id: str,
        *,
        page_token: str | None,
        include_replies: bool,
        max_results: int = 100,
    ) -> dict[str, Any]:
        return self.get(
            "commentThreads",
            part="snippet,replies" if include_replies else "snippet",
            videoId=video_id,
            pageToken=page_token,
            maxResults=max(1, min(max_results, 100)),
            order="time",
            textFormat="plainText",
        )

    def replies(self, parent_id: str, *, page_token: str | None, max_results: int = 100) -> dict[str, Any]:
        return self.get(
            "comments",
            part="snippet",
            parentId=parent_id,
            pageToken=page_token,
            maxResults=max(1, min(max_results, 100)),
            textFormat="plainText",
        )


class Scraper:
    def __init__(
        self,
        *,
        api_key: str,
        config: ScrapeConfig,
        log: Callable[[str, str], None],
        checkpoint: Callable[[], None],
        database_path: str = str(DEFAULT_DATABASE_PATH),
    ) -> None:
        self.config = config
        self.log = log
        self.checkpoint = checkpoint
        self.database_path = database_path
        self.client = YouTubeClient(api_key, timeout=config.timeout)
        self.cleaner = DescriptionCleaner()
        self.known_ids = known_video_ids(database_path)
        self.cleaning_ids = videos_requiring_description_cleaning(database_path)
        self.stats = {
            "channels": 0,
            "videos": 0,
            "skipped_videos": 0,
            "threads": 0,
            "comments": 0,
            "cleaned_existing": 0,
            "errors": 0,
        }

    def run(self) -> dict[str, Any]:
        if not self.config.channels and not self.config.video_urls:
            raise ValueError("At least one channel or video URL is required.")
        try:
            for identifier in self.config.channels:
                self._run_channel(identifier)
            for raw_url in self.config.video_urls:
                self._run_single_video(raw_url)
            return dict(self.stats)
        finally:
            self.client.close()

    def _run_channel(self, raw_identifier: str) -> None:
        self.checkpoint()
        try:
            identifier = parse_channel_identifier(raw_identifier)
            resource = self.client.channel(identifier)
            channel = parse_channel(resource, raw_identifier)
            self.stats["channels"] += 1
            self.log("info", f"Channel detected: {channel['title']} ({channel['id']})")
        except Exception as exc:
            self._record_error("channel", raw_identifier, exc)
            return

        playlist_id = channel.get("uploads_playlist_id")
        if not playlist_id:
            self._record_error("channel", raw_identifier, "No uploads playlist found.")
            return

        checked_videos = 0
        page_token: str | None = None
        while True:
            self.checkpoint()
            if self._reached_limit(checked_videos, self.config.max_videos_per_channel):
                break
            try:
                payload = self.client.playlist_items(playlist_id, page_token=page_token)
            except Exception as exc:
                self._record_error("uploads", raw_identifier, exc)
                return

            for item in payload.get("items", []):
                self.checkpoint()
                if self._reached_limit(checked_videos, self.config.max_videos_per_channel):
                    break
                try:
                    video = parse_video(item, channel)
                    if not video["id"]:
                        raise ValueError("Playlist item has no video ID.")
                except Exception as exc:
                    self._record_error("video_parse", raw_identifier, exc)
                    continue
                checked_videos += 1
                self._process_or_skip(video)

            page_token = payload.get("nextPageToken")
            if not page_token:
                break
            self._sleep()

    def _run_single_video(self, raw_url: str) -> None:
        self.checkpoint()
        try:
            video_id = extract_video_id(raw_url)
            resource = self.client.video(video_id)
            video = parse_direct_video(resource)
            self.log("info", f"Single video detected: {video['title']} ({video_id})")
        except Exception as exc:
            self._record_error("video", raw_url, exc)
            return
        self._process_or_skip(video)

    def _process_or_skip(self, video: dict[str, Any]) -> None:
        video_id = video["id"]
        if video_id in self.known_ids:
            if video_id in self.cleaning_ids:
                self.log("info", f"Refreshing description cleaning: {video['title']}")
                save_video_with_comments(video, [], [], self.database_path)
                self._clean_description(video)
                self.cleaning_ids.discard(video_id)
                self.stats["cleaned_existing"] += 1
                self._sleep()
                return
            self.stats["skipped_videos"] += 1
            self.log("info", f"Skipped, already stored: {video['title']} ({video_id})")
            return

        self.log("info", f"Scrape Video: {video['title']} ({video_id})")
        threads, comments = self._fetch_comments(video)
        video["scraped_at"] = utc_now()
        save_video_with_comments(video, threads, comments, self.database_path)
        self.known_ids.add(video_id)
        self.stats["videos"] += 1
        self.stats["threads"] += len(threads)
        self.stats["comments"] += len(comments)
        self.log(
            "info",
            f"Saved: {video['title']} · {len(threads)} threads · {len(comments)} comments",
        )

        self._clean_description(video)
        self._sleep()

    def _clean_description(self, video: dict[str, Any]) -> None:
        video_id = video["id"]
        description = video.get("description", "")
        try:
            cleaned = self.cleaner.clean(description, self.config.description_prompt)
        except Exception as exc:
            self._record_error("description_cleaning", video_id, exc)
            raise DescriptionCleaningAbort(str(exc)) from exc
        update_description_clean(
            video_id,
            cleaned,
            "empty" if not description.strip() else "cleaned",
            self.database_path,
        )
        self.log("info", f"Description cleaned: {video['title']}")

    def _fetch_comments(self, video: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        threads: list[dict[str, Any]] = []
        comments: list[dict[str, Any]] = []
        page_token: str | None = None
        page_number = 0
        while True:
            self.checkpoint()
            if self._reached_limit(len(threads), self.config.max_comment_threads_per_video):
                break
            page_number += 1
            try:
                payload = self.client.comment_threads(
                    video["id"],
                    page_token=page_token,
                    include_replies=self.config.include_replies,
                )
            except Exception as exc:
                self._record_error("comment_threads", video["id"], exc)
                break

            for item in payload.get("items", []):
                if self._reached_limit(len(threads), self.config.max_comment_threads_per_video):
                    break
                try:
                    thread, parsed_comments, included_replies = parse_thread(item, video)
                except Exception as exc:
                    self._record_error("comment_parse", video["id"], exc)
                    continue
                threads.append(thread)
                comments.extend(parsed_comments)
                total_replies = thread.get("total_reply_count") or 0
                if self.config.include_replies and thread.get("top_level_comment_id") and total_replies > included_replies:
                    self._fetch_missing_replies(
                        video,
                        thread,
                        comments,
                    )

            page_token = payload.get("nextPageToken")
            if not page_token:
                break
            self._sleep()
        return threads, comments

    def _fetch_missing_replies(
        self,
        video: dict[str, Any],
        thread: dict[str, Any],
        comments: list[dict[str, Any]],
    ) -> None:
        parent_id = thread["top_level_comment_id"]
        known_ids = {comment["id"] for comment in comments if comment["thread_id"] == thread["id"]}
        page_token: str | None = None
        while True:
            self.checkpoint()
            try:
                payload = self.client.replies(parent_id, page_token=page_token)
            except Exception as exc:
                self._record_error("comment_replies", video["id"], exc)
                return
            for item in payload.get("items", []):
                comment = parse_comment(
                    item,
                    thread_id=thread["id"],
                    video=video,
                    parent_id=parent_id,
                    depth=1,
                )
                if comment["id"] not in known_ids:
                    comments.append(comment)
                    known_ids.add(comment["id"])
            page_token = payload.get("nextPageToken")
            if not page_token:
                return
            self._sleep()

    def _record_error(self, stage: str, target: str, error: object) -> None:
        self.stats["errors"] += 1
        self.log("error", f"{stage} · {target} · {error}")

    def _sleep(self) -> None:
        if self.config.delay > 0:
            time.sleep(self.config.delay)

    @staticmethod
    def _reached_limit(current: int, limit: int) -> bool:
        return limit > 0 and current >= limit


def parse_channel_identifier(raw: str) -> tuple[str, str]:
    original = raw.strip()
    if not original:
        raise ValueError("Channel-Bezeichner darf nicht leer sein.")
    parsed = urlparse(original)
    if parsed.scheme and parsed.netloc:
        if "youtube.com" not in parsed.netloc.lower():
            raise ValueError("Only YouTube channel URLs are supported.")
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if parts and parts[0] == "channel" and len(parts) > 1:
            return "id", parts[1]
        if parts and parts[0] == "user" and len(parts) > 1:
            return "username", parts[1]
        for part in parts:
            if part.startswith("@"):
                return "handle", part
        raise ValueError("Channel-URL muss /channel/..., /user/... oder /@handle enthalten.")
    if original.startswith("channel:"):
        return "id", original.removeprefix("channel:").strip()
    if original.startswith("user:"):
        return "username", original.removeprefix("user:").strip()
    if original.startswith("@"):
        return "handle", original
    if original.startswith("UC") and len(original) >= 20:
        return "id", original
    return "handle", original


def extract_video_id(raw_url: str) -> str:
    value = raw_url.strip()
    parsed = urlparse(value)
    if not parsed.scheme and re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        return value
    if not parsed.netloc:
        raise ValueError("Not a valid YouTube video URL.")
    host = parsed.netloc.lower()
    if "youtu.be" in host:
        candidate = parsed.path.strip("/").split("/")[0]
    elif "youtube.com" in host:
        query_id = parse_qs(parsed.query).get("v", [None])[0]
        path_parts = [part for part in parsed.path.split("/") if part]
        candidate = query_id or (path_parts[1] if path_parts and path_parts[0] in {"shorts", "embed", "live"} and len(path_parts) > 1 else None)
    else:
        candidate = None
    if not candidate or not re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate):
        raise ValueError("The URL does not contain a recognizable YouTube video ID.")
    return candidate


def parse_channel(resource: dict[str, Any], source: str) -> dict[str, Any]:
    snippet = resource.get("snippet", {})
    details = resource.get("contentDetails", {})
    related = details.get("relatedPlaylists", {})
    return {
        "id": str(resource.get("id", "")),
        "title": str(snippet.get("title", "")),
        "uploads_playlist_id": related.get("uploads"),
        "source": source,
    }


def parse_video(resource: dict[str, Any], channel: dict[str, Any]) -> dict[str, Any]:
    snippet = resource.get("snippet", {})
    details = resource.get("contentDetails", {})
    status = resource.get("status", {})
    resource_id = snippet.get("resourceId", {})
    video_id = str(details.get("videoId") or resource_id.get("videoId") or "")
    return {
        "id": video_id,
        "channel_id": channel["id"],
        "channel_title": snippet.get("videoOwnerChannelTitle") or snippet.get("channelTitle") or channel["title"],
        "title": str(snippet.get("title", "")),
        "description": str(snippet.get("description", "")),
        "published_at": details.get("videoPublishedAt") or snippet.get("publishedAt"),
        "privacy_status": status.get("privacyStatus"),
        "url": f"https://www.youtube.com/watch?v={video_id}",
    }


def parse_direct_video(resource: dict[str, Any]) -> dict[str, Any]:
    snippet = resource.get("snippet", {})
    video_id = str(resource.get("id", ""))
    return {
        "id": video_id,
        "channel_id": snippet.get("channelId"),
        "channel_title": snippet.get("channelTitle"),
        "title": str(snippet.get("title", "")),
        "description": str(snippet.get("description", "")),
        "published_at": snippet.get("publishedAt"),
        "privacy_status": resource.get("status", {}).get("privacyStatus"),
        "url": f"https://www.youtube.com/watch?v={video_id}",
    }


def parse_thread(item: dict[str, Any], video: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    snippet = item.get("snippet", {})
    top_resource = snippet.get("topLevelComment", {})
    top_snippet = top_resource.get("snippet", {})
    thread_id = str(item.get("id", ""))
    top_id = top_resource.get("id")
    thread = {
        "id": thread_id,
        "video_id": video["id"],
        "top_level_comment_id": top_id,
        "total_reply_count": safe_int(snippet.get("totalReplyCount")),
    }
    comments: list[dict[str, Any]] = []
    if top_id:
        comments.append(parse_comment(top_resource, thread_id=thread_id, video=video, parent_id=None, depth=0))
    included = item.get("replies", {}).get("comments", [])
    for reply in included:
        comments.append(parse_comment(reply, thread_id=thread_id, video=video, parent_id=top_id, depth=1))
    return thread, comments, len(included)


def parse_comment(
    resource: dict[str, Any],
    *,
    thread_id: str,
    video: dict[str, Any],
    parent_id: str | None,
    depth: int,
) -> dict[str, Any]:
    snippet = resource.get("snippet", {})
    return {
        "id": str(resource.get("id", "")),
        "thread_id": thread_id,
        "video_id": video["id"],
        "parent_id": parent_id or snippet.get("parentId"),
        "text": str(snippet.get("textOriginal") or snippet.get("textDisplay") or ""),
        "published_at": snippet.get("publishedAt"),
        "updated_at": snippet.get("updatedAt"),
        "depth": depth,
    }


def safe_int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
