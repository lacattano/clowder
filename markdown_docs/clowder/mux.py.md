# `clowder/mux.py`

## Purpose

This module is the multiplexer adapter. It drives the terminal multiplexer (by
default `herdr`) by agent name, so the rest of `clowder` never talks to the
multiplexer directly.

Two design points come from the top of the file:

- Transport uses the multiplexer's own names. Nothing here stores a session
  address, because addresses move when panes reload while names do not.
- The live agent list *is* the roster, so there is no roster file to maintain. The
  list also returns each agent's Pi session file, which is how a report finds its
  own worker's usage without guessing.

## Public API

### Dataclasses

```python
@dataclass
class AgentInfo:
    name: str
    pane_id: str | None = None
    cwd: str | None = None
    workspace_id: str | None = None
    status: str | None = None
    session_path: str | None = None
    focused: bool = False


@dataclass
class MuxResult:
    argv: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
```

- `AgentInfo` - one agent as the multiplexer sees it right now.
- `MuxResult` - what happened when a command was run.
  - `ok` (property) -> `bool` - true when the command returned zero and did not
    time out.
  - `error_text() -> str` - a short human message for a failed run (the tail of
    stderr, or a timeout note).

### `Mux` class

Drives one multiplexer binary by agent name.

```python
def __init__(self, binary, prompt_argv, bus_send_argv=None, bus_peers_argv=None)
def build_prompt_argv(self, agent, brief) -> list[str]
def build_bus_send_argv(self, agent, brief) -> list[str]
def prompt(self, agent, brief, timeout_s=60.0, wait=False, until=()) -> MuxResult
def list_agents(self, timeout_s=15.0) -> list[AgentInfo]
def find_agent(self, name, timeout_s=15.0) -> AgentInfo | None
def build_split_argv(self, cwd, direction="right", focus=False, env=None) -> list[str]
def split_pane(self, cwd, direction="right", focus=False, env=None, timeout_s=20.0) -> str
def move_pane(self, pane_id, workspace_id=None, focus=False, timeout_s=20.0) -> MuxResult
def close_pane(self, pane_id, timeout_s=20.0) -> MuxResult
def build_send_keys_argv(self, target, keys) -> list[str]
def send_keys(self, target, keys, timeout_s=20.0) -> MuxResult
def run_in_pane(self, pane_id, command, timeout_s=20.0) -> MuxResult
def pane_foreground(self, pane_id, timeout_s=15.0) -> str | None
def wait_for_output(self, pane_id, pattern, timeout_s, *, regex=False, source="visible", lines=None) -> MuxResult
def bus_send(self, agent, brief, timeout_s=60.0) -> MuxResult
def bus_peers(self, timeout_s=15.0) -> list[tuple[str, str]] | None
def is_bus_joined(self, agent, cwd=None, timeout_s=15.0) -> bool | None
def read_input(self, target, timeout_s=15.0) -> str | None
def start_agent(self, name, pane_id, kind="pi", timeout_s=None, agent_args=()) -> MuxResult
def wait_for_agent(self, name, until, timeout_s) -> MuxResult
def rename_agent(self, old, new, timeout_s=20.0) -> MuxResult
```

Highlights:

- `prompt(agent, brief)` - submits a brief. Returns as soon as the multiplexer
  accepts it. `wait` and `until` control whether it also waits for agent states.
- `list_agents()` - asks the multiplexer which agents exist; this is the live
  roster. `find_agent(name)` returns one or `None`.
- `split_pane(cwd, ...)` - opens a sibling pane whose working directory is `cwd`,
  and returns the new pane id. `move_pane` / `close_pane` are the companions.
- `wait_for_output(pane_id, pattern, timeout_s, ...)` - waits until a pane's output
  matches `pattern`, or the timeout runs out. `regex` and `source` tune the match.
- `is_bus_joined(agent, cwd=None)` - `True`/`False`, or `None` when it cannot tell.
- `read_input(target)` - the text sitting in an agent's input box; `None` when it
  cannot be read.
- `start_agent(name, pane_id, kind="pi", ...)` - starts an agent in a pane that is
  at a shell prompt.

### Module functions

```python
def reply_pane_id(result: MuxResult) -> str | None
def input_line(snapshot: str) -> str | None
def parse_peer_addresses(output: str) -> list[tuple[str, str]]
def peer_joined(peers, agent, cwd=None) -> bool
def parse_agent_list(output: str) -> list[AgentInfo]
```

- `reply_pane_id(result)` - the pane id a multiplexer reply names, if it names one.
- `input_line(snapshot)` - the text in a Pi pane's input box, read from a screen
  snapshot. Returns `None` when no input line can be found.
- `parse_peer_addresses(output)` - reads `remote-pi peers` output as `(path, name)`
  pairs.
- `peer_joined(peers, agent, cwd=None)` - whether `agent` is on the bus. With a
  `cwd`, the folder must match too.
- `parse_agent_list(output)` - reads the multiplexer's agent list into
  `AgentInfo` objects.

Module constants: `MAX_BRIEF_CHARS = 8000`; `_RULE_CHARS` (private) is the set of
characters the Pi editor draws as rules.

## How It Works (internals)

Each helper is listed under the public function that calls it.

### `Mux.split_pane(...)`, `Mux.pane_foreground(...)`, `reply_pane_id(...)` - read pane ids from JSON

- `_dig(node, *keys) -> object | None` - walks a nested JSON payload, e.g.
  `_dig(payload, "result", "pane", "pane_id")`. Returns `None` as soon as a level is
  not a dict, so a shape change yields no answer instead of an exception.

### `Mux._run(...)` - run one multiplexer command

- `_as_text(value) -> str` - coerces captured output (bytes, `None` or a string) to
  a string, replacing undecodable bytes.

`_run` is the one place that calls `subprocess.run`: it captures stdout/stderr as
UTF-8, measures the elapsed time, and converts a timeout into a `MuxResult` with
`timed_out=True`. A missing binary becomes a `MuxError` that names the `mux.bin`
config key.

### `input_line(snapshot)` - read the input box from a screen

- `_is_border(line) -> bool` - true when a line is an editor border: either all
  rule characters, or a short label between two runs of rule characters (the
  "Working" spinner line).

`input_line` scans a pane snapshot for the input box, using `_is_border` to skip
the chrome around it.

### `peer_joined(peers, agent, cwd=None)` - is this agent on the bus?

- `_normalize_path(value) -> str` - normalizes a peer path for comparison: slashes
  to backslashes, trailing backslashes removed, lowercased (Windows-friendly).

### `Mux._result(...)`, `MuxResult.error_text()`, `parse_agent_list(...)`, `reply_pane_id(...)` - parse multiplexer output

- `_first_json(output) -> object | None` - finds the first JSON value in command
  output: the whole text if it parses, otherwise the first line that starts with
  `{` or `[` and parses. Returns `None` when there is none.

### `parse_agent_list(output)` - build the roster

- `_find_agents(node, depth=0) -> list[object] | None` - searches a nested payload
  (up to depth 6) for a list under the key `"agents"`, so a wrapper change in the
  multiplexer's reply still yields the list.
- `_session_path(record) -> str | None` - finds an agent's Pi session file. It
  prefers the structured `agent_session.value` field, then falls back to any string
  value ending in `.jsonl`. A pane can be reset to a new session, so the newest
  record wins.
- `_opt_str(value) -> str | None` - `None`/empty -> `None`, otherwise `str(value)`.

### Private methods

- `Mux._fill(parts, agent, brief) -> list[str]` - substitutes `{agent}` and
  `{brief}` in a command template. If no part contained `{brief}`, the brief is
  appended as the last argument. Raises `MuxError` when the brief is longer than
  `MAX_BRIEF_CHARS`. Used by `build_prompt_argv` and `build_bus_send_argv`.
- `Mux._run(argv, timeout_s) -> MuxResult` - as above; the common runner.
- `Mux._result(argv, timeout_s) -> object` - runs a command that must succeed and
  returns its parsed JSON. Raises `MuxError` when the run fails or the reply is not
  JSON.
