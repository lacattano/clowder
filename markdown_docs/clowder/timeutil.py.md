# `clowder/timeutil.py`

## Purpose

This module is the tool's clock. Two time systems meet here:

- Records that `clowder` writes use ISO 8601 UTC strings, e.g. `2026-10-13T18:00:00Z`.
- Messages from Pi sessions carry epoch numbers, sometimes in seconds and sometimes
  in milliseconds.

`timeutil` parses both into one floating-point epoch so they can be compared. That
comparison is what makes questions like "how much usage since this dispatch?"
answerable even when the two timestamps came from different sources.

It also formats ages and durations for humans, e.g. `4m` or `1h05m`.

## Public API

```python
def now() -> datetime
def now_iso() -> str
def to_iso(moment: datetime) -> str
def parse_iso(text: str) -> datetime
def to_epoch(value: object) -> float | None
def elapsed_seconds(start: str | None, end: str | None = None) -> float
def human_age(seconds: float) -> str
def human_duration(seconds: float) -> str
```

- `now()` - the current time as a timezone-aware `datetime` in UTC.
- `now_iso()` - the current time as an ISO 8601 UTC string.
- `to_iso(moment)` - converts any `datetime` to the canonical string form
  (UTC, seconds precision, `Z` suffix). Returns `str`.
- `parse_iso(text)` - parses an ISO string into a timezone-aware `datetime`. A
  naive string is assumed to be UTC. Returns `datetime`; raises `ValueError` on bad
  input.
- `to_epoch(value)` - parses an ISO string, an epoch second number, or an epoch
  millisecond number into a `float` epoch in seconds. Returns `None` when the value
  is `None`, a `bool`, empty, or unparseable.
- `elapsed_seconds(start, end=None)` - whole seconds from `start` to `end` (or to
  now when `end` is `None`). Returns `0.0` when `start` cannot be parsed.
- `human_age(seconds)` - a short age label: `8s`, `4m`, `3h`, `2d`.
- `human_duration(seconds)` - a precise label: `14s`, `3m30s`, `1h05m`.

## How It Works (internals)

This module has no `_`-prefixed helpers. Its logic is short and lives directly in
the public functions, with one module-level constant:

- `_MS_THRESHOLD = 1e11` - the cut that separates epoch seconds (about `1e9`) from
  epoch milliseconds (about `1e12`). Both are far from the cut, so the guess is
  safe. `to_epoch` divides by `1000` when a number is at or above this value.

Worth knowing:

- `to_epoch` is recursive for string input: it first tries `parse_iso`, then falls
  back to `to_epoch(float(text))`. This lets a numeric string be treated as an
  epoch number while still accepting ISO strings.
- `bool` is checked before `int`/`float` on purpose, because `bool` is a subclass of
  `int` and `True` should not become `1.0`.
- `elapsed_seconds` clamps a missing start to `0.0` rather than raising, so callers
  can call it on a record that has no timestamp without a guard.
