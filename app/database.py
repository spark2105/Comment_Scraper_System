from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "sql" / "comment_scraper.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS Videos (
    videoID TEXT PRIMARY KEY,
    channel_id TEXT,
    channel_title TEXT,
    Video_Title TEXT NOT NULL,
    Video_Description TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    Description_Clean TEXT,
    Description_Clean_Status TEXT NOT NULL DEFAULT 'pending',
    scraped_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS Threads (
    ThreadID TEXT PRIMARY KEY,
    videoID TEXT NOT NULL,
    Top_Level_Comment_ID TEXT,
    total_reply_count INTEGER,
    FOREIGN KEY (videoID) REFERENCES Videos(videoID) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS Comments (
    Comment_ID TEXT PRIMARY KEY,
    ThreadID TEXT NOT NULL,
    VideoID TEXT NOT NULL,
    ParentID TEXT,
    published_at TEXT,
    updated_at TEXT,
    Comment_Text TEXT NOT NULL DEFAULT '',
    depth INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (ThreadID) REFERENCES Threads(ThreadID) ON DELETE CASCADE,
    FOREIGN KEY (VideoID) REFERENCES Videos(videoID) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_videos_channel ON Videos(channel_id, channel_title);
CREATE INDEX IF NOT EXISTS idx_comments_thread ON Comments(ThreadID);
CREATE INDEX IF NOT EXISTS idx_comments_video ON Comments(VideoID);
CREATE INDEX IF NOT EXISTS idx_comments_published ON Comments(published_at);
"""


def _migrate_legacy_schema(connection: sqlite3.Connection) -> None:
    """Rename legacy German database identifiers without dropping user data."""
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if "Kommentare" in tables and "Comments" not in tables:
        connection.execute("ALTER TABLE Kommentare RENAME TO Comments")

    if "Comments" in tables or "Kommentare" in tables:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(Comments)")
        }
        if "KommentarID" in columns and "CommentID" not in columns:
            connection.execute(
                "ALTER TABLE Comments RENAME COLUMN KommentarID TO CommentID"
            )


def _migrate_video_columns(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(Videos)")
    }
    if not columns:
        return
    if "Title" in columns and "Video_Title" not in columns:
        connection.execute("ALTER TABLE Videos RENAME COLUMN Title TO Video_Title")
    if "Description" in columns and "Video_Description" not in columns:
        connection.execute(
            "ALTER TABLE Videos RENAME COLUMN Description TO Video_Description"
        )


def _migrate_comments_table(connection: sqlite3.Connection) -> None:
    """Rebuild Comments when its columns differ from the current schema."""
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(Comments)")
    }
    if not columns:
        return
    removed_columns = {
        "author_display_name",
        "author_channel_id",
        "like_count",
        "is_reply",
    }
    needs_video_id = "VideoID" not in columns
    needs_renamed_columns = "Comment_ID" not in columns or "Comment_Text" not in columns
    if not columns.intersection(removed_columns) and not needs_video_id and not needs_renamed_columns:
        return

    connection.execute("ALTER TABLE Comments RENAME TO Comments_legacy_metadata")
    connection.execute(
        """
        CREATE TABLE Comments (
            Comment_ID TEXT PRIMARY KEY,
            ThreadID TEXT NOT NULL,
            VideoID TEXT NOT NULL,
            ParentID TEXT,
            published_at TEXT,
            updated_at TEXT,
            Comment_Text TEXT NOT NULL DEFAULT '',
            depth INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (ThreadID) REFERENCES Threads(ThreadID) ON DELETE CASCADE,
            FOREIGN KEY (VideoID) REFERENCES Videos(videoID) ON DELETE CASCADE
        )
        """
    )
    video_id_expression = "legacy.VideoID" if "VideoID" in columns else "threads.videoID"
    comment_id_column = "Comment_ID" if "Comment_ID" in columns else "CommentID"
    comment_text_column = "Comment_Text" if "Comment_Text" in columns else "text"
    connection.execute(
        f"""
        INSERT INTO Comments (
            Comment_ID, ThreadID, VideoID, ParentID, published_at, updated_at,
            Comment_Text, depth
        )
        SELECT legacy.{comment_id_column}, legacy.ThreadID, {video_id_expression},
               legacy.ParentID, legacy.published_at, legacy.updated_at,
               legacy.{comment_text_column}, legacy.depth
        FROM Comments_legacy_metadata AS legacy
        JOIN Threads AS threads ON threads.ThreadID = legacy.ThreadID
        """
    )
    connection.execute("DROP TABLE Comments_legacy_metadata")


def ensure_database(path: str | Path = DEFAULT_DATABASE_PATH) -> Path:
    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=30)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        _migrate_legacy_schema(connection)
        _migrate_video_columns(connection)
        _migrate_comments_table(connection)
        connection.execute("DROP INDEX IF EXISTS idx_videos_title")
        connection.executescript(SCHEMA)
        connection.commit()
    finally:
        connection.close()
    return database_path


@contextmanager
def connect(path: str | Path = DEFAULT_DATABASE_PATH) -> Iterator[sqlite3.Connection]:
    database_path = ensure_database(path)
    connection = sqlite3.connect(database_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    try:
        yield connection
    finally:
        connection.close()


def known_video_ids(path: str | Path = DEFAULT_DATABASE_PATH) -> set[str]:
    with connect(path) as connection:
        return {row[0] for row in connection.execute("SELECT videoID FROM Videos")}


def videos_requiring_description_cleaning(
    path: str | Path = DEFAULT_DATABASE_PATH,
) -> set[str]:
    """Return stored videos whose description has not produced usable output yet."""
    with connect(path) as connection:
        rows = connection.execute(
            """
            SELECT videoID
            FROM Videos
            WHERE (
                TRIM(COALESCE(Video_Description, '')) = ''
                AND COALESCE(Description_Clean_Status, 'pending') <> 'empty'
            )
            OR (
                TRIM(COALESCE(Video_Description, '')) <> ''
                AND (
                    COALESCE(Description_Clean_Status, 'pending') <> 'cleaned'
                    OR TRIM(COALESCE(Description_Clean, '')) = ''
                )
            )
            """
        ).fetchall()
    return {str(row[0]) for row in rows}


def save_video_with_comments(
    video: dict[str, Any],
    threads: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    path: str | Path = DEFAULT_DATABASE_PATH,
) -> None:
    """Persist one complete video unit in one short transaction."""
    with connect(path) as connection:
        connection.execute(
            """
            INSERT INTO Videos (
                videoID, channel_id, channel_title, Video_Title, Video_Description,
                published_at, Description_Clean, Description_Clean_Status, scraped_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, 'pending', ?)
            ON CONFLICT(videoID) DO UPDATE SET
                channel_id = excluded.channel_id,
                channel_title = excluded.channel_title,
                Video_Title = excluded.Video_Title,
                Video_Description = excluded.Video_Description,
                published_at = excluded.published_at,
                scraped_at = excluded.scraped_at,
                Description_Clean = CASE
                    WHEN Videos.Video_Description IS excluded.Video_Description
                    THEN Videos.Description_Clean ELSE NULL END,
                Description_Clean_Status = CASE
                    WHEN Videos.Video_Description IS excluded.Video_Description
                    THEN Videos.Description_Clean_Status ELSE 'pending' END
            """,
            (
                video["id"],
                video.get("channel_id"),
                video.get("channel_title"),
                video.get("title", ""),
                video.get("description", ""),
                video.get("published_at"),
                video["scraped_at"],
            ),
        )

        for thread in threads:
            connection.execute(
                """
                INSERT INTO Threads (ThreadID, videoID, Top_Level_Comment_ID, total_reply_count)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(ThreadID) DO UPDATE SET
                    videoID = excluded.videoID,
                    Top_Level_Comment_ID = excluded.Top_Level_Comment_ID,
                    total_reply_count = excluded.total_reply_count
                """,
                (
                    thread["id"],
                    video["id"],
                    thread.get("top_level_comment_id"),
                    thread.get("total_reply_count"),
                ),
            )

        for comment in comments:
            connection.execute(
                """
                INSERT INTO Comments (
                    Comment_ID, ThreadID, VideoID, ParentID, published_at, updated_at,
                    Comment_Text, depth
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(Comment_ID) DO UPDATE SET
                    ThreadID = excluded.ThreadID,
                    VideoID = excluded.VideoID,
                    ParentID = excluded.ParentID,
                    published_at = excluded.published_at,
                    updated_at = excluded.updated_at,
                    Comment_Text = excluded.Comment_Text,
                    depth = excluded.depth
                """,
                (
                    comment["id"],
                    comment["thread_id"],
                    comment["video_id"],
                    comment.get("parent_id"),
                    comment.get("published_at"),
                    comment.get("updated_at"),
                    comment.get("text", ""),
                    comment.get("depth", 0),
                ),
            )
        connection.commit()


def update_description_clean(
    video_id: str,
    cleaned_description: str,
    status: str,
    path: str | Path = DEFAULT_DATABASE_PATH,
) -> None:
    with connect(path) as connection:
        connection.execute(
            """
            UPDATE Videos
            SET Description_Clean = ?, Description_Clean_Status = ?
            WHERE videoID = ?
            """,
            (cleaned_description, status, video_id),
        )
        connection.commit()


def statistics(path: str | Path = DEFAULT_DATABASE_PATH) -> dict[str, int]:
    with connect(path) as connection:
        return {
            "videos": int(connection.execute("SELECT COUNT(*) FROM Videos").fetchone()[0]),
            "threads": int(connection.execute("SELECT COUNT(*) FROM Threads").fetchone()[0]),
            "comments": int(connection.execute("SELECT COUNT(*) FROM Comments").fetchone()[0]),
        }


def list_channels(path: str | Path = DEFAULT_DATABASE_PATH) -> list[str]:
    with connect(path) as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT COALESCE(NULLIF(channel_title, ''), channel_id) AS channel
            FROM Videos
            WHERE channel_id IS NOT NULL OR channel_title IS NOT NULL
            ORDER BY channel COLLATE NOCASE
            """
        ).fetchall()
    return [row[0] for row in rows if row[0]]


def list_videos(
    *,
    channel: str = "",
    limit: int = 50,
    offset: int = 0,
    path: str | Path = DEFAULT_DATABASE_PATH,
) -> list[dict[str, Any]]:
    conditions: list[str] = []
    parameters: list[Any] = []
    if channel.strip():
        conditions.append("(channel_title = ? OR channel_id = ?)")
        parameters.extend([channel, channel])

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"""
        SELECT videoID AS video_id, channel_id, channel_title, Video_Title AS title,
               Video_Description AS description, Description_Clean AS description_clean,
               Description_Clean_Status AS description_clean_status,
               published_at, scraped_at
        FROM Videos
        {where}
        ORDER BY COALESCE(published_at, scraped_at) DESC, Video_Title COLLATE NOCASE
        LIMIT ? OFFSET ?
    """
    parameters.extend([max(1, min(limit, 250)), max(0, offset)])
    with connect(path) as connection:
        rows = connection.execute(query, parameters).fetchall()
    return [dict(row) for row in rows]


def get_video(video_id: str, path: str | Path = DEFAULT_DATABASE_PATH) -> dict[str, Any] | None:
    with connect(path) as connection:
        video_row = connection.execute(
            """
            SELECT videoID AS video_id, channel_id, channel_title, Video_Title AS title,
                   Video_Description AS description, Description_Clean AS description_clean,
                   Description_Clean_Status AS description_clean_status,
                   published_at, scraped_at
            FROM Videos WHERE videoID = ?
            """,
            (video_id,),
        ).fetchone()
        if video_row is None:
            return None

        comment_rows = connection.execute(
            """
            SELECT c.Comment_ID AS comment_id, c.ThreadID AS thread_id,
                   c.VideoID AS video_id,
                   c.ParentID AS parent_id, c.published_at, c.updated_at,
                   c.Comment_Text AS text, c.depth
            FROM Comments c
            JOIN Threads t ON t.ThreadID = c.ThreadID
            WHERE t.videoID = ?
            ORDER BY COALESCE(c.published_at, '') ASC, c.depth ASC
            """,
            (video_id,),
        ).fetchall()

    result = dict(video_row)
    result["comments"] = [dict(row) for row in comment_rows]
    return result
