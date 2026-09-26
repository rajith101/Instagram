"""Timestamps are stored as UTC ISO-8601 strings ("2026-01-31T09:30:00Z") so they
sort and compare correctly as plain text in SQLite."""

from datetime import datetime, timezone

FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        # Naive values are interpreted as the server's local time.
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).strftime(FORMAT)


def parse(value: str) -> datetime:
    """Parse an ISO-8601 string. Values without an offset are treated as local time."""
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc)


def now_iso() -> str:
    return to_iso(utcnow())
