"""Dispatch and reset must never type over text the owner left in the box.

The multiplexer writes a sent brief into the worker's own input box, which is
the box the owner types in. A second writer there merges the two texts into one
message. These tests ask for the bus path when one is configured, the guarded
typing path when it is not, and a refusal whenever the box already holds text.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from clowder import cli
from tests.support import clean_env, make_mux_launcher, write_config, write_fake_state


class BusDispatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "ws"
        self.repo = self.workspace / "myrepo"
        self.repo.mkdir(parents=True)
        self.launcher = make_mux_launcher(self.root / "bin")
        self.state = self.root / "state.json"
        self.config = self.root / "clowder.config.toml"
        self.log = self.root / "calls.jsonl"

    # -- harness -----------------------------------------------------------

    def write_config(self, *, bus: bool = False, input_wait: float = 0.0) -> None:
        sections: dict[str, object] = {
            "workspace": {"root": str(self.workspace)},
            "mux": {"bin": str(self.launcher)},
            "state": {"path": str(self.state)},
            "dispatch": {"input_wait": input_wait},
        }
        if bus:
            sections["bus"] = {
                "send_argv": [str(self.launcher), "bus", "send", "{agent}", "{brief}"],
                "peers_argv": [str(self.launcher), "bus", "peers"],
            }
        write_config(self.config, **sections)

    def cli(self, *args: str, env: dict[str, object] | None = None):
        out, err = io.StringIO(), io.StringIO()
        argv = ["--config", str(self.config), *args]
        with (
            clean_env(**(env or {})),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def dispatch(self, *args: str, env: dict[str, object] | None = None):
        base: dict[str, object] = {
            "CLOWDER_FAKE_CWD": str(self.repo),
            "CLOWDER_FAKE_LOG": str(self.log),
        }
        base.update(env or {})
        return self.cli("dispatch", *args, env=base)

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def calls_named(self, *prefix: str) -> list[list[str]]:
        return [call for call in self.calls() if call[: len(prefix)] == list(prefix)]

    def tasks(self) -> dict:
        if not self.state.exists():
            return {}
        return json.loads(self.state.read_text(encoding="utf-8"))["tasks"]

    # -- the bus path ------------------------------------------------------

    def test_a_joined_pane_gets_the_brief_over_the_bus_not_the_box(self) -> None:
        self.write_config(bus=True)
        code, _, _ = self.dispatch(
            "maker",
            "myrepo",
            "scout: look at the thing",
            "--shape",
            "scout",
            env={"CLOWDER_FAKE_BUS": "joined"},
        )
        self.assertEqual(code, 0)
        # The box is never touched: no prompt, one bus send.
        self.assertEqual(self.calls_named("agent", "prompt"), [])
        sent = self.calls_named("bus", "send")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][2], "maker")
        self.assertTrue(sent[0][3].startswith("[clowder job t-0001 | scout | myrepo |"))

        task = next(iter(self.tasks().values()))
        self.assertEqual(task["status"], "dispatched")
        self.assertEqual(task["mux_argv"][:2], [str(self.launcher), "bus"])
        # The recorded argv is the full brief; the fake's .cmd shim truncates it
        # at the first newline on the way through cmd.exe, but clowder built it
        # whole. The real multiplexer is an .exe and takes it whole.
        self.assertIn("scout: look at the thing", task["mux_argv"][-1])

    def test_a_bus_send_that_fails_is_recorded_and_the_box_is_left_alone(self) -> None:
        self.write_config(bus=True)
        code, _, err = self.dispatch(
            "maker",
            "myrepo",
            "scout: look",
            "--shape",
            "scout",
            env={"CLOWDER_FAKE_BUS": "joined", "CLOWDER_FAKE_BUS_SEND_FAIL": "1"},
        )
        self.assertEqual(code, 2)
        self.assertEqual(self.calls_named("agent", "prompt"), [])
        task = next(iter(self.tasks().values()))
        self.assertEqual(task["status"], "failed")
        self.assertIn("bus_refused", task["mux_error"])
        self.assertIn("dispatch failed", err)

    # -- the guarded typing path -------------------------------------------

    def test_a_pane_that_is_not_joined_is_typed_into_when_the_box_is_empty(self) -> None:
        self.write_config(bus=True)
        code, _, _ = self.dispatch("maker", "myrepo", "scout: look", "--shape", "scout")
        self.assertEqual(code, 0)
        self.assertEqual(self.calls_named("bus", "send"), [])
        self.assertEqual(len(self.calls_named("agent", "prompt")), 1)

    def test_dispatch_refuses_when_the_box_holds_owner_text(self) -> None:
        self.write_config()
        code, _, err = self.dispatch(
            "maker",
            "myrepo",
            "scout: look",
            "--shape",
            "scout",
            env={"CLOWDER_FAKE_INPUT": "half a sentence I was still writing"},
        )
        self.assertEqual(code, 2)
        self.assertIn("input box is not empty", err)
        self.assertIn("still writing", err)
        # Nothing was sent, and nothing was recorded.
        self.assertEqual(self.calls_named("agent", "prompt"), [])
        self.assertEqual(self.tasks(), {})

    def test_dispatch_refuses_when_the_box_cannot_be_read(self) -> None:
        self.write_config()
        code, _, err = self.dispatch(
            "maker",
            "myrepo",
            "scout: look",
            "--shape",
            "scout",
            env={"CLOWDER_FAKE_INPUT_FAIL": "1"},
        )
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)
        self.assertEqual(self.calls_named("agent", "prompt"), [])

    def test_force_types_even_with_text_in_the_box(self) -> None:
        self.write_config()
        code, _, _ = self.dispatch(
            "maker",
            "myrepo",
            "scout: look",
            "--shape",
            "scout",
            "--force",
            env={"CLOWDER_FAKE_INPUT": "the owner's draft"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(self.calls_named("agent", "prompt")), 1)

    # -- the same guard on reset -------------------------------------------

    def test_reset_refuses_when_the_box_holds_owner_text(self) -> None:
        self.write_config()
        code, _, err = self.cli(
            "agent",
            "reset",
            "maker",
            env={
                "CLOWDER_FAKE_CWD": str(self.repo),
                "CLOWDER_FAKE_LOG": str(self.log),
                "CLOWDER_FAKE_INPUT": "a draft in the box",
            },
        )
        self.assertEqual(code, 2)
        self.assertIn("input box is not empty", err)
        self.assertEqual(self.calls_named("agent", "send-keys"), [])

    def test_reset_types_when_the_box_is_empty(self) -> None:
        self.write_config()
        state = self.root / "mux-state.json"
        sessions = self.root / "sessions"
        sessions.mkdir()
        session = sessions / "maker.jsonl"
        session.write_text("", encoding="utf-8")
        write_fake_state(
            state,
            agents=[
                {
                    "name": "maker",
                    "pane_id": "w9:p1",
                    "cwd": str(self.repo),
                    "session_file": str(session),
                }
            ],
        )
        code, _, _ = self.cli(
            "agent",
            "reset",
            "maker",
            env={
                "CLOWDER_FAKE_STATE": str(state),
                "CLOWDER_FAKE_SESSION_DIR": str(sessions),
                "CLOWDER_FAKE_LOG": str(self.log),
            },
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(self.calls_named("agent", "send-keys")), 1)
