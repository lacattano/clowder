"""Time handling.

Two clocks meet here. Records we write use ISO 8601 UTC strings. Pi session
messages carry epoch milliseconds. Both are parsed to one float so they can be
compared, which is what makes "usage since this dispatch" possible.
"""

from __future__ import annotations

from datetime import UTC, datetime

# Epoch milliseconds land near 1e12. Epoch seconds land near 1e9. Anything this
# large is milliseconds. The cut is well clear of both.
_MS_THRESHOLD = 1e11


def now() -> datetime:
    return datetime.now(UTC)


def now_iso() -> str:
    return to_iso(now())


def to_iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(text: str) -> datetime:
    cleaned = text.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    moment = datetime.fromisoformat(cleaned)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


def to_epoch(value: object) -> float | None:
    """Parse an ISO string or an epoch number (seconds or milliseconds)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000.0 if abs(number) >= _MS_THRESHOLD else number
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return parse_iso(text).timestamp()
        except ValueError:
            pass
        try:
            return to_epoch(float(text))
        except ValueError:
            return None
    return None


def elapsed_seconds(start: str | None, end: str | None = None) -> float:
    """Seconds from `start` to `end`, or to now when `end` is None."""
    began = to_epoch(start)
    if began is None:
        return 0.0
    finished = to_epoch(end)
    if finished is None:
        finished = now().timestamp()
    return finished - began


def human_age(seconds: float) -> str:
    """A short age: 8s, 4m, 3h, 2d."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        return f"{total // 3600}h"
    return f"{total // 86400}d"


def human_duration(seconds: float) -> str:
    """A precise duration: 14s, 3m30s, 1h05m."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}s"
    return f"{total // 3600}h{(total % 3600) // 60:02d}m"
