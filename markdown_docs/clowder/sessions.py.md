# `clowder/sessions.py`

## Purpose

This module reads Pi session files so `clowder` can report real usage and cost
instead of guessing.

A Pi session is a JSON Lines file. Its first record names the session and its
working directory. Each assistant message carries usage, cost, provider and model
for that turn, so those values are read, never scraped from screen text.

One pane is a long-lived session, so a second task in the same pane shares the
same file. Every read therefore takes a `since` cut and an `until` cut: only turns
in `[since, until)` count toward this task, where `until` is the start of the next
task in the same pane. Without the upper cut, a later task's answer would be read
as this one's.

## Public API

### Dataclasses

```python
@dataclass
class Usage:
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
```

- `Usage` - tokens, cost and identity summed over the turns of one task.
  - `duration_seconds` (property) -> `float | None` - `last_at - first_at`, clamped
    at zero, or `None` when either end is missing.
  - `to_dict() -> dict[str, object]` - the dataclass as a plain dict.
  - `from_dict(cls, data) -> Usage` - rebuild a `Usage` from a dict, ignoring
    unknown keys.

```python
@dataclass
class ModelInfo:
    provider: str | None = None
    model: str | None = None
    thinking: str | None = None
```

- `ModelInfo` - the provider, model and thinking level a session runs on.
  - `label` (property) -> `str` - one line for a person, e.g.
    `anthropic/claude (thinking high)`.

### Functions

```python
def iter_records(path: str | Path) -> Iterator[dict[str, object]]
def read_usage(path, since=None, until=None) -> Usage | None
def read_model(path) -> ModelInfo | None
def read_answer(path, since=None, until=None) -> str | None
def extract_text(content: object) -> str | None
```

- `iter_records(path)` - yields each parseable JSON record. A torn or non-JSON last
  line is skipped, not fatal. An unreadable file yields nothing.
- `read_usage(path, since=None, until=None)` - sums usage over assistant turns in
  `[since, until)`. Returns `None` when the file is unreadable or holds no assistant
  turn.
- `read_model(path)` - the model a session runs, or `None` when the file says
  nothing. Later records win, so both a fresh session's opening `model_change` and
  an older session's assistant turns answer.
- `read_answer(path, since=None, until=None)` - the last prose the agent wrote in
  `[since, until)`. Tool calls and thinking are not the answer.
- `extract_text(content)` - joins the text parts of a message, ignoring thinking
  and tool calls. Returns `None` when there is no text.

Module constant:

- `TEXT_PARTS = ("text",)` - content part types that count as human-readable
  answer text.

## How It Works (internals)

Every private helper is listed under the public functions that call it.

### `read_usage(path, since, until)` - sum one task's turns

- `_int(value) -> int` - coerces a session-file value to a whole number, returning
  `0` for booleans, non-numbers and bad numeric strings. Session files are written
  by another program, so every field is treated as untrusted.
- `_float(value) -> float` - the same, for floats; returns `0.0` on anything it
  cannot convert.
- `_opt_str(value) -> str | None` - stringifies a value, returning `None` for
  `None` or an empty string. Used for `provider`, `model`, `session_id` and `cwd`.

`read_usage` walks the records and keeps only `"message"` records whose
`message.role` is `"assistant"`. It converts each turn's timestamp with
`to_epoch`, drops turns outside `[since, until)`, then accumulates the usage
fields. It tracks the earliest and latest turn times in `first_at` / `last_at`.
If no turn was counted it returns `None`.

### `read_model(path)` - the session's model

- `_opt_str(value) -> str | None` - as above.

`read_model` handles three record types: `model_change`, `thinking_level_change`,
and assistant `message` records. The newest non-empty value of each field wins, so
a message that carries no thinking level never blanks the previous one. It returns
`None` only when all three fields stayed empty.

### `read_answer(path, since, until)` - the last prose answer

- `_opt_str(value) -> str | None` - (used by `read_usage` / `read_model`, not by
  `read_answer` itself).

`read_answer` walks assistant `message` records in the time window and replaces its
running answer each time `extract_text` finds prose, so the last one wins. The
timestamp fallback order is `message.timestamp`, then the record's own `timestamp`.

### Internal utilities

- None beyond the three coercion helpers above; `extract_text` and the `Usage` /
  `ModelInfo` methods are public and hold their logic directly.
