"""The audit file.

One JSON object per line, beside the state file. Every command that changes the
record in a way a person would need explained appends one line: what changed, why,
when and by whom. A record that can be edited with no trace is not a record.

The format is shared on purpose, so the queue drop (q-0036) and the state repair
write lines a reader can scan the same way.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .errors import StateError
from .timeutil import parse_iso


def audit_path(state_path: str | Path) -> Path:
    """The audit file that belongs to a state file: `state.json.audit`."""
    path = Path(state_path)
    return path.with_name(path.name + ".audit")


def append_audit(state_path: str | Path, record: dict[str, object]) -> Path:
    """Append one audit line, and return the file it went to.

    The line reaches the disk before this returns, so a caller that treats it as the
    trail for a change can write the change afterwards and know the trail is already
    there. An unwritable file is a clowder error naming the path, not a traceback:
    the caller has to be able to say that the change did not happen.
    """
    path = audit_path(state_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise StateError(f"{path}: cannot write the audit line: {exc}") from exc
    return path


def _legacy_row(text: str, number: int, reason: str) -> dict[str, object]:
    """A line the tool cannot parse, kept as one named row.

    The trail is the record of what happened, including the lines a person wrote by
    hand before the format was enforced. A reader that refuses the whole file for
    one hand-written line hides every fact after it, so an unreadable line becomes a
    `legacy` row that carries its own text, its line number and why it could not be
    parsed. Nothing is dropped and nothing is guessed.
    """
    row: dict[str, object] = {"action": "legacy", "line": number, "text": text, "why": reason}
    first = text.split(" ", 1)[0]
    try:
        parse_iso(first)
    except ValueError, TypeError:
        pass  # a line with no timestamp keeps none; the line number still places it
    else:
        row["at"] = first  # the exact token, not a normalised copy of it
    return row


def read_audit(state_path: str | Path) -> list[dict[str, object]]:
    """Every audit line, oldest first. A malformed line is kept as a `legacy` row.

    The file holds hand-written lines from before the format was enforced, and a
    record that cannot be read is worse than a record with a line that needs a
    person's eye. An unparseable line, or a JSON value that is not an object, comes
    back as `{"action": "legacy", ...}` with the original text and its line number,
    so every caller sees the whole trail and none of them can die on it.
    """
    path = audit_path(state_path)
    if not path.is_file():
        return []
    rows: list[dict[str, object]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            rows.append(_legacy_row(text, number, "not JSON"))
            continue
        if not isinstance(payload, dict):
            rows.append(_legacy_row(text, number, "JSON, but not an object"))
            continue
        rows.append(payload)
    return rows


def read_actions(state_path: str | Path, action: str) -> list[dict[str, object]]:
    """Every line for one action, oldest first. For display, so it never raises.

    A hand-written line that is not JSON is not a fault to a view: it is skipped
    here. `read_audit` returns it as a named `legacy` row, so a reader that
    reconstructs the trail still sees it.
    """
    path = audit_path(state_path)
    if not path.is_file():
        return []
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("action") == action:
            rows.append(payload)
    return rows
