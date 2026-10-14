# `clowder/config.py`

## Purpose

This module reads `clowder`'s configuration. The design idea is stated at the top
of the file: "The tool is the repo. One person's setup - repos, models, agents,
transport - is config, and lives outside the repo." Nothing personal is baked in.
A missing workspace root is an error, not a guess.

`load_config` finds a TOML file, checks each section, applies environment
overrides, and returns one immutable `Config` object where every path is absolute
and expanded. Everything downstream reads settings from that object instead of
re-reading files.

## Public API

```python
def default_home() -> Path
def find_config_file(explicit: str | Path | None = None) -> Path | None
def load_config(explicit: str | Path | None = None) -> Config
```

- `default_home()` - where `clowder` keeps its own state when config says nothing
  else. Returns `$CLOWDER_HOME` if set, otherwise `~/.clowder`.
- `find_config_file(explicit=None)` - locates the config in this order: the
  explicit path, `$CLOWDER_CONFIG`, `clowder.config.toml` / `clowder.toml` in the
  current directory, then the same names in `default_home()`. Returns a `Path`, or
  `None` when no file exists. Raises `ConfigError` when an explicit or env path
  points at a missing file.
- `load_config(explicit=None)` - builds and returns a `Config`. The environment
  beats the file (see `CLOWDER_*` variables below). Raises `ConfigError` on bad
  TOML, a bad section shape, an unknown key, or an invalid value.

### `Config` (frozen dataclass)

Resolved settings; every path is absolute and expanded.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `home` | `Path` | - | Default state directory. |
| `source` | `Path \| None` | - | The config file read, or `None`. |
| `workspace_root` | `Path \| None` | - | Where named repos are looked up. |
| `mux_bin` | `str` | `herdr` | The multiplexer command. |
| `mux_prompt_argv` | `tuple[str, ...]` | `("agent","prompt","{agent}","{brief}")` | Command template for typing a brief. |
| `sessions_root` | `Path` | `~/.pi/agent/sessions` | Where Pi session files live. |
| `state_path` | `Path` | - | The JSON state file. |
| `repos` | `dict[str, Path]` | - | Named repo -> path. |
| `dispatch_marker` | `bool` | `True` | Prepend the dispatch marker. |
| `dispatch_input_wait` | `float` | `1.5` | Seconds to wait for a worker's input box to clear. |
| `bus_send_argv` | `tuple[str, ...] \| None` | `None` | Agent-bus send command template. |
| `bus_peers_argv` | `tuple[str, ...] \| None` | `None` | Agent-bus peer-list command template. |
| `front_door_name` | `str \| None` | `None` | The front door agent's name. |
| `worktree_dir` | `str` | `.worktrees` | Folder that holds worker spaces. |
| `worktree_base` | `str \| None` | `None` | Base ref for new worktrees. |
| `worktree_setup` | `str \| None` | `None` | Setup command run in a new worktree. |
| `job_branch_prefix` | `str` | `task/` | Prefix for job branches. |
| `delivery_mode` | `str` | `local-only` | One of `local-only`, `direct-PR`, `no-mistakes`. |
| `git_name` | `str` | `clowder-bot` | Commit author name. |
| `git_email` | `str` | (bot address) | Commit author email. |
| `heavy_max_memory` | `float \| None` | `None` | Optional heavy-step memory threshold. |
| `heavy_max_load` | `float \| None` | `None` | Optional heavy-step load threshold. |

Methods:

- `resolve_repo(name) -> Path` - turns a repo name into a path. A name present in
  `repos` wins. A string that looks like a path (absolute, starts with `.`, or
  contains `/` or `\`) is used as-is. Otherwise it is resolved under
  `workspace_root`; with no root set this raises `ConfigError`.
- `as_dict() -> dict[str, object]` - the settings as plain data, ready to print or
  serialize.

Module constants: `DEFAULT_MUX_BIN`, `DEFAULT_MUX_PROMPT_ARGV`, `STATE_FILENAME`,
`DEFAULT_DELIVERY`, `DELIVERY_MODES`, `BUILT_DELIVERY_MODES`, `DEFAULT_GIT_NAME`,
`DEFAULT_GIT_EMAIL`, `SEARCH_NAMES`.

Environment overrides read by `load_config`: `CLOWDER_HOME`, `CLOWDER_CONFIG`,
`CLOWDER_WORKSPACE`, `CLOWDER_MUX_BIN`, `CLOWDER_SESSIONS_ROOT`, `CLOWDER_STATE`,
`CLOWDER_GIT_NAME`, `CLOWDER_GIT_EMAIL`.

## How It Works (internals)

Each helper is listed under the public function that calls it.

### `Config.resolve_repo(name)` - name to path

- `_require_dir(path, label) -> Path` - expands `~`, makes the path absolute, and
  raises `ConfigError` when it is not a directory. Always absolute on purpose: a
  recorded `repo_path` must not depend on the working directory it was recorded in.

### `load_config(explicit=None)` - build the `Config`

- `_table(raw, name, source) -> dict[str, object]` - returns one `[section]` of the
  config file, or `{}`. It is the single place that insists a section is a table,
  so every reader below can trust the shape. Raises `ConfigError` otherwise.
- `_opt_path(value) -> Path \| None` - `None`/`""` -> `None`; otherwise an
  expanded, absolute `Path`.
- `_opt_str(value) -> str \| None` - `None`/`""` -> `None`; otherwise `str(value)`.
- `_opt_number(value, label, source) -> float \| None` - validates an optional gate
  threshold. Rejects booleans, non-numbers, non-finite values (`nan`/`inf`) and
  negatives with a `ConfigError` that names the file and key.
- `_opt_argv(value, label, source) -> tuple[str, ...] \| None` - validates a
  command template: a list of strings becomes a tuple, an empty list becomes
  `None`, anything else is a `ConfigError`.

The `load_config` flow: find the file; parse TOML (turning a decode error into a
`ConfigError` with a Windows-path hint); pull each section through `_table`; apply
defaults and env overrides; validate unknown keys for the `bus`, `worktree`, `git`
and `gates` sections; then construct the frozen `Config`. `default_home()` and
`find_config_file()` are called at the top of that flow.

There are no other `_`-helpers. `find_config_file` and `default_home` hold their
short logic directly.
