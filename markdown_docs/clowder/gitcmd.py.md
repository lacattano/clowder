# `clowder/gitcmd.py`

## Purpose

This module calls git directly. The tool needs only four things from git: is this
tree clean, which branch is it on, which commit, and put that branch here.
Everything else is the human's business - the tool never commits, pushes or merges
on its own. A report is not an approval, and a person reads the diff before it
goes anywhere.

`gitcmd` wraps git in two primitives (`run_git` and `try_git`) and then builds
read-only helpers, branch/worktree actions, publish guards, and a small cache
(`RiskMemo`) on top. Every function takes a working directory as its first
argument.

## Public API

### Dataclasses

```python
@dataclass(frozen=True)
class CopyState:
    path: str
    branch: str | None = None
    commit: str | None = None
    dirty: bool = False
    detached: bool = False
    is_repo: bool = True
    main_checkout: bool = False


@dataclass
class WorktreeInfo:
    path: str
    branch: str | None
    commit: str | None
    detached: bool = False
    bare: bool = False


@dataclass(frozen=True)
class Identity:
    name: str
    email: str


@dataclass(frozen=True)
class CommitAuthorship:
    commit: str
    author: str
    author_email: str
    committer: str
    committer_email: str
```

- `CopyState` - what one folder currently holds; its point is telling folders
  apart. `label` (property) -> a short human label.
- `WorktreeInfo` - one worktree as git lists it. `label` (property) -> a short
  human label.
- `Identity` - the commit identity the crew uses in every repo. `env()` ->
  `dict[str, str]` pins git's author and committer, config or not.
- `CommitAuthorship` - who git says wrote and committed one save.

### Running git

```python
def run_git(cwd, *args, timeout_s=120.0, env=None) -> str
def try_git(cwd, *args, timeout_s=120.0, env=None) -> str | None
def run_command(command: str, cwd, timeout_s=900.0) -> None
```

- `run_git(...)` - runs git and insists it worked. `env` is merged over the current
  environment. Raises `GitError` on a missing binary, timeout, OS error, or a
  non-zero exit.
- `try_git(...)` - the same, but a failure returns `None` instead of raising. Used
  where a failure is an answer.
- `run_command(command, cwd)` - runs a configured setup command. Uses `shlex` (no
  shell), so shell operators are not interpreted and cannot trip the caller.

### Reading a repo

```python
def is_repo(path) -> bool
def toplevel(path) -> str | None
def status_entries(path) -> list[str]
def is_clean(path) -> bool
def current_branch(path) -> str | None
def head_commit(path, short=True) -> str | None
def commit_of(path, ref, short=False) -> str | None
def is_detached(path) -> bool
def count_commits(path, rev_range) -> int
def is_ancestor(path, ancestor, descendant) -> bool
def merge_base(path, left, right) -> str | None
def is_linked_worktree(path) -> bool
def describe_copy(path) -> CopyState
def branch_exists(path, branch) -> bool
def local_branches(path) -> list[str]
def has_remote(path, remote="origin") -> bool
def remote_branch_commit(path, base, remote="origin") -> str | None
```

- `status_entries` returns `git status --porcelain`, one entry per changed or
  untracked path. It excludes exactly `.pi/remote-pi/config.json`, the local file
  the tool writes so a pane joins the relay; without that, `job open` would refuse
  the space the tool just made.
- `writer_commits_since(path, passed, tip, exclude=None) -> int | None` - commits
  after `passed` that are writer content, not the base coming in.
- `is_ancestor` and `merge_base` are the single calls used to test history.
- `describe_copy(path)` returns everything needed to tell this folder apart from
  another agent's folder.

### Base and worktrees

```python
def fetch(path, remote="origin", timeout_s=120.0) -> bool
def commits_behind(path, local, remote_ref) -> int | None
def branch_behind(path, branch, remote="origin") -> int | None
def resolve_base(path, preferred=None) -> str
def list_worktrees(repo) -> list[WorktreeInfo]
def switch_new_branch(path, branch, base) -> None
def switch_branch(path, branch) -> None
def switch_to_commit(path, commit) -> None
def detach_at(path, ref) -> None
def add_worktree(repo, target, branch, base) -> None
def add_worktree_free(repo, target, ref) -> None
def remove_worktree(repo, target) -> None
def move_worktree(path, target) -> None
def delete_branch(path, branch) -> None
```

- `resolve_base(path, preferred=None)` - the branch a new branch starts from, using
  `BASE_CANDIDATES` (`main`, `master`, `trunk`, `develop`) when none is preferred.
- `add_worktree_free` makes a worktree with no branch name on it: a space ready to
  lend out. `detach_at` frees a folder back to that resting state.
- `move_worktree` moves a linked worktree, running git from the parent because on
  Windows a folder that is a process's current directory cannot be renamed.
- `delete_branch` uses `branch -d`, so git refuses to delete an unmerged branch on
  purpose.

### Merge, risk and publish guards

```python
def unmerged_paths(path) -> list[str]
def merge_branch(path, ref, message, identity, timeout_s=120.0) -> tuple[bool, list[str]]
def branches_containing(path, commit, remotes=True) -> list[str]
def is_reachable(path, commit) -> bool
def patch_id(path, commit, timeout_s=120.0) -> str | None
def commit_risk(path, commit) -> str
def refs_fingerprint(path) -> str | None
def foreign_commits(path, rev_range, identity, *, exclude_remote=True) -> list[CommitAuthorship]
def refuse_foreign_authors(path, rev_range, identity, *, exclude_remote=True) -> None
def refuse_rewritten_base(path, base_commit, base, remote="origin") -> None
def push_branch(path, branch, remote="origin") -> None
def hold_ref(path, name, commit) -> str
```

- `merge_branch` merges one ref into the current branch under the crew identity and
  returns `(ok, conflicted_paths)`.
- `commit_risk` tells a lost change apart from one that only looks lost. Its
  answers are `ON_BRANCH`, `LOST`, `EMPTY`, `MERGE` or `UNKNOWN`.
- `refs_fingerprint` is a cheap hash of the refs that decide whether a commit is
  reachable.
- `refuse_foreign_authors` / `refuse_rewritten_base` are publish guards: they raise
  `GitError` rather than let a non-crew commit or a rewritten base be published.
- `push_branch` publishes one branch; the caller has already checked the owner's
  pass. `hold_ref` points a local ref at a saved commit so it outlives its branch.

Module constants: `BASE_CANDIDATES`, `DEFAULT_TIMEOUT_S`, the risk words
`ON_BRANCH`/`LOST`/`EMPTY`/`MERGE`/`UNKNOWN`, `RISK_FINGERPRINT_FORMAT`,
`RISK_MEMO_FILENAME`, `MAX_MEMO_ANSWERS_PER_FOLDER`.

### `RiskMemo` - cache `commit_risk` answers

```python
def __init__(self, path=None)
def load(self) -> None
def risk(self, path, commit) -> str
def save(self) -> None
```

A `commit_risk` answer is a pure function of the commit and the refs that hold it,
so a board write asking about many tasks can do about half the git work. The memo
covers one write and is carried between writes by a saved refs fingerprint. It is
a cache, not a record: losing it costs one slow write and nothing else.

## How It Works (internals)

Each helper is listed under the public function that calls it.

### `toplevel(path)` and `list_worktrees(repo)` - clean up git's answers

- `_clean_path(text) -> str` - strips the Windows extended-length prefix `\\?\`
  that git for Windows can return, so the path matches other code.

### `commit_risk(path, commit)` - lost, or just looks lost?

- `_parents(path, commit) -> list[str] | None` - a commit's parent hashes, or
  `None` when the commit cannot be read.
- `_is_empty_change(path, commit) -> bool | None` - does this commit change no
  file? `None` when it cannot be read.

`commit_risk` uses those plus `patch_id`, `branches_containing` and `is_reachable`
to decide between `ON_BRANCH`, `LOST`, `EMPTY`, `MERGE` and `UNKNOWN`.

### `RiskMemo` - the per-write cache

- `load()` - reads the memo file if present. A missing or unreadable file is a cold
  memo, not an error (best-effort cache).
- `risk(path, commit)` - returns a remembered answer while the folder's refs
  fingerprint is unchanged; otherwise re-reads the fingerprint, drops stale
  answers, and recomputes with `commit_risk`.
- `save()` - writes the memo beside the state file, atomically
  (`.tmp` + `os.replace`) and best effort (never raises), keeping the last
  `MAX_MEMO_ANSWERS_PER_FOLDER` answers per folder.

### `run_git` / `try_git` / `run_command`

These hold their logic directly. `try_git` is just `run_git` with a `GitError`
turned into `None`. `run_command` splits the configured command with `shlex` and
runs it without a shell.
