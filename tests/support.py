"""Shared test fixtures: a fake multiplexer on disk, and fake Pi sessions."""

from __future__ import annotations

import contextlib
import json
import os
import stat
import sys
from pathlib import Path

FAKE_MUX = Path(__file__).with_name("fake_mux.py")


@contextlib.contextmanager
def clean_env(**extra: object):
    """Run with no CLOWDER_* variables from the outside world, plus `extra`."""
    saved = dict(os.environ)
    for key in [k for k in os.environ if k.startswith("CLOWDER_")]:
        del os.environ[key]
    for key, value in extra.items():
        os.environ[key] = str(value)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


@contextlib.contextmanager
def working_dir(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def write_config(path: Path, **sections: object) -> Path:
    """Write a config file. Paths are written with forward slashes."""
    lines: list[str] = ["# test config"]
    for name, body in sections.items():
        lines.append(f"[{name}]")
        assert isinstance(body, dict)
        for key, value in body.items():
            if isinstance(value, list):
                rendered = "[" + ", ".join(json.dumps(v) for v in value) + "]"
            else:
                # json escaping is valid TOML basic-string escaping.
                rendered = json.dumps(value)
            lines.append(f"{key} = {rendered}")
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def make_mux_launcher(directory: Path) -> Path:
    """A runnable stand-in for the multiplexer binary.

    The real thing is invoked as one executable plus arguments, so the stand-in
    has to be a real executable too. On Windows that is a .cmd shim; elsewhere a
    shell script.

    One known limit of the Windows shim: cmd.exe splits its own command line on
    newlines, so a brief containing newlines does not survive this stand-in. The
    real multiplexer is an .exe and takes argv directly, so that is a property of
    the shim, not of clowder. Argv construction with newlines is covered in
    test_mux.
    """
    directory.mkdir(parents=True, exist_ok=True)
    python = Path(sys.executable)
    script = FAKE_MUX

    if os.name == "nt":
        launcher = directory / "fakemux.cmd"
        launcher.write_text(f'@echo off\r\n"{python}" "{script}" %*\r\n', encoding="ascii")
        return launcher

    launcher = directory / "fakemux"
    launcher.write_text(f'#!/bin/sh\nexec "{python}" "{script}" "$@"\n', encoding="ascii")
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return launcher


def write_fake_state(
    path: Path,
    agents: list[dict[str, object]] | None = None,
    panes: dict[str, str] | None = None,
) -> Path:
    """Seed the stand-in multiplexer with live agents, for the creating commands."""
    payload = {
        "agents": [
            {
                "name": agent["name"],
                "pane_id": agent.get("pane_id", "w9:p1"),
                "cwd": agent.get("cwd", ""),
                "status": agent.get("status", "idle"),
                "session_file": agent.get("session_file", ""),
            }
            for agent in (agents or [])
        ],
        "panes": panes or {},
        "next_pane": 10,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def write_session(
    path: Path,
    cwd: str,
    turns: list[dict[str, object]],
    session_id: str = "sess-0001",
) -> Path:
    """Write a Pi session file.

    Each turn is: {"at": epoch_ms, "text": "the prose answer", "usage": {...}}.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        json.dumps(
            {
                "type": "session",
                "version": 3,
                "id": session_id,
                "timestamp": "2026-09-27T10:00:00.000Z",
                "cwd": cwd,
            }
        ),
        json.dumps(
            {
                "type": "message",
                "id": "sys",
                "timestamp": "2026-09-27T10:00:01.000Z",
                "message": {"role": "system", "content": "preamble"},
            }
        ),
    ]
    for index, turn in enumerate(turns):
        usage = dict(turn.get("usage") or {})
        usage.setdefault(
            "cost",
            {
                "input": 0.001,
                "output": 0.001,
                "cacheRead": 0.0,
                "cacheWrite": 0.0,
                "total": 0.002,
            },
        )
        content: list[dict[str, object]] = [
            {"type": "thinking", "thinking": "should not be the answer"},
            {"type": "toolCall", "id": f"call_{index}", "name": "read", "arguments": {}},
        ]
        text = turn.get("text")
        if text:
            content.append({"type": "text", "text": text})
        lines.append(
            json.dumps(
                {
                    "type": "message",
                    "id": f"turn{index}",
                    "timestamp": "2026-09-27T10:00:02.000Z",
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "api": "openai-completions",
                        "provider": turn.get("provider", "test-provider"),
                        "model": turn.get("model", "test-model"),
                        "usage": usage,
                        "stopReason": "stop",
                        "timestamp": turn.get("at", 1790000000000),
                    },
                }
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def default_usage(
    input_tokens: int = 1000,
    output_tokens: int = 200,
    total: int | None = None,
    cost_total: float = 0.002,
) -> dict[str, object]:
    return {
        "input": input_tokens,
        "output": output_tokens,
        "cacheRead": 0,
        "cacheWrite": 0,
        "reasoning": 0,
        "totalTokens": total if total is not None else input_tokens + output_tokens,
        "cost": {
            "input": cost_total / 2,
            "output": cost_total / 2,
            "cacheRead": 0.0,
            "cacheWrite": 0.0,
            "total": cost_total,
        },
    }
