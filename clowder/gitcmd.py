"""Git, called directly.

The tool needs four things from git and nothing more: is this tree clean, which
branch is it on, which commit, and put that branch here. Everything else is the
human's business - the tool never commits, pushes or merges. A report is not an
approval, and the diff is read by a person before it goes anywhere.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import GitError

# Branches a new one might reasonably start from, best guess first.
BASE_CANDIDATES = ("main", "master", "trunk", "develop")

DEFAULT_TIMEOUT_S = 120.0


@dataclass(frozen=True)
class CopyState:
    """What one folder currently holds. The whole point is telling folders apart."""

    path: str
    branch: str | None = None
    commit: str | None = None
    dirty: bool = False
    detached: bool = False
    is_repo: bool = True
    main_checkout: bool = False

    def label(self) -> str:
        if not self.is_repo:
            return "not a git repository"
        where = self.branch or ("no branch" if self.detached else "?")
        flags = []
        if self.main_checkout:
            flags.append("the main checkout")
        if self.dirty:
            flags.append("unsaved work")
        marked = f" ({', '.join(flags)})" if flags else ""
        return f"{where} @ {self.commit or 'no commit'}{marked}"


@dataclass(frozen=True)
class WorktreeInfo:
    path: str
    branch: str | None
    commit: str | None
    detached: bool = False
    bare: bool = False

    @property
    def label(self) -> str:
        return self.branch or "(detached)"


def _clean_path(text: str) -> str:
    # git for Windows can answer with an extended-length prefix.
    if text.startswith("\\\\?\\"):
        return text[4:]
    return text


def run_git(cwd: str | Path, *args: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> str:
    """Run git and insist that it worked."""
    argv = ["git", *args]
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
    except FileNotFoundError as exc:
        raise GitError("cannot run git: it is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} did not finish in {timeout_s:.0f}s") from exc
    except OSError as exc:
        raise GitError(f"cannot run git: {exc}") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise GitError(f"git {' '.join(args)} failed in {cwd}: {detail}")
    return completed.stdout


def try_git(cwd: str | Path, *args: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> str | None:
    """Run git where a failure is an answer, not an error."""
    try:
        return run_git(cwd, *args, timeout_s=timeout_s)
    except GitError:
        return None


# -- reading ---------------------------------------------------------------


def is_repo(path: str | Path) -> bool:
    answer = try_git(path, "rev-parse", "--is-inside-work-tree")
    return (answer or "").strip() == "true"


def toplevel(path: str | Path) -> str | None:
    answer = try_git(path, "rev-parse", "--show-toplevel")
    if answer is None or not answer.strip():
        return None
    return _clean_path(answer.strip())


def status_entries(path: str | Path) -> list[str]:
    """`git status --porcelain`, one entry per changed or untracked path."""
    answer = try_git(path, "status", "--porcelain")
    if answer is None:
        return []
    return [line for line in answer.splitlines() if line.strip()]


def is_clean(path: str | Path) -> bool:
    return not status_entries(path)


def current_branch(path: str | Path) -> str | None:
    answer = try_git(path, "rev-parse", "--abbrev-ref", "HEAD")
    if answer is None:
        return None
    name = answer.strip()
    if not name or name == "HEAD":
        return None
    return name


def head_commit(path: str | Path, short: bool = True) -> str | None:
    args = ["rev-parse"]
    if short:
        args.append("--short")
    args.append("HEAD")
    answer = try_git(path, *args)
    if answer is None or not answer.strip():
        return None
    return answer.strip()


def commit_of(path: str | Path, ref: str, short: bool = False) -> str | None:
    args = ["rev-parse"]
    if short:
        args.append("--short")
    args.append(ref)
    answer = try_git(path, *args)
    if answer is None or not answer.strip():
        return None
    return answer.strip()


def is_detached(path: str | Path) -> bool:
    answer = try_git(path, "rev-parse", "--abbrev-ref", "HEAD")
    return answer is not None and answer.strip() == "HEAD"


def count_commits(path: str | Path, rev_range: str) -> int:
    """How many commits are in a range such as `main..HEAD`."""
    answer = try_git(path, "rev-list", "--count", rev_range)
    if answer is None or not answer.strip():
        return 0
    try:
        return int(answer.strip())
    except ValueError:
        return 0


def is_linked_worktree(path: str | Path) -> bool:
    """True for a worktree made beside the repo, false for the main checkout."""
    git_dir = try_git(path, "rev-parse", "--absolute-git-dir")
    common = try_git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not git_dir or not common:
        return False
    return os.path.normcase(git_dir.strip()) != os.path.normcase(common.strip())


def describe_copy(path: str | Path) -> CopyState:
    """Everything needed to tell this folder apart from another agent's folder."""
    target = str(path)
    if not is_repo(path):
        return CopyState(path=target, is_repo=False)
    linked = is_linked_worktree(path)
    return CopyState(
        path=target,
        branch=current_branch(path),
        commit=head_commit(path),
        dirty=not is_clean(path),
        detached=is_detached(path),
        main_checkout=not linked,
    )


def branch_exists(path: str | Path, branch: str) -> bool:
    return try_git(path, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}") is not None


def local_branches(path: str | Path) -> list[str]:
    answer = try_git(path, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    if not answer:
        return []
    return [line.strip() for line in answer.splitlines() if line.strip()]


def has_remote(path: str | Path, remote: str = "origin") -> bool:
    """Is there a remote to fetch from or compare against?"""
    return try_git(path, "remote", "get-url", remote) is not None


def fetch(
    path: str | Path, remote: str = "origin", timeout_s: float = DEFAULT_TIMEOUT_S
) -> bool:
    """Fetch the remote before a base is chosen. False when there is no remote."""
    if not has_remote(path, remote):
        return False
    run_git(path, "fetch", remote, timeout_s=timeout_s)
    return True


def commits_behind(path: str | Path, local: str, remote_ref: str) -> int | None:
    """How many commits `remote_ref` is ahead of `local`.

    None when the remote ref does not exist, so a repo with no remote is never
    reported as stale by accident.
    """
    if try_git(path, "rev-parse", "--verify", "--quiet", remote_ref) is None:
        return None
    return count_commits(path, f"{local}..{remote_ref}")


def branch_behind(path: str | Path, branch: str, remote: str = "origin") -> int | None:
    """How far the local branch is behind its remote-tracking branch."""
    return commits_behind(path, branch, f"refs/remotes/{remote}/{branch}")


def resolve_base(path: str | Path, preferred: str | None = None) -> str:
    """The branch a new branch starts from.

    An explicit name must exist. Otherwise the first of the usual names wins,
    then whatever branch is checked out. Anything else is an error, because
    guessing the base of someone's work is how a branch starts from the wrong
    place.
    """
    if preferred:
        if not branch_exists(path, preferred):
            raise GitError(
                f"no branch {preferred!r} in {path}. "
                f"Known branches: {', '.join(local_branches(path)) or 'none'}"
            )
        return preferred
    for candidate in BASE_CANDIDATES:
        if branch_exists(path, candidate):
            return candidate
    current = current_branch(path)
    if current:
        return current
    raise GitError(f"cannot work out a base branch in {path}; set worktree.base")


def list_worktrees(repo: str | Path) -> list[WorktreeInfo]:
    text = run_git(repo, "worktree", "list", "--porcelain")
    found: list[WorktreeInfo] = []
    block: dict[str, str] = {}
    flags: set[str] = set()

    def flush() -> None:
        if not block and not flags:
            return
        raw_branch = block.get("branch")
        branch = raw_branch[len("refs/heads/") :] if raw_branch else None
        found.append(
            WorktreeInfo(
                path=_clean_path(block.get("worktree", "")),
                branch=branch,
                commit=block.get("HEAD"),
                detached="detached" in flags,
                bare="bare" in flags,
            )
        )

    for line in text.splitlines():
        if not line.strip():
            flush()
            block = {}
            flags = set()
            continue
        key, _, value = line.partition(" ")
        if key in ("detached", "bare", "locked", "prunable"):
            flags.add(key)
        else:
            block[key] = value
    flush()
    return found


# -- writing ---------------------------------------------------------------


def switch_new_branch(path: str | Path, branch: str, base: str) -> None:
    run_git(path, "switch", "-c", branch, base)


def switch_branch(path: str | Path, branch: str) -> None:
    run_git(path, "switch", branch)


def switch_to_commit(path: str | Path, commit: str) -> None:
    """Put a folder on one exact commit, with no branch name on it.

    This is how a reviewer gets the writer's code: the same files, pinned to one
    save, without holding the branch the writer is still using.
    """
    run_git(path, "switch", "--detach", commit)


def detach_at(path: str | Path, ref: str) -> None:
    """Free a folder: no branch name on it, sitting on the current base.

    This is the resting state of a reusable space. A space on a branch is in use;
    a space detached at the base is free for the next piece of work.
    """
    run_git(path, "switch", "--detach", ref)


def branches_containing(path: str | Path, commit: str, remotes: bool = True) -> list[str]:
    """Every branch whose history includes this commit.

    Empty means the commit is reachable from no branch at all - only from some
    folder's HEAD. That is work that will be lost when the folder is reused.

    A detached HEAD shows up in this list as the pseudo-entry `(no branch)`, so
    anything in brackets is not a branch and is dropped.
    """
    args = ["branch", "--contains", commit, "--format=%(refname:short)"]
    if remotes:
        args.insert(1, "-a")
    answer = try_git(path, *args)
    if not answer:
        return []
    return [
        name
        for name in (line.strip() for line in answer.splitlines())
        if name and not name.startswith("(")
    ]


def is_reachable(path: str | Path, commit: str) -> bool:
    """Is this commit held, rather than alive only in one folder's HEAD?"""
    if branches_containing(path, commit):
        return True
    # A held change lives under refs/clowder until its branch is published, so it
    # is reachable too. Without this, the board would call held work stranded.
    held = try_git(
        path, "for-each-ref", "--contains", commit, "--format=%(refname)", "refs/clowder"
    )
    return bool(held and held.strip())


def patch_id(path: str | Path, commit: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> str | None:
    """The stable patch-id of one commit's change, or None when it cannot be told.

    Two commits with the same patch-id carry the same change even when a rebase
    gave them different hashes. A merge or an empty commit has no patch-id, and
    the caller is told so rather than guessing.
    """
    shown = try_git(path, "show", "--no-color", "--patch", "--format=commit %H", commit)
    if not shown or not shown.strip():
        return None
    try:
        piped = subprocess.run(
            ["git", "patch-id", "--stable"],
            cwd=str(path),
            input=shown,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
    except OSError:
        return None
    except subprocess.TimeoutExpired:
        return None
    if piped.returncode != 0:
        return None
    lines = piped.stdout.strip().splitlines()
    if not lines:
        return None
    parts = lines[0].split()
    return parts[0] if parts else None


def _parents(path: str | Path, commit: str) -> list[str] | None:
    """A commit's parent hashes, or None when the commit cannot be read."""
    answer = try_git(path, "rev-list", "--parents", "-n", "1", commit)
    if not answer or not answer.strip():
        return None
    return answer.split()[1:]


def _is_empty_change(path: str | Path, commit: str) -> bool | None:
    """Does this commit change no file? None when it cannot be read."""
    changed = try_git(
        path, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", commit
    )
    if changed is None:
        return None
    return not changed.strip()


# What, if anything, is at risk about a commit that is on no branch.
ON_BRANCH = "on_branch"
LOST = "lost"
EMPTY = "empty"
MERGE = "merge"
UNKNOWN = "unknown"


def commit_risk(path: str | Path, commit: str) -> str:
    """Tell a lost change apart from one that only looks lost.

    ON_BRANCH - a branch, a remote or a refs/clowder ref already holds it.
    LOST      - the change is nowhere but this commit: real work at risk.
    EMPTY     - the commit changes no file, so there is nothing to lose.
    MERGE     - a merge; it carries its parents' content, nothing of its own.
    UNKNOWN   - it could not be told: a read failure.

    Two commits with the same patch-id carry the same change even when a rebase
    gave them different hashes, so `LOST` is decided by content, not by hash.
    """
    # Work in full hashes: `git log --all` includes this worktree's HEAD, and a
    # short hash would fail to match itself and be counted as its own content.
    full = commit_of(path, commit) or commit
    if is_reachable(path, full):
        return ON_BRANCH
    parents = _parents(path, full)
    if parents is None:
        return UNKNOWN
    if len(parents) > 1:
        return MERGE
    ident = patch_id(path, full)
    if ident is None:
        if _is_empty_change(path, full) is True:
            return EMPTY
        return UNKNOWN
    subject = try_git(path, "show", "-s", "--format=%s", full)
    if subject is None:
        return UNKNOWN
    subject = subject.strip()
    if not subject:
        return UNKNOWN
    listing = try_git(
        path,
        "log",
        "--all",
        "--no-merges",
        "--fixed-strings",
        f"--grep={subject}",
        "--format=%H",
    )
    if listing is None:
        return UNKNOWN
    for other in listing.splitlines():
        other = other.strip()
        if not other or other == full:
            continue
        if patch_id(path, other) == ident:
            return ON_BRANCH
    return LOST


def add_worktree(repo: str | Path, target: str | Path, branch: str, base: str) -> None:
    """Add a worktree on `branch`, making the branch from `base` if needed."""
    if branch_exists(repo, branch):
        run_git(repo, "worktree", "add", str(target), branch)
    else:
        run_git(repo, "worktree", "add", "-b", branch, str(target), base)


def add_worktree_free(repo: str | Path, target: str | Path, ref: str) -> None:
    """Add a worktree with no branch name on it: a space, ready to lend out."""
    run_git(repo, "worktree", "add", "--detach", str(target), ref)


def remove_worktree(repo: str | Path, target: str | Path) -> None:
    run_git(repo, "worktree", "remove", str(target))


def delete_branch(path: str | Path, branch: str) -> None:
    """Delete a merged branch. Refuses on an unmerged one, on purpose."""
    run_git(path, "branch", "-d", branch)


def push_branch(path: str | Path, branch: str, remote: str = "origin") -> None:
    """Publish one branch. The caller has already checked the owner's pass."""
    run_git(path, "push", "-u", remote, branch)


def hold_ref(path: str | Path, name: str, commit: str) -> str:
    """Point a local ref at a saved commit, so it outlives its branch.

    The commit is already in the object store, so the ref costs nothing and holds
    no space. The branch may then be kept or deleted; the commit stays reachable.
    """
    ref = f"refs/clowder/held/{name}"
    run_git(path, "update-ref", ref, commit)
    return ref


def run_command(command: str, cwd: str | Path, timeout_s: float = 900.0) -> None:
    """Run a configured setup command. No shell, so no operators to trip over."""
    parts = shlex.split(command)
    if not parts:
        return
    try:
        completed = subprocess.run(
            parts,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
    except FileNotFoundError as exc:
        raise GitError(f"cannot run {parts[0]!r}: not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"{parts[0]} did not finish in {timeout_s:.0f}s") from exc
    except OSError as exc:
        raise GitError(f"cannot run {parts[0]!r}: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise GitError(f"setup command failed in {cwd}: {detail}")
