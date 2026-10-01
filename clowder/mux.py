"""The multiplexer adapter.

Transport is the multiplexer's own names. Nothing here holds a session address,
because session addresses move when panes reload; names do not.

The live agent list is the roster, so there is no roster file to maintain. It
also hands back each agent's Pi session file, which is how a report finds its
own worker's usage without guessing.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .errors import MuxError

# A brief is passed as one argv element. Windows caps a command line near 32k
# characters; this leaves room for the rest of the arguments.
MAX_BRIEF_CHARS = 8000


@dataclass(frozen=True)
class AgentInfo:
    """One agent as the multiplexer sees it, right now."""

    name: str
    pane_id: str | None = None
    cwd: str | None = None
    workspace_id: str | None = None
    status: str | None = None
    session_path: str | None = None
    focused: bool = False


@dataclass(frozen=True)
class MuxResult:
    """What happened when a prompt was submitted."""

    argv: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.returncode == 0

    def error_text(self) -> str:
        if self.timed_out:
            return f"the multiplexer did not return within {self.duration_ms}ms"
        payload = _first_json(self.stdout)
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                joined = ": ".join(
                    str(x) for x in (error.get("code"), error.get("message")) if x
                )
                if joined:
                    return joined
        detail = (self.stderr or self.stdout or "").strip()
        return detail or f"exit code {self.returncode}"


class Mux:
    """Drives one multiplexer binary by agent name."""

    def __init__(self, binary: str, prompt_argv: Sequence[str]) -> None:
        self.binary = binary
        self.prompt_argv = tuple(prompt_argv)

    # -- argv --------------------------------------------------------------

    def build_prompt_argv(self, agent: str, brief: str) -> list[str]:
        """Fill the configured template. {agent} and {brief} are substituted."""
        if len(brief) > MAX_BRIEF_CHARS:
            raise MuxError(
                f"brief is {len(brief)} characters; the limit is {MAX_BRIEF_CHARS}. "
                "Put it in a file and pass --brief-file."
            )
        argv = [self.binary]
        filled = False
        for part in self.prompt_argv:
            argv.append(part.replace("{agent}", agent).replace("{brief}", brief))
            if "{brief}" in part:
                filled = True
        if not filled:
            argv.append(brief)
        return argv

    # -- running -----------------------------------------------------------

    def _run(self, argv: Sequence[str], timeout_s: float) -> MuxResult:
        started = time.monotonic()
        try:
            completed = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            return MuxResult(
                argv=tuple(argv),
                returncode=None,
                stdout=_as_text(exc.stdout),
                stderr=_as_text(exc.stderr),
                duration_ms=elapsed,
                timed_out=True,
            )
        except FileNotFoundError as exc:
            raise MuxError(
                f"cannot run {argv[0]!r}: not found on PATH. Set mux.bin in the config."
            ) from exc
        except OSError as exc:
            raise MuxError(f"cannot run {argv[0]!r}: {exc}") from exc

        elapsed = int((time.monotonic() - started) * 1000)
        return MuxResult(
            argv=tuple(argv),
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            duration_ms=elapsed,
        )

    def prompt(
        self,
        agent: str,
        brief: str,
        timeout_s: float = 60.0,
        wait: bool = False,
        until: Sequence[str] = (),
    ) -> MuxResult:
        """Submit a brief to an agent. Returns as soon as it is accepted.

        Waiting is off by default: a report arrives later, on its own turn.
        """
        argv = self.build_prompt_argv(agent, brief)
        if wait:
            argv.append("--wait")
        for state in until:
            argv.extend(["--until", state])
        return self._run(argv, timeout_s)

    def list_agents(self, timeout_s: float = 15.0) -> list[AgentInfo]:
        """Ask the multiplexer which agents exist. This is the live roster."""
        argv = [self.binary, "agent", "list"]
        result = self._run(argv, timeout_s)
        if not result.ok:
            raise MuxError(f"{' '.join(argv)}: {result.error_text()}")
        return parse_agent_list(result.stdout)

    def find_agent(self, name: str, timeout_s: float = 15.0) -> AgentInfo | None:
        for agent in self.list_agents(timeout_s):
            if agent.name == name:
                return agent
        return None

    # -- creating topology -------------------------------------------------

    def split_pane(
        self,
        cwd: str | Path,
        direction: str = "right",
        focus: bool = False,
        timeout_s: float = 20.0,
    ) -> str:
        """Open a sibling pane whose working directory is `cwd`.

        A pane gets its directory when it is made and cannot be moved later, so
        this is the only place the directory can be set. Returns the pane id.
        """
        argv = [
            self.binary,
            "pane",
            "split",
            "--current",
            "--cwd",
            str(cwd),
            "--direction",
            direction,
            "--focus" if focus else "--no-focus",
        ]
        payload = self._result(argv, timeout_s)
        pane_id = _dig(payload, "result", "pane", "pane_id")
        if not isinstance(pane_id, str) or not pane_id:
            raise MuxError(
                "pane split returned no pane_id, so the new pane cannot be addressed"
            )
        return pane_id

    def move_pane(
        self,
        pane_id: str,
        workspace_id: str,
        focus: bool = False,
        timeout_s: float = 20.0,
    ) -> MuxResult:
        """Move a pane into a workspace as a new tab.

        The pane keeps its working directory. `--no-focus` is the default and is
        passed explicitly, so making a pane never pulls the human's view to it.
        """
        argv = [
            self.binary,
            "pane",
            "move",
            pane_id,
            "--workspace",
            workspace_id,
            "--new-tab",
            "--focus" if focus else "--no-focus",
        ]
        return self._run(argv, timeout_s)

    def build_send_keys_argv(self, target: str, keys: Sequence[str]) -> list[str]:
        """Type key presses into a pane. A slash command must arrive as keys.

        A slash command sent through the agent prompt is a message, not a command:
        the agent answers it and the session stays. Individual key presses go into
        the editor, so Enter submits them.
        """
        return [self.binary, "agent", "send-keys", target, *keys]

    def send_keys(self, target: str, keys: Sequence[str], timeout_s: float = 20.0) -> MuxResult:
        """Type key presses into an agent's pane and report what happened."""
        return self._run(self.build_send_keys_argv(target, keys), timeout_s)

    def start_agent(
        self,
        name: str,
        pane_id: str,
        kind: str = "pi",
        timeout_s: float | None = None,
    ) -> MuxResult:
        """Start an agent in a pane that is sitting at a shell prompt.

        Returns only once the multiplexer has detected the agent and considers it
        ready, or with a failure that names the reason.
        """
        argv = [
            self.binary,
            "agent",
            "start",
            name,
            "--kind",
            kind,
            "--pane",
            pane_id,
        ]
        return self._run(argv, timeout_s if timeout_s is not None else 45.0)

    def _result(self, argv: Sequence[str], timeout_s: float) -> object:
        """Run a command that must succeed, and hand back its JSON."""
        result = self._run(argv, timeout_s)
        if not result.ok:
            raise MuxError(f"{' '.join(argv[:3])}: {result.error_text()}")
        payload = _first_json(result.stdout)
        if payload is None:
            raise MuxError(f"{' '.join(argv[:3])}: the reply was not JSON")
        return payload


def _dig(node: object, *keys: str) -> object | None:
    """Walk a JSON payload: _dig(payload, "result", "pane", "pane_id")."""
    current = node
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def reply_pane_id(result: MuxResult) -> str | None:
    """The pane id a multiplexer reply names, if it names one.

    A move gives the pane a new id in the target workspace, so the reply is the
    only place the new id can be read. None means the reply did not carry one.
    """
    payload = _first_json(result.stdout)
    pane_id = _dig(payload, "result", "pane", "pane_id")
    return pane_id if isinstance(pane_id, str) and pane_id else None


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def parse_agent_list(output: str) -> list[AgentInfo]:
    """Read the multiplexer's agent list.

    The documented shape is {"result": {"agents": [...]}}. Anything else is
    searched for the first object that carries an "agents" list, so a small
    wrapper change does not break dispatch.
    """
    payload = _first_json(output)
    if payload is None:
        raise MuxError("agent list did not return JSON; cannot read the live roster")

    records = _find_agents(payload)
    if records is None:
        raise MuxError("agent list JSON had no 'agents' list")

    agents: list[AgentInfo] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        name = record.get("name")
        if not isinstance(name, str) or not name:
            continue
        agents.append(
            AgentInfo(
                name=name,
                pane_id=_opt_str(record.get("pane_id")),
                cwd=_opt_str(record.get("cwd")),
                workspace_id=_opt_str(record.get("workspace_id")),
                status=_opt_str(record.get("agent_status") or record.get("status")),
                session_path=_session_path(record),
                focused=bool(record.get("focused", False)),
            )
        )
    return agents


def _first_json(output: str) -> object | None:
    text = output.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(("{", "[")):
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def _find_agents(node: object, depth: int = 0) -> list[object] | None:
    if depth > 6:
        return None
    if isinstance(node, dict):
        found = node.get("agents")
        if isinstance(found, list):
            return found
        for value in node.values():
            hit = _find_agents(value, depth + 1)
            if hit is not None:
                return hit
    elif isinstance(node, list):
        for value in node:
            hit = _find_agents(value, depth + 1)
            if hit is not None:
                return hit
    return None


def _session_path(record: dict[str, object]) -> str | None:
    """Find the agent's Pi session file. A pane can be reset to a new one."""
    session = record.get("agent_session")
    if isinstance(session, dict):
        value = session.get("value")
        if isinstance(value, str) and value:
            return value
    for value in record.values():
        if isinstance(value, str) and value.endswith(".jsonl"):
            return value
    return None


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
