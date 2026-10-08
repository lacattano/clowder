"""The audit file.

One JSON object per line, beside the state file. Every command that changes the
record in a way a person would need explained appends one line: what changed, why,
when and by whom. A record that can be edited with no trace is not a record.

The format is shared on purpose, so the queue drop (q-0036) and the state repair
write lines a reader can scan the same way.
"""

from __future__ import annotations

import json
from pathlib import Path


def audit_path(state_path: str | Path) -> Path:
    """The audit file that belongs to a state file: `state.json.audit`."""
    path = Path(state_path)
    return path.with_name(path.name + ".audit")


def append_audit(state_path: str | Path, record: dict[str, object]) -> Path:
    """Append one audit line, and return the file it went to."""
    path = audit_path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return path


def read_audit(state_path: str | Path) -> list[dict[str, object]]:
    """Every audit line, oldest first. A malformed line is an error, not a skip."""
    path = audit_path(state_path)
    if not path.is_file():
        return []
    rows: list[dict[str, object]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError(f"{path}:{number}: audit line is not an object")
        rows.append(payload)
    return rows


def read_actions(state_path: str | Path, action: str) -> list[dict[str, object]]:
    """Every line for one action, oldest first. For display, so it never raises.

    `read_audit` refuses a malformed line, because a record that cannot be read is
    a fault. The board is a view: it must draw with one hand-written line that is
    not JSON (there is one in the real file), so this skips what it cannot parse
    and keeps the well-formed lines for the action asked for.
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
