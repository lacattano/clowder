"""Making sure an agent exists where a job belongs.

A pane is given its working directory when it is made and cannot be moved later.
So a pane *is* a repo: its shell, its context files and its skills all come from
that directory. A job for repo B cannot be handed to a pane sitting in repo A.

The only two ways out are to make an agent in repo B, or to ask the human. This
module tries the first and reports clearly when it falls back to the second.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import gitcmd
from .errors import UsageError
from .mux import AgentInfo, Mux

DEFAULT_KIND = "pi"
DEFAULT_ROLE = "maker"
DEFAULT_DIRECTION = "right"

# The multiplexer accepts [a-z][a-z0-9_-]{0,31}.
MAX_NAME = 32
NAME_RUNS = re.compile(r"[^a-z0-9]+")
VALID_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# The roles a crew uses. Only used to pick a default name.
ROLES = ("maker", "verifier", "teacher", "researcher")


def sanitise(text: str, limit: int = MAX_NAME) -> str:
    """Turn a repo name into something the multiplexer will accept as a name."""
    cleaned = NAME_RUNS.sub("-", text.strip().lower()).strip("-")
    if not cleaned:
        cleaned = "repo"
    if not cleaned[0].isalpha():
        cleaned = "a" + cleaned
    return cleaned[:limit].strip("-")


def derive_name(repo_name: str, role: str = DEFAULT_ROLE) -> str:
    """`cat-tan-trading` + `maker` -> `cat-tan-trading-maker`."""
    role_part = sanitise(role, MAX_NAME)
    room = MAX_NAME - len(role_part) - 1
    repo_part = sanitise(repo_name, max(1, room))
    return f"{repo_part}-{role_part}"[:MAX_NAME].strip("-")


def normalise(path: str | Path) -> str:
    """Comparable form of a path, for the case-insensitive Windows filesystem."""
    text = str(path)
    if text.startswith("\\\\?\\"):
        text = text[4:]
    return os.path.normcase(os.path.abspath(text))


def inside(child: str | Path | None, root: str | Path) -> bool:
    """Is `child` inside `root`? False when there is no answer to give."""
    if not child:
        return False
    try:
        return os.path.commonpath([normalise(child), normalise(root)]) == normalise(root)
    except ValueError:
        # Different drives have no common path.
        return False


def agents_in_repo(agents: list[AgentInfo], repo_path: str | Path) -> list[AgentInfo]:
    return [agent for agent in agents if inside(agent.cwd, repo_path)]


def role_of(name: str) -> str | None:
    """Which role a name is playing, when the name says so.

    `maker`, `tancat-verifier` and `myrepo-maker` all name a role. `odd-name` does
    not, and a name that names no role can serve any request.
    """
    for role in ROLES:
        if name == role or name.endswith(f"-{role}"):
            return role
    return None


def match_agent(
    agents: list[AgentInfo],
    repo_path: str | Path,
    repo_name: str,
    role: str,
) -> AgentInfo | None:
    """Pick the agent that already serves this repo, or None to make one.

    In order:
      1. the name this tool would have made, `cat-tan-trading-maker`
      2. a name that is the role, or ends in it: `maker`, `tancat-maker`
      3. the only agent in the repo, and only when its name names no other role

    Rule 3 stops at another role on purpose. Asking for a verifier in a repo that
    has one maker must make a verifier, not quietly hand the work to the maker.
    Anything less certain than the above is left to the human.
    """
    candidates = agents_in_repo(agents, repo_path)
    if not candidates:
        return None

    derived = derive_name(repo_name, role)
    for agent in candidates:
        if agent.name == derived:
            return agent

    role_part = sanitise(role, MAX_NAME)
    for agent in candidates:
        if agent.name == role_part or agent.name.endswith(f"-{role_part}"):
            return agent

    if len(candidates) == 1 and role_of(candidates[0].name) in (None, role_part):
        return candidates[0]
    return None


@dataclass
class EnsureResult:
    """What happened when we looked for an agent to serve a repo."""

    agent: AgentInfo | None = None
    created: bool = False
    reason: str | None = None
    candidates: list[str] = field(default_factory=list)
    worktree: str | None = None
    note: str | None = None

    @property
    def ok(self) -> bool:
        return self.agent is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "agent": (
                {
                    "name": self.agent.name,
                    "pane_id": self.agent.pane_id,
                    "cwd": self.agent.cwd,
                    "status": self.agent.status,
                    "session_file": self.agent.session_path,
                }
                if self.agent
                else None
            ),
            "created": self.created,
            "reason": self.reason,
            "candidates": self.candidates,
            "worktree": self.worktree,
            "note": self.note,
        }


def worktree_path(repo_path: str | Path, worktree_dir: str, name: str) -> Path:
    return Path(repo_path) / worktree_dir / name


def ensure_agent(
    mux: Mux,
    repo_name: str,
    repo_path: str | Path,
    role: str = DEFAULT_ROLE,
    kind: str = DEFAULT_KIND,
    direction: str = DEFAULT_DIRECTION,
    create: bool = True,
    name: str | None = None,
    worktree_dir: str = ".worktrees",
    base: str | None = None,
    setup: str | None = None,
) -> EnsureResult:
    """Return an agent that serves this repo, making one if allowed to.

    A new agent gets a space of its own: a folder with no branch name on it, sitting
    on the current base. That is the resting state - free, clean, and holding its
    install, so the next piece of work does not rebuild one. A piece of work puts a
    branch on the space; finishing it takes the branch off again.
    """
    agents = mux.list_agents()
    matched = match_agent(agents, repo_path, repo_name, role)
    if matched is not None:
        return EnsureResult(agent=matched, created=False, worktree=matched.cwd)

    candidates = sorted(agent.name for agent in agents_in_repo(agents, repo_path))
    wanted = requested_name(name) if name else derive_name(repo_name, role)

    # Never make a second agent under a name that is already live. Two panes with
    # one name would make the transport ambiguous, which is the failure this tool
    # exists to prevent.
    taken = next((agent for agent in agents if agent.name == wanted), None)
    if taken is not None:
        return EnsureResult(
            agent=None,
            created=False,
            reason=(
                f"an agent named {wanted} already exists, in "
                f"{taken.cwd or 'an unknown directory'}"
            ),
            candidates=candidates,
        )

    if candidates and name is None and len(candidates) > 1:
        # Several agents, none of them playing the role that was asked for. Which
        # one should get the work is the human's call, not a guess.
        listed = ", ".join(candidates)
        return EnsureResult(
            agent=None,
            created=False,
            reason=(
                f"{len(candidates)} agents already serve that repo, and none matches "
                f"the role {role!r}: {listed}. Name one of those, or pass --name to "
                "make another."
            ),
            candidates=candidates,
        )

    if not create:
        return EnsureResult(
            agent=None,
            created=False,
            reason="no agent is in that repo, and creating one was not allowed",
            candidates=[],
        )

    workdir, note = _prepare_worktree(repo_path, wanted, worktree_dir, base, setup)

    pane_id = mux.split_pane(cwd=str(workdir), direction=direction)
    started = mux.start_agent(wanted, pane_id, kind=kind)
    if not started.ok:
        return EnsureResult(
            agent=None,
            created=False,
            reason=f"could not start {wanted} in pane {pane_id}: {started.error_text()}",
            candidates=[],
        )

    # Prove it, do not assume it: a name that is not in the live list cannot be
    # addressed, and a brief sent to it would go nowhere.
    after = mux.list_agents()
    made = next((agent for agent in after if agent.name == wanted), None)
    if made is None:
        return EnsureResult(
            agent=None,
            created=True,
            reason=(
                f"{wanted} started in pane {pane_id} but is not in the live agent list yet"
            ),
            candidates=[],
        )
    return EnsureResult(agent=made, created=True, worktree=str(workdir), note=note)


def requested_name(name: str) -> str:
    """A name asked for explicitly must already be one the multiplexer allows."""
    cleaned = name.strip()
    if not VALID_NAME.match(cleaned):
        raise UsageError(
            f"{name!r} is not a usable agent name. Use lowercase letters, digits, "
            "hyphens or underscores, starting with a letter, at most 32 characters."
        )
    return cleaned


def _prepare_worktree(
    repo_path: str | Path,
    name: str,
    worktree_dir: str,
    base: str | None,
    setup: str | None,
) -> tuple[Path, str | None]:
    """Make the agent's space, or explain why it will share the repo."""
    if not gitcmd.is_repo(repo_path):
        return (
            Path(repo_path),
            f"{repo_path} is not a git repository, so that agent works in the checkout itself",
        )

    target = worktree_path(repo_path, worktree_dir, name)
    known = {normalise(item.path) for item in gitcmd.list_worktrees(repo_path)}
    if normalise(target) in known:
        return target, None

    base_ref = gitcmd.resolve_base(repo_path, base)
    gitcmd.add_worktree_free(repo_path, target, base_ref)
    if setup:
        gitcmd.run_command(setup, target)
    return target, None
