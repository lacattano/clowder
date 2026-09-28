"""Reading Pi session files.

A Pi session is JSON Lines. The first record names the session and its cwd.
Assistant messages carry usage, cost, provider and model per turn. So usage and
cost are read, never scraped.

Because one pane is a long-lived session, a second task in the same pane shares
a file. Every read therefore takes a `since` cut, and only turns after this
dispatch count towards this task.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .timeutil import to_epoch

# Content part types that hold text a human could read as the answer.
TEXT_PARTS = ("text",)


@dataclass
class Usage:
    """Tokens, cost and identity summed over the turns of one task."""

    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0
    total_tokens: int = 0
    cost_total: float = 0.0
    provider: str | None = None
    model: str | None = None
    session_id: str | None = None
    cwd: str | None = None
    first_at: float | None = None
    last_at: float | None = None

    @property
    def duration_seconds(self) -> float | None:
        if self.first_at is None or self.last_at is None:
            return None
        return max(0.0, self.last_at - self.first_at)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Usage:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})  # type: ignore[arg-type]


def iter_records(path: str | Path) -> Iterator[dict[str, object]]:
    """Yield each parseable record. A torn last line is skipped, not fatal."""
    try:
        handle = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def read_usage(path: str | Path, since: float | None = None) -> Usage | None:
    """Sum usage over assistant turns at or after `since`.

    Returns None when the file is unreadable or holds no assistant turn.
    A session that exists but has not answered yet is not usage.
    """
    usage = Usage()
    for record in iter_records(path):
        kind = record.get("type")
        if kind == "session":
            usage.session_id = _opt_str(record.get("id")) or usage.session_id
            usage.cwd = _opt_str(record.get("cwd")) or usage.cwd
            continue
        if kind != "message":
            continue
        message = record.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue

        moment = to_epoch(message.get("timestamp")) or to_epoch(record.get("timestamp"))
        if since is not None and moment is not None and moment < since:
            continue

        turn = message.get("usage")
        if not isinstance(turn, dict):
            turn = {}

        usage.turns += 1
        usage.input_tokens += _int(turn.get("input"))
        usage.output_tokens += _int(turn.get("output"))
        usage.cache_read += _int(turn.get("cacheRead"))
        usage.cache_write += _int(turn.get("cacheWrite"))
        usage.reasoning += _int(turn.get("reasoning"))
        usage.total_tokens += _int(turn.get("totalTokens"))
        cost = turn.get("cost")
        if isinstance(cost, dict):
            usage.cost_total += _float(cost.get("total"))

        usage.provider = _opt_str(message.get("provider")) or usage.provider
        usage.model = _opt_str(message.get("model")) or usage.model

        if moment is not None:
            usage.first_at = moment if usage.first_at is None else min(usage.first_at, moment)
            usage.last_at = moment if usage.last_at is None else max(usage.last_at, moment)

    if usage.turns == 0:
        return None
    return usage


def read_answer(path: str | Path, since: float | None = None) -> str | None:
    """The last thing the agent said in prose.

    Tool calls and thinking are not the answer. Only text parts are.
    """
    answer: str | None = None
    for record in iter_records(path):
        if record.get("type") != "message":
            continue
        message = record.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        moment = to_epoch(message.get("timestamp")) or to_epoch(record.get("timestamp"))
        if since is not None and moment is not None and moment < since:
            continue
        text = extract_text(message.get("content"))
        if text:
            answer = text
    return answer


def extract_text(content: object) -> str | None:
    """Join the text parts of a message, ignoring thinking and tool calls."""
    chunks: list[str] = []
    if isinstance(content, str):
        stripped = content.strip()
        return stripped or None
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") not in TEXT_PARTS:
                continue
            value = part.get("text")
            if isinstance(value, str) and value.strip():
                chunks.append(value.strip())
    if not chunks:
        return None
    return "\n".join(chunks)


def _int(value: object) -> int:
    """A whole number, or 0. Session files are written by another program."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:  # the string was not a number
            return 0
    return 0


def _float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:  # the string was not a number
            return 0.0
    return 0.0


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
