from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from clowder.errors import MuxError
from clowder.mux import Mux, parse_agent_list, reply_pane_id
from tests.support import clean_env, make_mux_launcher, write_fake_state

# Captured from `herdr agent list`, trimmed to two agents.
REAL_PAYLOAD = """
{"id":"cli:agent:list","result":{"agents":[
 {"agent":"pi","agent_session":{"agent":"pi","kind":"path","source":"herdr:pi",
  "value":"C:\\\\Users\\\\me\\\\.pi\\\\agent\\\\sessions\\\\--a--\\\\x.jsonl"},
  "agent_status":"idle","cwd":"C:\\\\Users\\\\me\\\\code\\\\myrepo","focused":false,
  "interactive_ready":true,"name":"verifier","pane_id":"w1:p2","tab_id":"w1:t2",
  "workspace_id":"w1"},
 {"agent":"pi","agent_session":{"agent":"pi","kind":"path","source":"herdr:pi",
  "value":"C:\\\\Users\\\\me\\\\.pi\\\\agent\\\\sessions\\\\--a--\\\\y.jsonl"},
  "agent_status":"working","cwd":"C:\\\\Users\\\\me\\\\code\\\\myrepo","focused":true,
  "name":"maker","pane_id":"w1:p1"}
],"type":"agent_list"}}
"""


class BuildArgvTest(unittest.TestCase):
    def test_template_is_filled(self) -> None:
        mux = Mux("herdr", ("agent", "prompt", "{agent}", "{brief}"))
        argv = mux.build_prompt_argv("maker", "fix the thing")
        self.assertEqual(argv, ["herdr", "agent", "prompt", "maker", "fix the thing"])

    def test_brief_stays_one_argument_even_with_newlines(self) -> None:
        mux = Mux("herdr", ("agent", "prompt", "{agent}", "{brief}"))
        brief = "ship: do it\n\n1. shape\n2. test"
        argv = mux.build_prompt_argv("maker", brief)
        self.assertEqual(len(argv), 5)
        self.assertEqual(argv[-1], brief)

    def test_brief_is_appended_when_the_template_omits_it(self) -> None:
        mux = Mux("herdr", ("agent", "prompt", "{agent}"))
        argv = mux.build_prompt_argv("maker", "the brief")
        self.assertEqual(argv[-1], "the brief")

    def test_an_overlong_brief_is_refused_before_it_is_sent(self) -> None:
        mux = Mux("herdr", ("agent", "prompt", "{agent}", "{brief}"))
        with self.assertRaises(MuxError) as caught:
            mux.build_prompt_argv("maker", "x" * 9000)
        self.assertIn("--brief-file", str(caught.exception))

    def test_send_keys_argv_types_the_command_as_keys(self) -> None:
        # A slash command through the prompt is a message, not a command; the
        # reset must arrive as keys so the editor submits it.
        mux = Mux("herdr", ("agent", "prompt", "{agent}", "{brief}"))
        self.assertEqual(
            mux.build_send_keys_argv("maker", ["/", "n", "e", "w", "enter"]),
            ["herdr", "agent", "send-keys", "maker", "/", "n", "e", "w", "enter"],
        )

    def test_split_argv_carries_the_pane_environment(self) -> None:
        mux = Mux("herdr", ("agent", "prompt", "{agent}", "{brief}"))
        argv = mux.build_split_argv("C:/code/myrepo", env={"GIT_AUTHOR_NAME": "clowder-bot"})
        self.assertEqual(argv[argv.index("--cwd") + 1], "C:/code/myrepo")
        self.assertIn("--env", argv)
        self.assertIn("GIT_AUTHOR_NAME=clowder-bot", argv)

    def test_split_argv_has_no_env_by_default(self) -> None:
        mux = Mux("herdr", ("agent", "prompt", "{agent}", "{brief}"))
        self.assertNotIn("--env", mux.build_split_argv("C:/code/myrepo"))


class ParseAgentListTest(unittest.TestCase):
    def test_reads_the_real_shape(self) -> None:
        agents = parse_agent_list(REAL_PAYLOAD)
        self.assertEqual([a.name for a in agents], ["verifier", "maker"])
        self.assertEqual(agents[0].pane_id, "w1:p2")
        self.assertEqual(agents[0].workspace_id, "w1")
        self.assertEqual(agents[0].status, "idle")
        self.assertTrue(agents[0].session_path is not None)
        self.assertTrue(agents[0].session_path.endswith("x.jsonl"))
        self.assertEqual(agents[1].status, "working")
        self.assertTrue(agents[1].focused)
        # The second record has no workspace_id, so it stays unset.
        self.assertIsNone(agents[1].workspace_id)

    def test_a_different_wrapper_still_works(self) -> None:
        payload = '{"data": {"nested": {"agents": [{"name": "solo"}]}}}'
        self.assertEqual([a.name for a in parse_agent_list(payload)], ["solo"])

    def test_no_agents_list_is_an_error(self) -> None:
        with self.assertRaises(MuxError):
            parse_agent_list('{"result": {"something_else": []}}')

    def test_non_json_is_an_error(self) -> None:
        with self.assertRaises(MuxError):
            parse_agent_list("herdr: no server running")
        with self.assertRaises(MuxError):
            parse_agent_list("")

    def test_records_without_a_name_are_skipped(self) -> None:
        payload = '{"agents": [{"name": ""}, {"pane_id": "w1:p9"}, {"name": "ok"}]}'
        self.assertEqual([a.name for a in parse_agent_list(payload)], ["ok"])


class RealProcessTest(unittest.TestCase):
    """The subprocess boundary itself: argv in, JSON out, exit code seen."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.launcher = make_mux_launcher(Path(self.tmp.name))

    def test_list_agents_reads_a_real_process(self) -> None:
        mux = Mux(str(self.launcher), ("agent", "prompt", "{agent}", "{brief}"))
        agents = mux.list_agents()
        self.assertEqual([a.name for a in agents], ["maker"])

    def test_prompt_success(self) -> None:
        mux = Mux(str(self.launcher), ("agent", "prompt", "{agent}", "{brief}"))
        result = mux.prompt("maker", "hello there")
        self.assertTrue(result.ok)
        self.assertEqual(result.returncode, 0)

    def test_prompt_failure_is_reported_not_raised(self) -> None:
        os.environ["CLOWDER_FAKE_PROMPT_FAIL"] = "1"
        self.addCleanup(os.environ.pop, "CLOWDER_FAKE_PROMPT_FAIL", None)
        mux = Mux(str(self.launcher), ("agent", "prompt", "{agent}", "{brief}"))
        result = mux.prompt("maker", "hello")
        self.assertFalse(result.ok)
        self.assertEqual(result.returncode, 4)
        self.assertIn("agent_blocked", result.error_text())

    def test_wait_and_until_reach_the_command_line(self) -> None:
        log = Path(self.tmp.name) / "calls.jsonl"
        os.environ["CLOWDER_FAKE_LOG"] = str(log)
        self.addCleanup(os.environ.pop, "CLOWDER_FAKE_LOG", None)
        mux = Mux(str(self.launcher), ("agent", "prompt", "{agent}", "{brief}"))
        mux.prompt("maker", "brief text", wait=True, until=["idle"])
        argv = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(
            argv, ["agent", "prompt", "maker", "brief text", "--wait", "--until", "idle"]
        )

    def test_no_wait_is_passed_by_default(self) -> None:
        log = Path(self.tmp.name) / "calls.jsonl"
        os.environ["CLOWDER_FAKE_LOG"] = str(log)
        self.addCleanup(os.environ.pop, "CLOWDER_FAKE_LOG", None)
        mux = Mux(str(self.launcher), ("agent", "prompt", "{agent}", "{brief}"))
        mux.prompt("maker", "brief text")
        argv = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertNotIn("--wait", argv)

    def test_a_newline_in_the_brief_survives_the_process_boundary(self) -> None:
        # The real boundary: one argv element, newline and all, with no shell to
        # re-parse it. `sys.executable` is used so no shim stands in the way.
        code = "import json,sys;print(json.dumps(sys.argv[1:]))"
        mux = Mux(sys.executable, ("-c", code, "{agent}", "{brief}"))
        result = mux.prompt("maker", "[clowder job t-0001 | ship | r | from me]\nthe brief")
        self.assertTrue(result.ok, result.error_text())
        self.assertEqual(
            json.loads(result.stdout),
            ["maker", "[clowder job t-0001 | ship | r | from me]\nthe brief"],
        )

    def test_a_missing_binary_is_an_error(self) -> None:
        mux = Mux(str(Path(self.tmp.name) / "nope-not-here"), ("agent", "list"))
        with self.assertRaises(MuxError):
            mux.list_agents()

    # -- creating topology, against a real process -------------------------

    def mux_env(self, **extra: object) -> dict[str, object]:
        state = Path(self.tmp.name) / "mux-state.json"
        write_fake_state(state, agents=[])
        return {
            "CLOWDER_FAKE_STATE": str(state),
            "CLOWDER_FAKE_SESSION_DIR": str(Path(self.tmp.name) / "sessions"),
            **extra,
        }

    def make_mux(self) -> Mux:
        return Mux(str(self.launcher), ("agent", "prompt", "{agent}", "{brief}"))

    def test_split_pane_returns_the_new_pane_id(self) -> None:
        with clean_env(**self.mux_env()):
            pane_id = self.make_mux().split_pane(cwd="C:/code/myrepo")
        self.assertTrue(pane_id.startswith("w9:p"), pane_id)

    def test_split_pane_sends_the_identity_environment(self) -> None:
        log = Path(self.tmp.name) / "split.jsonl"
        with clean_env(**self.mux_env(CLOWDER_FAKE_LOG=str(log))):
            self.make_mux().split_pane(
                cwd="C:/code/myrepo", env={"GIT_AUTHOR_EMAIL": "bot@example.com"}
            )
        argv = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertIn("--env", argv)
        self.assertIn("GIT_AUTHOR_EMAIL=bot@example.com", argv)

    def test_start_agent_then_the_live_list_shows_it(self) -> None:
        with clean_env(**self.mux_env()):
            mux = self.make_mux()
            pane_id = mux.split_pane(cwd="C:/code/myrepo")
            result = mux.start_agent("myrepo-maker", pane_id, kind="pi")
            self.assertTrue(result.ok, result.error_text())
            agents = mux.list_agents()

        self.assertEqual([a.name for a in agents], ["myrepo-maker"])
        self.assertEqual(agents[0].cwd, "C:/code/myrepo")
        self.assertEqual(agents[0].pane_id, pane_id)

    def test_a_blocked_start_is_reported_with_its_code(self) -> None:
        with clean_env(**self.mux_env(CLOWDER_FAKE_START_FAIL="1")):
            mux = self.make_mux()
            pane_id = mux.split_pane(cwd="C:/code/myrepo")
            result = mux.start_agent("myrepo-maker", pane_id)
        self.assertFalse(result.ok)
        self.assertIn("agent_not_ready", result.error_text())

    def test_starting_in_an_unknown_pane_fails_loudly(self) -> None:
        with clean_env(**self.mux_env()):
            result = self.make_mux().start_agent("myrepo-maker", "w9:p999")
        self.assertFalse(result.ok)
        self.assertIn("no_such_pane", result.error_text())

    def test_splitting_with_no_way_to_record_it_fails(self) -> None:
        with clean_env():
            with self.assertRaises(MuxError) as caught:
                self.make_mux().split_pane(cwd="C:/code/myrepo")
        self.assertIn("no_state", str(caught.exception))

    # -- moving a pane into a workspace -------------------------------------

    def test_move_pane_records_a_new_tab_without_focus(self) -> None:
        env = self.mux_env()
        with clean_env(**env):
            mux = self.make_mux()
            pane_id = mux.split_pane(cwd="C:/code/myrepo")
            result = mux.move_pane(pane_id, "w1")
        self.assertTrue(result.ok, result.error_text())
        state = json.loads(Path(str(env["CLOWDER_FAKE_STATE"])).read_text(encoding="utf-8"))
        self.assertEqual(
            state["moves"],
            [
                {
                    "pane_id": pane_id,
                    "workspace_id": "w1",
                    "new_tab": True,
                    "focus": False,
                }
            ],
        )

    def test_move_pane_never_asks_for_focus(self) -> None:
        log = Path(self.tmp.name) / "moves.jsonl"
        with clean_env(**self.mux_env(CLOWDER_FAKE_LOG=str(log))):
            self.make_mux().move_pane("w9:p2", "w1")
        argv = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(
            argv,
            ["pane", "move", "w9:p2", "--workspace", "w1", "--new-tab", "--no-focus"],
        )

    def test_an_agent_started_after_a_move_reports_that_workspace(self) -> None:
        with clean_env(**self.mux_env()):
            mux = self.make_mux()
            pane_id = mux.split_pane(cwd="C:/code/myrepo")
            moved = mux.move_pane(pane_id, "w1")
            self.assertTrue(moved.ok, moved.error_text())
            moved_id = reply_pane_id(moved)
            self.assertIsNotNone(moved_id, "a move reports the pane's new id")
            assert moved_id is not None
            self.assertNotEqual(moved_id, pane_id, "the move renumbers the pane")
            mux.start_agent("myrepo-maker", moved_id)
            agents = mux.list_agents()
        made = next(a for a in agents if a.name == "myrepo-maker")
        self.assertEqual(made.workspace_id, "w1")
        self.assertEqual(made.pane_id, moved_id)

    def test_moving_with_no_way_to_record_it_fails(self) -> None:
        with clean_env():
            result = self.make_mux().move_pane("w9:p2", "w1")
        self.assertFalse(result.ok)
        self.assertIn("no_state", result.error_text())


if __name__ == "__main__":
    unittest.main()
