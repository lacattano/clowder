"""A stand-in multiplexer.

Mimics only what clowder reads, and only the commands it runs:

    agent list
    agent prompt <target> <text>
    pane split --current --cwd <path> ...
    agent start <name> --kind <kind> --pane <id>
    pane move <pane_id> --workspace <id> --new-tab

Without CLOWDER_FAKE_STATE the fake is stateless: `agent list` reports one agent
built from the environment, and the creating commands fail. With a state file it
behaves like the real thing, so a created agent appears on the next list.

    CLOWDER_FAKE_SESSION      session file for the stateless agent
    CLOWDER_FAKE_AGENT        stateless agent name (default: maker)
    CLOWDER_FAKE_CWD          stateless agent directory (default: cwd)
    CLOWDER_FAKE_STATUS       agent status (default: idle)
    CLOWDER_FAKE_WORKSPACE    stateless agent workspace id (default: empty)
    CLOWDER_FAKE_STATE        json file holding agents and panes
    CLOWDER_FAKE_SESSION_DIR  where a created agent's session file is recorded
    CLOWDER_FAKE_LIST_FAIL    "1" makes the agent list fail
    CLOWDER_FAKE_PROMPT_FAIL  "1" makes prompt submission fail
    CLOWDER_FAKE_START_FAIL   "1" makes agent start fail
    CLOWDER_FAKE_LOG          append the received argv, one JSON line per call
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _log(argv: list[str]) -> None:
    path = os.environ.get("CLOWDER_FAKE_LOG")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(argv) + "\n")


def _state_path() -> Path | None:
    value = os.environ.get("CLOWDER_FAKE_STATE")
    return Path(value) if value else None


def _stateless_agent() -> dict[str, object]:
    return {
        "name": os.environ.get("CLOWDER_FAKE_AGENT", "maker"),
        "pane_id": "w9:p1",
        "cwd": os.environ.get("CLOWDER_FAKE_CWD") or os.getcwd(),
        "status": os.environ.get("CLOWDER_FAKE_STATUS", "idle"),
        "session_file": os.environ.get("CLOWDER_FAKE_SESSION", ""),
        "workspace_id": os.environ.get("CLOWDER_FAKE_WORKSPACE", ""),
    }


def _load() -> dict[str, object]:
    path = _state_path()
    assert path is not None, "only called in state mode"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"agents": [], "panes": {}, "next_pane": 2}


def _save(state: dict[str, object]) -> None:
    path = _state_path()
    assert path is not None
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _agents() -> list[dict[str, object]]:
    if _state_path() is None:
        return [_stateless_agent()]
    state = _load()
    return list(state.get("agents") or [])  # type: ignore[arg-type]


def _emit(payload: dict[str, object], code: int = 0) -> int:
    print(json.dumps(payload))
    return code


def _fail(code: str, message: str, status: int = 4) -> int:
    print(json.dumps({"error": {"code": code, "message": message}}))
    return status


def main(argv: list[str]) -> int:
    _log(argv)

    if argv[:2] == ["agent", "list"]:
        if os.environ.get("CLOWDER_FAKE_LIST_FAIL") == "1":
            print("boom", file=sys.stderr)
            return 3
        return _emit(
            {"id": "cli:agent:list", "result": {"agents": _agents()}, "type": "agent_list"}
        )

    if argv[:2] == ["agent", "prompt"]:
        if os.environ.get("CLOWDER_FAKE_PROMPT_FAIL") == "1":
            return _fail("agent_blocked", "the agent is waiting at a dialog")
        target = argv[2] if len(argv) > 2 else ""
        text = argv[3] if len(argv) > 3 else ""
        return _emit({"ok": True, "target": target, "chars": len(text)})

    if argv[:2] == ["pane", "split"]:
        if _state_path() is None:
            return _fail("no_state", "the fake has no state file to record a pane in", 6)
        cwd = ""
        for index, item in enumerate(argv):
            if item == "--cwd" and index + 1 < len(argv):
                cwd = argv[index + 1]
        state = _load()
        panes = state.setdefault("panes", {})
        assert isinstance(panes, dict)
        pane_id = f"w9:p{state.get('next_pane', 2)}"
        state["next_pane"] = int(state.get("next_pane", 2)) + 1  # type: ignore[arg-type]
        panes[pane_id] = cwd
        _save(state)
        return _emit(
            {"id": "cli:pane:split", "result": {"pane": {"pane_id": pane_id, "cwd": cwd}}}
        )

    if argv[:2] == ["pane", "move"]:
        if _state_path() is None:
            return _fail("no_state", "the fake has no state file to record a move in", 6)
        pane_id = argv[2] if len(argv) > 2 else ""
        workspace_id = ""
        for index, item in enumerate(argv):
            if item == "--workspace" and index + 1 < len(argv):
                workspace_id = argv[index + 1]
        state = _load()
        # A real move gives the pane a new id in the target workspace, and the old
        # id stops existing. Anything holding the old id must read the new one
        # from this reply, or its next command fails with pane-not-found.
        panes = state.get("panes") or {}
        assert isinstance(panes, dict)
        prefix = workspace_id or "w1"
        new_pane_id = f"{prefix}:p{state.get('next_pane', 2)}"
        state["next_pane"] = int(state.get("next_pane", 2)) + 1  # type: ignore[arg-type]
        if pane_id in panes:
            panes[new_pane_id] = panes.pop(pane_id)
        moves = state.setdefault("moves", [])
        assert isinstance(moves, list)
        moves.append(
            {
                "pane_id": pane_id,
                "workspace_id": workspace_id,
                "new_tab": "--new-tab" in argv,
                "focus": "--focus" in argv,
            }
        )
        # Remember which workspace the pane sits in, so a later `agent start`
        # reports it and `agent list` looks like the real thing.
        workspaces = state.setdefault("pane_workspaces", {})
        assert isinstance(workspaces, dict)
        workspaces.pop(pane_id, None)
        workspaces[new_pane_id] = workspace_id
        _save(state)
        return _emit(
            {
                "id": "cli:pane:move",
                "result": {"pane": {"pane_id": new_pane_id, "workspace_id": workspace_id}},
            }
        )

    if argv[:2] == ["agent", "start"]:
        if os.environ.get("CLOWDER_FAKE_START_FAIL") == "1":
            return _fail("agent_not_ready", "the agent blocked during startup")
        if _state_path() is None:
            return _fail("no_state", "the fake has no state file to record an agent in", 6)
        name = argv[2] if len(argv) > 2 else ""
        kind = ""
        pane_id = ""
        for index, item in enumerate(argv):
            if item == "--kind" and index + 1 < len(argv):
                kind = argv[index + 1]
            if item == "--pane" and index + 1 < len(argv):
                pane_id = argv[index + 1]
        state = _load()
        panes = state.get("panes") or {}
        assert isinstance(panes, dict)
        if pane_id not in panes:
            return _fail("no_such_pane", f"pane {pane_id} is not available", 5)
        cwd = str(panes[pane_id])
        workspaces = state.get("pane_workspaces") or {}
        assert isinstance(workspaces, dict)
        session_dir = os.environ.get("CLOWDER_FAKE_SESSION_DIR")
        session_file = str(Path(session_dir) / f"{name}.jsonl") if session_dir else ""
        agent = {
            "name": name,
            "pane_id": pane_id,
            "cwd": cwd,
            "status": "idle",
            "kind": kind,
            "session_file": session_file,
            "workspace_id": workspaces.get(pane_id, ""),
        }
        agents = state.setdefault("agents", [])
        assert isinstance(agents, list)
        agents.append(agent)
        _save(state)
        return _emit({"id": "cli:agent:start", "result": {"agent": agent}})

    print(f"fake mux: unsupported args {argv}", file=sys.stderr)
    return 9


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
