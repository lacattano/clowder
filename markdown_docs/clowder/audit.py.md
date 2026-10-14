# `clowder/audit.py`

## Purpose

This module reads and writes the audit file: one JSON object per line, sitting
beside the state file. Every command that changes the record in a way a person
would need explained appends one line - what changed, why, when and by whom. The
rule it enforces is stated at the top of the file: "A record that can be edited
with no trace is not a record."

The format is shared on purpose, so different writers (a queue drop, a state
repair) produce lines a reader can scan the same way.

## Public API

```python
def audit_path(state_path: str | Path) -> Path
def append_audit(state_path: str | Path, record: dict[str, object]) -> Path
def read_audit(state_path: str | Path) -> list[dict[str, object]]
def read_actions(state_path: str | Path, action: str) -> list[dict[str, object]]
```

- `audit_path(state_path)` - the audit file that belongs to a state file, named
  `state.json.audit` (the state file's name plus `.audit`).
- `append_audit(state_path, record)` - appends one JSON line (keys sorted, UTF-8)
  and returns the `Path` it wrote to. Creates the parent directory if needed.
- `read_audit(state_path)` - every audit line, oldest first. Returns `[]` when the
  file does not exist. A malformed line raises `ValueError`: a record that cannot
  be read is a fault.
- `read_actions(state_path, action)` - every line whose `action` field equals
  `action`, oldest first. Built for display, so it never raises: lines that are not
  parseable JSON are skipped.

## How It Works (internals)

This module has no `_`-prefixed helpers. Its logic is short and lives directly in
the four public functions.

The deliberate difference between the two readers:

- `read_audit` uses `json.loads` directly and raises on a bad line. It is the
  strict reader: the record must be intact.
- `read_actions` wraps `json.loads` in `try/except json.JSONDecodeError` and skips
  what it cannot parse. It is the view reader: the board must still draw even
  though the real audit file contains one hand-written, non-JSON line.

Both readers skip blank lines and both keep the file's original order.
`append_audit` and `audit_path` share the naming rule, so every writer and reader
agrees on where the audit lives.
