from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
import streamlit as st

if __package__:
    from .job_defaults import load_job_defaults, save_job_defaults
    from .local_services import ensure_local_services
else:
    from app.job_defaults import load_job_defaults, save_job_defaults
    from app.local_services import ensure_local_services


CRAWLER_URL = "http://127.0.0.1:8000"
DEFAULT_PROMPT_PATH = Path(__file__).resolve().parents[1] / "config" / "Description_Cleaning_Prompt.txt"


def api_request(method: str, path: str, **kwargs: Any) -> Any:
    try:
        response = httpx.request(method, f"{CRAWLER_URL}{path}", timeout=15, **kwargs)
        response.raise_for_status()
        if response.status_code == 204:
            return {}
        return response.json()
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail", exc.response.text)
        except ValueError:
            detail = exc.response.text
        raise RuntimeError(str(detail)) from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Crawler service is unreachable: {exc}") from exc


def parse_lines(value: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for line in value.splitlines():
        cleaned = line.strip()
        if cleaned and not cleaned.startswith("#") and cleaned not in seen:
            result.append(cleaned)
            seen.add(cleaned)
    return result


def default_prompt() -> str:
    try:
        return DEFAULT_PROMPT_PATH.read_text(encoding="utf-8")
    except OSError:
        return "Clean the video description and return only its factual content."


def configure_page() -> None:
    st.set_page_config(page_title="Comment Scraper", page_icon="💬", layout="wide")
    st.markdown(
        """
        <style>
        .block-container { max-width: 1500px; padding-top: 2rem; }
        [data-testid="stMetricValue"] { font-size: 1.45rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def initialise_state() -> None:
    defaults = load_job_defaults()
    if "api_key" not in st.session_state:
        st.session_state.api_key = ""
    if "description_prompt" not in st.session_state:
        st.session_state.description_prompt = defaults["description_prompt"] or default_prompt()
    if "channels_text" not in st.session_state:
        st.session_state.channels_text = "\n".join(defaults["channels"])
    if "videos_text" not in st.session_state:
        st.session_state.videos_text = "\n".join(defaults["video_urls"])
    if "max_videos_per_channel" not in st.session_state:
        st.session_state.max_videos_per_channel = defaults["max_videos_per_channel"]
    if "max_comment_threads_per_video" not in st.session_state:
        st.session_state.max_comment_threads_per_video = defaults["max_comment_threads_per_video"]
    if "include_replies" not in st.session_state:
        st.session_state.include_replies = defaults["include_replies"]


def render_sidebar(service_status: dict[str, tuple[bool, str]]) -> str:
    with st.sidebar:
        st.title("Comment Scraper")
        st.caption("Local YouTube scraping application")
        st.divider()
        st.subheader("Local services")
        for name, (available, message) in service_status.items():
            label = name.capitalize()
            if available:
                st.success(f"{label}: {message}")
            else:
                st.error(f"{label}: {message}")
        st.subheader("Session access")
        api_key = st.text_input(
            "YouTube API key",
            type="password",
            key="api_key",
            help="Kept only in this session's memory and never persisted.",
        )
        if api_key:
            st.success("API key set for this session.")
        else:
            st.warning("API key required")
    return api_key


def render_job_status_compact(state: dict[str, Any]) -> None:
    status = state.get("status", "idle")
    if status in {"starting", "running", "paused"}:
        st.sidebar.info(f"Active job: {status}")
    elif status in {"completed", "cancelled", "failed"}:
        st.sidebar.caption(f"Last job: {status}")


def render_scraping(api_key: str) -> None:
    st.title("Scraping management")
    st.caption("Configure, scrape, and monitor YouTube channels and individual videos.")

    try:
        state = api_request("GET", "/jobs/current")
    except RuntimeError as exc:
        st.error(str(exc))
        return

    render_job_status_compact(state)
    if state.get("status") in {"starting", "running", "paused"}:
        render_active_job(state)
        return

    if state.get("status") in {"completed", "cancelled", "failed"}:
        render_finished_job(state)

    st.subheader("Configure a new job")
    with st.form("scrape_form"):
        left, right = st.columns(2)
        with left:
            channels_text = st.text_area(
                "YouTube channels",
                key="channels_text",
                height=150,
                placeholder="@GoogleDevelopers\nUC_x5XG1OV2P6uZZ5FSM9Ttw\nhttps://www.youtube.com/@YouTubeCreators/videos",
                help="One handle, channel ID, or supported channel URL per line.",
            )
            max_videos = st.number_input(
                "Maximum videos per channel",
                min_value=0,
                value=int(st.session_state.max_videos_per_channel),
                step=1,
                key="max_videos_per_channel",
                help="0 means all videos in the uploads playlist.",
            )
        with right:
            videos_text = st.text_area(
                "Individual YouTube videos",
                key="videos_text",
                height=150,
                placeholder="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                help="One YouTube video URL per line. Already stored videos are skipped.",
            )
            max_comments = st.number_input(
                "Maximum comment threads per video",
                min_value=0,
                value=int(st.session_state.max_comment_threads_per_video),
                step=1,
                key="max_comment_threads_per_video",
                help="0 means all available top-level comment threads.",
            )

        include_replies = st.checkbox(
            "Include replies and fetch missing replies",
            key="include_replies",
        )
        prompt = st.text_area(
            "Description-cleaning prompt",
            key="description_prompt",
            height=230,
            help="Saved as the default for future jobs. It is not stored in the database.",
        )
        submitted = st.form_submit_button(
            "Start scraping",
            type="primary",
            disabled=not bool(api_key.strip()),
            width="stretch",
        )

    if not submitted:
        return
    channels = parse_lines(channels_text)
    video_urls = parse_lines(videos_text)
    if not channels and not video_urls:
        st.error("At least one channel or video URL is required.")
        return
    try:
        api_request(
            "POST",
            "/jobs",
            json={
                "api_key": api_key,
                "channels": channels,
                "video_urls": video_urls,
                "max_videos_per_channel": int(max_videos),
                "max_comment_threads_per_video": int(max_comments),
                "include_replies": include_replies,
                "description_prompt": prompt,
            },
        )
        save_job_defaults(
            {
                "channels": channels,
                "video_urls": video_urls,
                "max_videos_per_channel": int(max_videos),
                "max_comment_threads_per_video": int(max_comments),
                "include_replies": include_replies,
                "description_prompt": prompt,
            }
        )
        st.success("Scraping job started.")
        st.rerun()
    except RuntimeError as exc:
        st.error(f"Could not start scraping job: {exc}")


def render_active_job(state: dict[str, Any]) -> None:
    status = state.get("status", "running")
    st.subheader(f"Current job · {status}")
    st.info(state.get("current_message") or "Preparing job ...")

    summary = state.get("summary") or {}
    metrics = st.columns(6)
    metrics[0].metric("Videos", summary.get("videos", 0))
    metrics[1].metric("Skipped", summary.get("skipped_videos", 0))
    metrics[2].metric("Threads", summary.get("threads", 0))
    metrics[3].metric("Comments", summary.get("comments", 0))
    metrics[4].metric("Errors", summary.get("errors", 0))
    metrics[5].metric("Channels", summary.get("channels", 0))

    job_id = state.get("job_id")
    controls = st.columns(3)
    if status == "running" and controls[0].button("Pause", width="stretch"):
        api_request("POST", f"/jobs/{job_id}/pause")
        st.rerun()
    if status == "paused" and controls[0].button("Resume", width="stretch"):
        api_request("POST", f"/jobs/{job_id}/resume")
        st.rerun()
    if status in {"running", "paused", "starting"} and controls[1].button("Cancel", width="stretch"):
        api_request("POST", f"/jobs/{job_id}/cancel")
        st.rerun()

    render_logs(state.get("logs", []))
    time.sleep(1)
    st.rerun()


def render_finished_job(state: dict[str, Any]) -> None:
    status = state.get("status")
    if status == "completed":
        st.success("The last scraping job completed.")
    elif status == "cancelled":
        st.warning("The last scraping job was cancelled. Data saved so far remains available.")
    else:
        st.error(f"The last scraping job failed: {state.get('error') or 'unknown cause'}")
    summary = state.get("summary") or {}
    metrics = st.columns(5)
    metrics[0].metric("Videos", summary.get("videos", 0))
    metrics[1].metric("Skipped", summary.get("skipped_videos", 0))
    metrics[2].metric("Threads", summary.get("threads", 0))
    metrics[3].metric("Comments", summary.get("comments", 0))
    metrics[4].metric("Errors", summary.get("errors", 0))
    with st.expander("Job log", expanded=True):
        render_logs(state.get("logs", []))


def render_logs(logs: list[dict[str, str]]) -> None:
    if not logs:
        st.caption("No log entries yet.")
        return
    lines = [
        f"{entry.get('timestamp', '')} [{entry.get('level', '').upper()}] {entry.get('message', '')}"
        for entry in logs[-200:]
    ]
    st.code("\n".join(lines), language="text")


def render_jobs() -> None:
    st.title("Jobs")
    st.caption("Review scraping jobs, their logs, and failures.")
    try:
        result = api_request("GET", "/jobs")
    except RuntimeError as exc:
        st.error(str(exc))
        return

    jobs = result.get("jobs", [])
    if not jobs:
        st.info("No scraping jobs have been recorded yet.")
        return

    rows = []
    for job in jobs:
        summary = job.get("summary") or {}
        rows.append(
            {
                "Job ID": job.get("job_id"),
                "Status": job.get("status"),
                "Started": job.get("started_at"),
                "Finished": job.get("finished_at") or "-",
                "Videos": summary.get("videos", 0),
                "Threads": summary.get("threads", 0),
                "Comments": summary.get("comments", 0),
                "Errors": summary.get("errors", 0),
            }
        )
    st.dataframe(rows, use_container_width=True, hide_index=True)

    job_ids = [job["job_id"] for job in jobs]
    selected = st.selectbox(
        "Open job",
        job_ids,
        format_func=lambda job_id: next(
            (
                f"{job_id[:8]} · {job.get('status', 'unknown')} · {job.get('started_at', '')}"
                for job in jobs
                if job.get("job_id") == job_id
            ),
            job_id,
        ),
    )
    try:
        job = api_request("GET", f"/jobs/{selected}")
        logs = api_request("GET", f"/jobs/{selected}/logs").get("logs", [])
    except RuntimeError as exc:
        st.error(str(exc))
        return

    st.subheader(f"Job details · {selected[:8]}")
    status = job.get("status", "unknown")
    st.caption(
        f"Status: {status} · Started: {job.get('started_at', '-')} · "
        f"Finished: {job.get('finished_at') or '-'}"
    )
    if job.get("error"):
        st.error(job["error"])

    warnings_and_errors = [
        entry for entry in logs if entry.get("level") in {"warning", "error"}
    ]
    if warnings_and_errors:
        st.markdown("#### Errors and warnings")
        for entry in warnings_and_errors:
            message = f"{entry.get('timestamp', '')} · {entry.get('message', '')}"
            if entry.get("level") == "error":
                st.error(message)
            else:
                st.warning(message)

    with st.expander("Job configuration"):
        st.json(job.get("config_summary") or {})
    with st.expander("Full job log", expanded=True):
        render_logs(logs)

    if status in {"starting", "running", "paused"}:
        st.info("Active jobs cannot be deleted. Cancel the job first.")
    elif st.button("Delete job", type="secondary", key=f"delete_job_{selected}"):
        try:
            api_request("DELETE", f"/jobs/{selected}")
            st.success("Job deleted.")
            st.rerun()
        except RuntimeError as exc:
            st.error(str(exc))

def render_browser() -> None:
    st.title("Videos and comments")
    st.caption("Browse and inspect the stored YouTube data.")
    try:
        stats = api_request("GET", "/data/statistics")
        channels = api_request("GET", "/data/channels").get("channels", [])
    except RuntimeError as exc:
        st.error(str(exc))
        return

    metric_columns = st.columns(3)
    metric_columns[0].metric("Videos", stats.get("videos", 0))
    metric_columns[1].metric("Threads", stats.get("threads", 0))
    metric_columns[2].metric("Comments", stats.get("comments", 0))

    channel_column, page_column = st.columns([1.5, 1])
    with channel_column:
        channel = st.selectbox("Channel", ["All channels", *channels])
    with page_column:
        page_size = st.selectbox("Per page", [10, 25, 50], index=1)
    page = st.number_input("Page", min_value=1, value=1, step=1)

    try:
        result = api_request(
            "GET",
            "/data/videos",
            params={
                "channel": "" if channel == "All channels" else channel,
                "limit": page_size,
                "offset": (int(page) - 1) * page_size,
            },
        )
    except RuntimeError as exc:
        st.error(str(exc))
        return

    videos = result.get("videos", [])
    if not videos:
        st.info("No videos found.")
        return

    table_rows = [
        {
            "Video ID": video.get("video_id"),
            "Title": video.get("title"),
            "Channel": video.get("channel_title") or video.get("channel_id"),
            "Published": video.get("published_at"),
            "Cleaning": video.get("description_clean_status"),
        }
        for video in videos
    ]
    st.dataframe(table_rows, use_container_width=True, hide_index=True)

    selected = st.selectbox(
        "Open video",
        [video["video_id"] for video in videos],
        format_func=lambda video_id: next(
            (f"{item.get('title', '')} · {video_id}" for item in videos if item.get("video_id") == video_id),
            video_id,
        ),
    )
    try:
        video = api_request("GET", f"/data/videos/{selected}")
    except RuntimeError as exc:
        st.error(str(exc))
        return
    render_video_detail(video)


def render_video_detail(video: dict[str, Any]) -> None:
    st.divider()
    st.subheader(video.get("title") or video.get("video_id"))
    st.markdown(
        f"Channel: **{video.get('channel_title') or video.get('channel_id') or '-'}**  ·  "
        f"[Open on YouTube](https://www.youtube.com/watch?v={video.get('video_id')})"
    )
    raw, cleaned = st.columns(2)
    with raw:
        st.markdown("#### Original description")
        st.write(video.get("description") or "-")
    with cleaned:
        st.markdown("#### Cleaned description")
        st.write(video.get("description_clean") or "-")

    comments = video.get("comments", [])
    st.markdown(f"#### Comments ({len(comments)})")
    if not comments:
        st.info("No comments were stored for this video.")
        return
    for index, comment in enumerate(comments, start=1):
        label = f"Comment {index} · {comment.get('published_at') or ''}"
        with st.expander(label):
            st.write(comment.get("text") or "")


def main() -> None:
    configure_page()
    service_status = ensure_local_services()
    initialise_state()
    api_key = render_sidebar(service_status)
    page = st.sidebar.radio("View", ["Scraping", "Jobs", "Videos & comments"])
    if page == "Scraping":
        render_scraping(api_key)
    elif page == "Jobs":
        render_jobs()
    else:
        render_browser()


if __name__ == "__main__":
    main()
