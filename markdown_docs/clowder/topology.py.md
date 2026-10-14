# `clowder/topology.py`

## Purpose

This module makes sure an agent exists where a job belongs.

A multiplexer pane is given its working directory when it is made and cannot be
moved later. So a pane *is* a repo: its shell, its context files and its skills all
come from that directory. A job for repo B cannot be handed to a pane sitting in
repo A.

There are only two ways out: make an agent in repo B, or ask the human. This
module tries the first and reports clearly when it falls back to the second. It
also derives safe agent names, finds an existing agent that already serves a repo,
prepares the agent's worktree, and writes the pane's remote-pi config so its Pi
joins the relay on start.

## Public API

```python
def sanitise(text: str, limit: int = MAX_NAME) -> str
def derive_name(repo_name: str, role: str = DEFAULT_ROLE) -> str
def normalise(path: str | Path) -> str
def inside(child: str | Path | None, root: str | Path) -> bool
def agents_in_repo(agents: list[AgentInfo], repo_path: str | Path) -> list[AgentInfo]
def workspace_for_repo(agents: list[AgentInfo], repo_path: str | Path) -> str | None
def role_of(name: str) -> str | None
def match_agent(agents, repo_path, repo_name, role) -> AgentInfo | None
def worktree_path(repo_path: str | Path, worktree_dir: str, name: str) -> Path
def write_remote_pi_config(workdir: str | Path, name: str) -> Path | None
def ensure_agent(mux, repo_name, repo_path, role="maker", kind="pi",
                 direction="right", create=True, name=None, worktree_dir=".worktrees",
                 base=None, setup=None, identity=None, split=False) -> EnsureResult
def requested_name(name: str) -> str
```

- `sanitise(text, limit=32)` - makes a string a multiplexer-safe name: lowercases,
  replaces runs of non-alphanumerics with `-`, trims dashes, ensures a leading
  letter (`"a"` is prepended if not) and cuts to `limit`. Never returns `""`.
- `derive_name(repo_name, role="maker")` - `cat-tan-trading` + `maker` ->
  `cat-tan-trading-maker`, trimmed to fit 32 characters.
- `normalise(path)` - a comparable form of a path for the case-insensitive Windows
  filesystem: strips the `\\?\` prefix, then `normcase(abspath(...))`.
- `inside(child, root)` - is `child` inside `root`? Returns `False` when there is no
  answer (no child, or two different drives).
- `agents_in_repo(agents, repo_path)` - the agents whose `cwd` is inside the repo.
- `workspace_for_repo(agents, repo_path)` - the workspace id an agent already in
  this repo lives in, or `None`. Names are sorted so the choice does not depend on
  the multiplexer's listing order.
- `role_of(name)` - which role a name plays when it says so: `maker`,
  `tancat-verifier`, `myrepo-maker` all name a role; `odd-name` does not.
- `match_agent(agents, repo_path, repo_name, role)` - picks the existing agent that
  serves this repo, or `None` to make one. Order: the derived name, then a name
  that is or ends in the role, then the only agent in the repo when its name names
  no other role. It stops at another role on purpose: asking for a verifier where
  only a maker exists must make a verifier, not quietly reuse the maker.
- `worktree_path(repo_path, worktree_dir, name)` - `<repo>/<worktree_dir>/<name>`.
- `write_remote_pi_config(workdir, name)` - writes
  `<workdir>/.pi/remote-pi/config.json` with `agent_name` and
  `auto_start_relay: true`, keeping any existing values. Returns the `Path`, or
  `None` when there is no folder. An existing `auto_start_relay` is kept, so a pane
  deliberately taken off the relay stays off.
- `ensure_agent(...)` - the main entry point. Returns an `EnsureResult`: an agent
  that serves the repo, making one if allowed. `name` forces an exact name (and
  never falls back to a role match). `split=True` keeps the old
  split-beside-the-caller placement; otherwise the pane moves to its repo's
  workspace (or a new tab). `identity`, when given, is set in the new pane's
  environment so its commits carry the crew's identity.
- `requested_name(name)` - validates an explicitly requested name against
  `VALID_NAME`. Raises `UsageError` on a bad name.

### `EnsureResult` (dataclass)

What happened when the tool looked for an agent.

Fields: `agent: AgentInfo | None`, `created: bool`, `reason: str | None`,
`candidates: list[str]`, `worktree: str | None`, `note: str | None`.

- `ok` (property) -> `bool` - `agent is not None`.
- `as_dict() -> dict[str, object]` - the result as plain data, including a summary
  of the agent (name, pane id, cwd, status, session file).

Module constants: `DEFAULT_KIND = "pi"`, `DEFAULT_ROLE = "maker"`,
`DEFAULT_DIRECTION = "right"`, `MAX_NAME = 32`, `NAME_RUNS`, `VALID_NAME`,
`ROLES = ("maker","verifier","teacher","researcher")`, `REMOTE_PI_CONFIG`.

## How It Works (internals)

The single private helper is listed under the public function that calls it.

### `ensure_agent(...)` - find or make a serving agent

- `_prepare_worktree(repo_path, name, worktree_dir, base, setup) -> tuple[Path, str | None]`
  - makes the agent's own space, or explains why it will share the repo. If the
  repo is not a git repository it returns the checkout itself plus a note. If the
  target worktree already exists it is reused. Otherwise it resolves the base ref,
  calls `gitcmd.add_worktree_free(...)`, then runs the optional `setup` command in
  the new space.

The `ensure_agent` flow passes through, in order: list agents and candidates;
resolve an explicit name or match a derived one; refuse to reuse a live name;
refuse to guess when several agents serve the repo and none matches the role;
bail if `create` is false; prepare the worktree; write the remote-pi config; find
a workspace; split the pane; move it to the workspace (closing the split to undo
it if the move fails); start the agent; then re-list agents to prove the name is
live before returning success. The helper functions above (`inside`,
`derive_name`, `match_agent`, `workspace_for_repo`, `write_remote_pi_config`,
`worktree_path`, `normalise`, `requested_name`) do the small checks it uses.

There are no other `_`-helpers. `sanitise`, `normalise`, `inside`, `role_of` and
`match_agent` hold their short logic directly.
