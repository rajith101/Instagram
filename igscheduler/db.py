"""SQLite storage for scheduled posts."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from typing import List, Optional

from .timeutil import now_iso

MEDIA_TYPES = ("IMAGE", "REELS")

# scheduled -> publishing -> published
#                        \-> failed -> (retry) -> scheduled
STATUSES = ("scheduled", "publishing", "published", "failed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    media_type   TEXT NOT NULL,
    media_url    TEXT NOT NULL,
    caption      TEXT NOT NULL DEFAULT '',
    scheduled_at TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'scheduled',
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    ig_media_id  TEXT,
    created_at   TEXT NOT NULL,
    published_at TEXT
);
CREATE INDEX IF NOT EXISTS posts_due ON posts (status, scheduled_at);
"""


@dataclass
class Post:
    id: int
    media_type: str
    media_url: str
    caption: str
    scheduled_at: str
    status: str
    attempts: int
    error: Optional[str]
    ig_media_id: Optional[str]
    created_at: str
    published_at: Optional[str]

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class Store:
    """Opens a fresh connection per operation, so it is safe to share across threads."""

    def __init__(self, path: str):
        self.path = path
        with closing(self._connect()) as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _execute(self, sql: str, params=()) -> sqlite3.Cursor:
        with closing(self._connect()) as conn:
            with conn:
                return conn.execute(sql, params)

    def _query(self, sql: str, params=()) -> List[Post]:
        with closing(self._connect()) as conn:
            return [Post(**dict(row)) for row in conn.execute(sql, params)]

    def add(self, media_type: str, media_url: str, caption: str, scheduled_at: str) -> Post:
        cur = self._execute(
            "INSERT INTO posts (media_type, media_url, caption, scheduled_at, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (media_type, media_url, caption, scheduled_at, now_iso()),
        )
        return self.get(cur.lastrowid)

    def get(self, post_id: int) -> Optional[Post]:
        rows = self._query("SELECT * FROM posts WHERE id = ?", (post_id,))
        return rows[0] if rows else None

    def list(self) -> List[Post]:
        return self._query(
            "SELECT * FROM posts ORDER BY"
            " CASE status WHEN 'published' THEN 1 ELSE 0 END,"
            " CASE WHEN status = 'published' THEN published_at END DESC,"
            " scheduled_at, id"
        )

    def due(self, now: str) -> List[Post]:
        return self._query(
            "SELECT * FROM posts WHERE status = 'scheduled' AND scheduled_at <= ?"
            " ORDER BY scheduled_at, id",
            (now,),
        )

    def claim(self, post_id: int) -> bool:
        """Atomically move a post from scheduled to publishing. Returns False if
        someone else already claimed it (or it was deleted)."""
        cur = self._execute(
            "UPDATE posts SET status = 'publishing', attempts = attempts + 1"
            " WHERE id = ? AND status = 'scheduled'",
            (post_id,),
        )
        return cur.rowcount == 1

    def mark_published(self, post_id: int, ig_media_id: str) -> None:
        self._execute(
            "UPDATE posts SET status = 'published', ig_media_id = ?, error = NULL,"
            " published_at = ? WHERE id = ?",
            (ig_media_id, now_iso(), post_id),
        )

    def mark_failed(self, post_id: int, error: str) -> None:
        self._execute(
            "UPDATE posts SET status = 'failed', error = ? WHERE id = ?", (error, post_id)
        )

    def reschedule(self, post_id: int, scheduled_at: Optional[str] = None) -> bool:
        """Put a failed or scheduled post (back) in the queue."""
        if scheduled_at is None:
            cur = self._execute(
                "UPDATE posts SET status = 'scheduled', error = NULL"
                " WHERE id = ? AND status IN ('failed', 'scheduled')",
                (post_id,),
            )
        else:
            cur = self._execute(
                "UPDATE posts SET status = 'scheduled', error = NULL, scheduled_at = ?"
                " WHERE id = ? AND status IN ('failed', 'scheduled')",
                (scheduled_at, post_id),
            )
        return cur.rowcount == 1

    def delete(self, post_id: int) -> bool:
        """Delete a post that is not currently being published."""
        cur = self._execute(
            "DELETE FROM posts WHERE id = ? AND status != 'publishing'", (post_id,)
        )
        return cur.rowcount == 1

    def recover_interrupted(self) -> int:
        """Posts left in 'publishing' after a crash may or may not have gone out.
        Mark them failed rather than risk a duplicate post; the user can retry."""
        cur = self._execute(
            "UPDATE posts SET status = 'failed',"
            " error = 'Interrupted while publishing - check Instagram before retrying'"
            " WHERE status = 'publishing'"
        )
        return cur.rowcount
