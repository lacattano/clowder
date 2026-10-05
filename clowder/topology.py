"""Making sure an agent exists where a job belongs.

A pane is given its working directory when it is made and cannot be moved later.
So a pane *is* a repo: its shell, its context files and its skills all come from
that directory. A job for repo B cannot be handed to a pane sitting in repo A.

The only two ways out are to make an agent in repo B, or to ask the human. This
module tries the first and reports clearly when it falls back to the second.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import gitcmd
from .errors import UsageError
from .mux import AgentInfo, Mux, reply_pane_id

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


def workspace_for_repo(agents: list[AgentInfo], repo_path: str | Path) -> str | None:
    """The workspace an agent already in this repo lives in, if one is known.

    A repo keeps its agents as tabs in one workspace, so a new agent joins the
    first peer that reports a workspace. Names are sorted so the choice does not
    depend on the order the multiplexer happens to list agents in. None means no
    peer reported a workspace, and the caller falls back to splitting.
    """
    for agent in sorted(agents_in_repo(agents, repo_path), key=lambda item: item.name):
        if agent.workspace_id:
            return agent.workspace_id
    return None


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


# Where a pane keeps its own remote-pi settings. The remote-pi extension reads it
# from the pane's working directory, which is fixed when the pane is made.
REMOTE_PI_CONFIG = (".pi", "remote-pi", "config.json")


def write_remote_pi_config(workdir: str | Path, name: str) -> Path | None:
    """Give a new pane a remote-pi config, so its Pi joins the relay on start.

    The remote-pi extension auto-inits on session start only when the pane's own
    `.pi/remote-pi/config.json` exists and `auto_start_relay` is true. A fresh
    space has no such file, so a pane made there sits on the local mesh only, off
    the relay, and the owner's phone never sees it. This writes the file before
    the pane's Pi starts.

    A new file gets `auto_start_relay: true`, so a new pane joins the relay with
    no hand step. An existing value is kept, so a pane deliberately taken off the
    relay stays off, and other settings already in the file are kept too.

    Returns the path, or None when there is no folder to write into.
    """
    root = Path(workdir)
    if not root.is_dir():
        return None
    path = root.joinpath(*REMOTE_PI_CONFIG)
    data: dict[str, object] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except OSError:
            loaded = None
        except json.JSONDecodeError:
            loaded = None
        if isinstance(loaded, dict):
            data = loaded
    data["agent_name"] = name
    if "auto_start_relay" not in data:
        data["auto_start_relay"] = True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


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
    identity: gitcmd.Identity | None = None,
    split: bool = False,
) -> EnsureResult:
    """Return an agent that serves this repo, making one if allowed to.

    A new agent gets a space of its own: a folder with no branch name on it, sitting
    on the current base. That is the resting state - free, clean, and holding its
    install, so the next piece of work does not rebuild one. A piece of work puts a
    branch on the space; finishing it takes the branch off again.

    By default the new pane opens as a new tab, so the caller's own pane is never
    halved; `split=True` asks for the old split-beside-the-caller placement.

    `identity`, when given, is set in the new pane's environment, so every commit
    the agent makes in it carries the crew's identity rather than the checkout's.
    """
    agents = mux.list_agents()
    candidates = sorted(agent.name for agent in agents_in_repo(agents, repo_path))

    if name:
        # An explicit name means exactly that name. Do not fall back to a role
        # match: "make me tancat-maker" must not hand back the maker.
        wanted = requested_name(name)
        found = next((agent for agent in agents if agent.name == wanted), None)
        if found is not None:
            if inside(found.cwd, repo_path):
                return EnsureResult(agent=found, created=False, worktree=found.cwd)
            # A live name is never reused from another repo: that pane serves the
            # other repo, and a brief sent to it would be refused anyway.
            return EnsureResult(
                agent=None,
                created=False,
                reason=(
                    f"an agent named {wanted} already exists, in "
                    f"{found.cwd or 'an unknown directory'}"
                ),
                candidates=candidates,
            )
    else:
        wanted = derive_name(repo_name, role)
        matched = match_agent(agents, repo_path, repo_name, role)
        if matched is not None:
            return EnsureResult(agent=matched, created=False, worktree=matched.cwd)

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

    # Before the pane's Pi starts, give it a remote-pi config that turns the relay
    # on. Without it the extension does not auto-init, and the pane never reaches
    # the owner's phone. Written here, before the split, so a failure leaves no
    # half-made pane behind.
    write_remote_pi_config(workdir, wanted)

    # A repo's agents are tabs in one workspace. When a peer is already there, the
    # new pane joins that workspace. With no peer reporting one, the pane opens a
    # new tab in its own workspace - the caller's - so the caller's tab is left
    # whole. `split=True` skips the move and leaves the pane beside the caller.
    workspace_id = workspace_for_repo(agents, repo_path)

    pane_id = mux.split_pane(
        cwd=str(workdir),
        direction=direction,
        env=identity.env() if identity else None,
    )
    if not split:
        moved = mux.move_pane(pane_id, workspace_id)
        if not moved.ok:
            where = (
                f"workspace {workspace_id}"
                if workspace_id
                else "a new tab in the caller's workspace"
            )
            # The pane is still a split in the caller's tab, so close it to undo
            # the split. Without this the caller's tab stays halved while the
            # report says nothing half-made is left.
            closed = mux.close_pane(pane_id)
            if closed.ok:
                undone = f" The split pane {pane_id} was closed."
            else:
                undone = (
                    f" The split pane {pane_id} could not be closed "
                    f"({closed.error_text()}), so the caller's tab is still halved."
                )
            return EnsureResult(
                agent=None,
                created=False,
                reason=(
                    f"made pane {pane_id} for {wanted}, but could not move it into "
                    f"{where}: {moved.error_text()}.{undone}"
                ),
                candidates=candidates,
            )
        # A move gives the pane a new id in the target workspace, so the id the
        # split returned no longer exists. Start in the pane the move reports.
        moved_id = reply_pane_id(moved)
        if moved_id is None:
            return EnsureResult(
                agent=None,
                created=False,
                reason=(
                    f"moved pane {pane_id} for {wanted} into "
                    f"{workspace_id or 'a new tab'}, but the move did not report the "
                    "pane's new id, so the agent cannot be started in it and the "
                    "pane cannot be closed"
                ),
                candidates=candidates,
            )
        pane_id = moved_id
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
