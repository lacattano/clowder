"""Configuration.

The tool is the repo. One person's setup - repos, models, agents, transport - is
config, and lives outside the repo. Nothing here has a personal default baked in:
a missing workspace root is an error, not a guess.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError

DEFAULT_MUX_BIN = "herdr"
DEFAULT_MUX_PROMPT_ARGV = ("agent", "prompt", "{agent}", "{brief}")
STATE_FILENAME = "state.json"

# How finished work reaches the world. Firstmate names three modes and we borrow
# the names: `local-only` stays on this machine until the human merges,
# `direct-PR` opens a pull request, `no-mistakes` runs a validation pipeline.
# Only `local-only` is built. The others are named so the config cannot quietly
# mean something the code does not do.
DEFAULT_DELIVERY = "local-only"
DELIVERY_MODES = ("local-only", "direct-PR", "no-mistakes")
BUILT_DELIVERY_MODES = ("local-only",)

# The identity every commit the crew makes carries. It is a bot, not the owner,
# so his own global git config stays his. One default for every repo the crew
# touches, and one place to change it.
DEFAULT_GIT_NAME = "clowder-bot"
DEFAULT_GIT_EMAIL = "94532220+lacattano@users.noreply.github.com"

SEARCH_NAMES = ("clowder.config.toml", "clowder.toml")


def default_home() -> Path:
    """Where clowder keeps its own state when config says nothing else."""
    override = os.environ.get("CLOWDER_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".clowder"


@dataclass(frozen=True)
class Config:
    """Resolved settings. Every path here is absolute and expanded."""

    home: Path
    source: Path | None
    workspace_root: Path | None
    mux_bin: str
    mux_prompt_argv: tuple[str, ...]
    sessions_root: Path
    state_path: Path
    repos: dict[str, Path]
    dispatch_marker: bool = True
    # What a dispatch does when the owner has left text in the worker's input box.
    # A dispatch that types over it merges two writers into one message, so the
    # tool waits this long for the box to clear, then refuses.
    dispatch_input_wait: float = 1.5
    # The agent bus, when this machine has a shell command that can send on it.
    # `send_argv` is a complete argv with {agent} and {brief}; `peers_argv` lists
    # the joined peers. Both are optional: with no send command the tool types,
    # under the input-box guard.
    bus_send_argv: tuple[str, ...] | None = None
    bus_peers_argv: tuple[str, ...] | None = None
    front_door_name: str | None = None
    worktree_dir: str = ".worktrees"
    worktree_base: str | None = None
    worktree_setup: str | None = None
    job_branch_prefix: str = "task/"
    delivery_mode: str = "local-only"
    git_name: str = DEFAULT_GIT_NAME
    git_email: str = DEFAULT_GIT_EMAIL

    def resolve_repo(self, name: str) -> Path:
        """Turn a repo name into a path.

        A name is resolved against the workspace root, so the tool never has to
        know any real repo. An obvious path is accepted as-is, for one-off use.
        """
        named = self.repos.get(name)
        if named is not None:
            return _require_dir(named, f"repos.{name}")

        looks_like_path = (
            Path(name).is_absolute() or name.startswith(".") or "/" in name or "\\" in name
        )
        if looks_like_path:
            return _require_dir(Path(name).expanduser(), name)

        if self.workspace_root is None:
            raise ConfigError(
                f"repo {name!r} is a name, but no workspace root is set. "
                "Set [workspace] root in the config, or pass the repo as a path."
            )
        return _require_dir(self.workspace_root / name, name)

    def as_dict(self) -> dict[str, object]:
        return {
            "home": str(self.home),
            "config_source": str(self.source) if self.source else None,
            "workspace_root": str(self.workspace_root) if self.workspace_root else None,
            "mux_bin": self.mux_bin,
            "mux_prompt_argv": list(self.mux_prompt_argv),
            "sessions_root": str(self.sessions_root),
            "state_path": str(self.state_path),
            "repos": {k: str(v) for k, v in sorted(self.repos.items())},
            "dispatch_marker": self.dispatch_marker,
            "dispatch_input_wait": self.dispatch_input_wait,
            "bus_send_argv": list(self.bus_send_argv) if self.bus_send_argv else None,
            "bus_peers_argv": list(self.bus_peers_argv) if self.bus_peers_argv else None,
            "front_door_name": self.front_door_name,
            "worktree_dir": self.worktree_dir,
            "worktree_base": self.worktree_base,
            "worktree_setup": self.worktree_setup,
            "job_branch_prefix": self.job_branch_prefix,
            "delivery_mode": self.delivery_mode,
            "git_name": self.git_name,
            "git_email": self.git_email,
        }


def _require_dir(path: Path, label: str) -> Path:
    # Always absolute: a recorded repo_path must not depend on the cwd it was
    # recorded in.
    resolved = path.expanduser().absolute()
    if not resolved.is_dir():
        raise ConfigError(f"{label}: not a directory: {resolved}")
    return resolved


def find_config_file(explicit: str | Path | None = None) -> Path | None:
    """Explicit path, then $CLOWDER_CONFIG, then cwd, then the home dir."""
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        return path

    from_env = os.environ.get("CLOWDER_CONFIG")
    if from_env:
        path = Path(from_env).expanduser()
        if not path.is_file():
            raise ConfigError(f"CLOWDER_CONFIG is not a file: {path}")
        return path

    for name in SEARCH_NAMES:
        candidate = Path.cwd() / name
        if candidate.is_file():
            return candidate

    for name in SEARCH_NAMES:
        candidate = default_home() / name
        if candidate.is_file():
            return candidate

    return None


def load_config(explicit: str | Path | None = None) -> Config:
    """Build a Config. Config beats nothing; the environment beats the file."""
    source = find_config_file(explicit)
    raw: dict[str, object] = {}
    if source is not None:
        try:
            with open(source, "rb") as fh:
                raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(
                f"{source}: not valid TOML: {exc}\n"
                "hint: on Windows, write paths in single quotes, so backslashes "
                "need no escaping: root = 'C:\\Users\\you\\code'"
            ) from exc
        except OSError as exc:
            raise ConfigError(f"{source}: cannot read: {exc}") from exc

    home = default_home()
    workspace = _table(raw, "workspace", source)
    mux = _table(raw, "mux", source)
    sessions = _table(raw, "sessions", source)
    state = _table(raw, "state", source)
    repos = _table(raw, "repos", source)
    dispatch = _table(raw, "dispatch", source)
    bus = _table(raw, "bus", source)
    front_door = _table(raw, "front_door", source)
    worktree = _table(raw, "worktree", source)
    git = _table(raw, "git", source)

    workspace_root = _opt_path(os.environ.get("CLOWDER_WORKSPACE") or workspace.get("root"))

    mux_bin = str(os.environ.get("CLOWDER_MUX_BIN") or mux.get("bin") or DEFAULT_MUX_BIN)

    prompt_argv = mux.get("prompt_argv")
    if prompt_argv is None:
        prompt_template = DEFAULT_MUX_PROMPT_ARGV
    else:
        if not isinstance(prompt_argv, list) or not all(
            isinstance(item, str) for item in prompt_argv
        ):
            raise ConfigError(f"{source}: mux.prompt_argv must be a list of strings")
        prompt_template = tuple(prompt_argv)

    sessions_root = _opt_path(
        os.environ.get("CLOWDER_SESSIONS_ROOT") or sessions.get("root")
    ) or (Path.home() / ".pi" / "agent" / "sessions")

    state_path = _opt_path(os.environ.get("CLOWDER_STATE") or state.get("path"))
    if state_path is None:
        state_path = (source.parent if source else home) / STATE_FILENAME

    marker = dispatch.get("marker", True)
    if not isinstance(marker, bool):
        raise ConfigError(f"{source}: dispatch.marker must be true or false")

    input_wait = dispatch.get("input_wait", 1.5)
    if (
        not isinstance(input_wait, (int, float))
        or isinstance(input_wait, bool)
        or input_wait < 0
    ):
        raise ConfigError(
            f"{source}: dispatch.input_wait must be a number of seconds, 0 or more"
        )

    unknown_bus = set(bus) - {"send_argv", "peers_argv"}
    if unknown_bus:
        raise ConfigError(f"{source}: unknown bus keys: {sorted(unknown_bus)}")
    bus_send_argv = _opt_argv(bus.get("send_argv"), "bus.send_argv", source)
    bus_peers_argv = _opt_argv(bus.get("peers_argv"), "bus.peers_argv", source)

    front_door_name = front_door.get("name")
    if front_door_name is not None and not isinstance(front_door_name, str):
        raise ConfigError(f"{source}: front_door.name must be a string")

    for key, value in worktree.items():
        if not isinstance(value, str):
            raise ConfigError(f"{source}: worktree.{key} must be a string")
    unknown = set(worktree) - {
        "dir",
        "base",
        "setup",
        "job_prefix",
        "delivery",
    }
    if unknown:
        raise ConfigError(f"{source}: unknown worktree keys: {sorted(unknown)}")

    delivery = str(worktree.get("delivery") or DEFAULT_DELIVERY)
    if delivery not in DELIVERY_MODES:
        raise ConfigError(f"{source}: worktree.delivery must be one of {DELIVERY_MODES}")

    unknown_git = set(git) - {"name", "email"}
    if unknown_git:
        raise ConfigError(f"{source}: unknown git keys: {sorted(unknown_git)}")
    for key in ("name", "email"):
        value = git.get(key)
        if value is not None and not isinstance(value, str):
            raise ConfigError(f"{source}: git.{key} must be a string")
    git_name = str(os.environ.get("CLOWDER_GIT_NAME") or git.get("name") or DEFAULT_GIT_NAME)
    git_email = str(
        os.environ.get("CLOWDER_GIT_EMAIL") or git.get("email") or DEFAULT_GIT_EMAIL
    )

    return Config(
        home=home,
        source=source,
        workspace_root=workspace_root,
        mux_bin=mux_bin,
        mux_prompt_argv=tuple(prompt_template),
        sessions_root=sessions_root.expanduser(),
        state_path=state_path,
        repos={str(k): _opt_path(v) or Path(str(v)) for k, v in repos.items()},
        dispatch_marker=marker,
        dispatch_input_wait=float(input_wait),
        bus_send_argv=bus_send_argv,
        bus_peers_argv=bus_peers_argv,
        front_door_name=front_door_name or None,
        worktree_dir=str(worktree.get("dir") or ".worktrees"),
        worktree_base=_opt_str(worktree.get("base")),
        worktree_setup=_opt_str(worktree.get("setup")),
        job_branch_prefix=str(worktree.get("job_prefix") or "task/"),
        delivery_mode=delivery,
        git_name=git_name,
        git_email=git_email,
    )


def _opt_path(value: object) -> Path | None:
    if value in (None, ""):
        return None
    return Path(str(value)).expanduser().absolute()


def _opt_str(value: object) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _opt_argv(value: object, label: str, source: Path | None) -> tuple[str, ...] | None:
    """A configured command template: a list of strings, or nothing at all."""
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{source}: {label} must be a list of strings")
    if not value:
        return None
    return tuple(value)


def _table(raw: dict[str, object], name: str, source: Path | None) -> dict[str, object]:
    """One `[section]` of the config file, or an empty one.

    Also the one place that insists a section is a table, so every reader below
    can trust the shape it is handed.
    """
    value = raw.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{source}: [{name}] must be a table")
    return {str(key): item for key, item in value.items()}
