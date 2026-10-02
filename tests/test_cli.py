"""End to end through the command line, with a stand-in multiplexer."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from clowder import cli, gitcmd
from clowder.state import StateStore, Task
from tests.support import (
    clean_env,
    default_usage,
    make_mux_launcher,
    write_config,
    write_fake_state,
    write_session,
)


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

        self.workspace = self.root / "ws"
        self.repo_path = self.workspace / "myrepo"
        (self.workspace / "myrepo").mkdir(parents=True)
        self.make_repo(self.repo_path)

        self.launcher = make_mux_launcher(self.root / "bin")
        self.session_file = self.root / "sessions" / "--x--" / "s.jsonl"
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        self.session_file.write_text("", encoding="utf-8")

        self.state = self.root / "state.json"
        self.config = write_config(
            self.root / "clowder.config.toml",
            workspace={"root": str(self.workspace)},
            mux={"bin": str(self.launcher)},
            sessions={"root": str(self.root / "sessions")},
            state={"path": str(self.state)},
        )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def make_repo(path: Path) -> None:
        """A real repository, so jobs and worktrees can be exercised for real."""
        gitcmd.run_git(path, "init", "-b", "main")
        gitcmd.run_git(path, "config", "user.email", "test@example.com")
        gitcmd.run_git(path, "config", "user.name", "Test")
        (path / "readme.md").write_text("hello\n", encoding="utf-8")
        gitcmd.run_git(path, "add", "readme.md")
        gitcmd.run_git(path, "commit", "-m", "first")

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

    def fake_env(self, **extra: object) -> dict[str, object]:
        return {
            "CLOWDER_FAKE_SESSION": str(self.session_file),
            # The stand-in agent lives in the repo the tests dispatch to, so the
            # cross-repo guard passes unless a test says otherwise.
            "CLOWDER_FAKE_CWD": str(self.workspace / "myrepo"),
            **extra,
        }

    def state_env(self, **extra: object) -> dict[str, object]:
        """Environment for the stand-in's stateful mode, where agents can be made."""
        return {
            "CLOWDER_FAKE_STATE": str(self.root / "mux-state.json"),
            "CLOWDER_FAKE_SESSION_DIR": str(self.root / "sessions"),
            **extra,
        }

    def dispatch(self, *args: str, env: dict[str, object] | None = None):
        return self.cli("dispatch", *args, env=env or self.fake_env())

    def load_state(self) -> dict:
        return json.loads(self.state.read_text(encoding="utf-8"))

    def only_task(self) -> dict:
        tasks = self.load_state()["tasks"]
        self.assertEqual(len(tasks), 1)
        return next(iter(tasks.values()))

    # -- dispatch ----------------------------------------------------------

    def test_dispatch_records_the_task(self) -> None:
        code, out, _ = self.cli(
            "dispatch",
            "maker",
            "myrepo",
            "ship: add the refund page",
            env=self.fake_env(),
        )
        self.assertEqual(code, 0)
        self.assertIn("t-0001 sent to maker", out)

        task = self.only_task()
        self.assertEqual(task["agent"], "maker")
        self.assertEqual(task["repo"], "myrepo")
        self.assertEqual(task["question"], "ship: add the refund page")
        self.assertEqual(task["shape"], "ship")
        self.assertEqual(task["status"], "dispatched")
        self.assertEqual(task["repo_path"], str(self.workspace / "myrepo"))
        self.assertEqual(task["pane_id"], "w9:p1")
        self.assertEqual(task["agent_session"], str(self.session_file))
        self.assertIsNotNone(task["dispatched_at"])
        self.assertEqual(task["mux_returncode"], 0)

    def test_the_message_is_marker_then_report_instructions_then_brief(self) -> None:
        self.cli(
            "dispatch",
            "maker",
            "myrepo",
            "ship: add the refund page",
            "--from",
            "topcat",
            env=self.fake_env(),
        )
        sent = self.only_task()["mux_argv"][-1]
        self.assertTrue(
            sent.startswith("[clowder job t-0001 | ship | myrepo | from topcat]\n"), sent
        )
        # The tool, not the brief writer, tells the worker to report over the bus.
        self.assertIn("send its report to topcat over the agent bus", sent)
        self.assertIn("peer list", sent)
        self.assertIn("clowder inbox", sent)
        self.assertTrue(sent.endswith("ship: add the refund page"), sent)

    def test_the_marker_reaches_the_process(self) -> None:
        log = self.root / "calls.jsonl"
        self.cli(
            "dispatch",
            "maker",
            "myrepo",
            "ship: add the refund page now",
            env=self.fake_env(CLOWDER_FAKE_LOG=str(log)),
        )
        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        prompt_calls = [c for c in calls if c[:2] == ["agent", "prompt"]]
        self.assertEqual(len(prompt_calls), 1)
        argv = prompt_calls[0]
        self.assertEqual(len(argv), 4, "the message must be one argument, not five")
        self.assertEqual(argv[2], "maker")
        self.assertTrue(
            argv[3].startswith("[clowder job t-0001 | ship | myrepo | from the front door]"),
            argv[3],
        )
        self.assertEqual(self.only_task()["sender"], "the front door")

    def test_the_marker_names_the_front_door_by_default(self) -> None:
        self.dispatch("maker", "myrepo", "ship: do the thing")
        self.assertEqual(self.only_task()["sender"], "the front door")

    def test_no_marker_sends_the_bare_brief(self) -> None:
        self.cli(
            "dispatch",
            "maker",
            "myrepo",
            "ship: do the thing",
            "--no-marker",
            env=self.fake_env(),
        )
        task = self.only_task()
        self.assertEqual(task["mux_argv"][-1], "ship: do the thing")
        self.assertIsNone(task["sender"])

    def test_marker_can_be_turned_off_in_config(self) -> None:
        self.config = write_config(
            self.root / "nomarker.toml",
            workspace={"root": str(self.workspace)},
            mux={"bin": str(self.launcher)},
            state={"path": str(self.state)},
            dispatch={"marker": False},
        )
        self.dispatch("maker", "myrepo", "ship: do the thing")
        self.assertIsNone(self.only_task()["sender"])

    def test_front_door_name_is_used_as_the_sender(self) -> None:
        self.config = write_config(
            self.root / "named.toml",
            workspace={"root": str(self.workspace)},
            mux={"bin": str(self.launcher)},
            state={"path": str(self.state)},
            front_door={"name": "topcat"},
        )
        self.dispatch("maker", "myrepo", "ship: do the thing")
        self.assertEqual(self.only_task()["sender"], "topcat")

    def test_an_unknown_agent_is_refused_before_anything_is_sent(self) -> None:
        code, _, err = self.cli(
            "dispatch", "ghost", "myrepo", "ship: do it", env=self.fake_env()
        )
        self.assertEqual(code, 2)
        self.assertIn("no agent named 'ghost'", err)
        self.assertIn("maker", err)
        self.assertIn("ensure", err, "the error must teach the next call")
        self.assertFalse(self.state.exists(), "a refused dispatch records nothing")

    def test_a_cross_repo_agent_is_refused(self) -> None:
        other = self.workspace / "other"
        other.mkdir()
        code, _, err = self.cli(
            "dispatch",
            "maker",
            "myrepo",
            "ship: do it",
            env=self.fake_env(CLOWDER_FAKE_CWD=str(other)),
        )
        self.assertEqual(code, 2)
        self.assertIn("is in", err)
        self.assertIn("ensure myrepo", err)
        self.assertFalse(self.state.exists(), "a refused dispatch records nothing")

    def test_a_worktree_agent_serves_its_repo(self) -> None:
        worktree = self.workspace / "myrepo" / ".worktrees" / "refund"
        worktree.mkdir(parents=True)
        code, _, _ = self.cli(
            "dispatch",
            "maker",
            "myrepo",
            "ship: do it",
            env=self.fake_env(CLOWDER_FAKE_CWD=str(worktree)),
        )
        self.assertEqual(code, 0, "a pane inside a worktree of the repo is fine")

    def test_force_overrides_the_cross_repo_refusal(self) -> None:
        other = self.workspace / "other"
        other.mkdir()
        code, _, err = self.cli(
            "dispatch",
            "maker",
            "myrepo",
            "ship: do it",
            "--force",
            env=self.fake_env(CLOWDER_FAKE_CWD=str(other)),
        )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_dry_run_sends_nothing(self) -> None:
        code, out, _ = self.cli(
            "dispatch",
            "maker",
            "myrepo",
            "ship: do it",
            "--dry-run",
            env=self.fake_env(),
        )
        self.assertEqual(code, 0)
        self.assertIn("would run", out)
        self.assertIn("main checkout", out)
        self.assertIn("clowder job t-0001", out)
        self.assertFalse(self.state.exists())

    def test_a_rejected_prompt_is_recorded_as_failed(self) -> None:
        code, _, err = self.cli(
            "dispatch",
            "maker",
            "myrepo",
            "ship: do it",
            env=self.fake_env(CLOWDER_FAKE_PROMPT_FAIL="1"),
        )
        self.assertEqual(code, 2)
        self.assertIn("agent_blocked", err)
        task = self.only_task()
        self.assertEqual(task["status"], "failed")
        self.assertIn("agent_blocked", task["mux_error"])

    def test_a_brief_can_come_from_a_file(self) -> None:
        brief_path = self.root / "brief.md"
        brief_path.write_text("ship: 1. do it\n2. test it\n", encoding="utf-8")
        code, _, _ = self.cli(
            "dispatch",
            "maker",
            "myrepo",
            "--brief-file",
            str(brief_path),
            env=self.fake_env(),
        )
        self.assertEqual(code, 0)
        task = self.only_task()
        self.assertIn("2. test it", task["brief"])
        self.assertEqual(task["question"], "ship: 1. do it")

    def test_brief_and_brief_file_together_is_an_error(self) -> None:
        code, _, err = self.cli(
            "dispatch",
            "maker",
            "myrepo",
            "ship: do it",
            "--brief-file",
            str(self.root / "brief.md"),
            env=self.fake_env(),
        )
        self.assertEqual(code, 1)
        self.assertIn("not both", err)

    def test_a_missing_brief_is_an_error(self) -> None:
        code, _, err = self.cli("dispatch", "maker", "myrepo", env=self.fake_env())
        self.assertEqual(code, 1)
        self.assertIn("brief is required", err)

    def test_an_overlong_brief_is_refused(self) -> None:
        code, _, err = self.cli(
            "dispatch", "maker", "myrepo", "ship: " + "x" * 9000, env=self.fake_env()
        )
        self.assertEqual(code, 2)
        self.assertIn("--brief-file", err)
        self.assertFalse(self.state.exists())

    def test_a_brief_that_omits_the_shape_is_noted(self) -> None:
        code, _, err = self.cli(
            "dispatch", "maker", "myrepo", "look into the billing page", env=self.fake_env()
        )
        self.assertEqual(code, 0)
        self.assertIn("does not say", err)

    def test_worktree_must_exist(self) -> None:
        code, _, err = self.cli(
            "dispatch",
            "maker",
            "myrepo",
            "ship: do it",
            "--worktree",
            str(self.root / "nope"),
            env=self.fake_env(),
        )
        self.assertEqual(code, 1)
        self.assertIn("not a directory", err)

    def test_worktree_is_recorded_and_named_in_the_report(self) -> None:
        worktree = self.workspace / "myrepo" / ".worktrees" / "refund-policy"
        worktree.mkdir(parents=True)
        self.cli(
            "dispatch",
            "maker",
            "myrepo",
            "ship: refund policy",
            "--worktree",
            str(worktree),
            env=self.fake_env(),
        )
        self.assertEqual(self.only_task()["worktree"], str(worktree))
        code, out, _ = self.cli("report", "t-0001", env=self.fake_env())
        self.assertEqual(code, 0)
        self.assertIn("worktree refund-policy", out)

    # -- tasks -------------------------------------------------------------

    def test_tasks_lists_the_dispatched_task(self) -> None:
        self.dispatch("maker", "myrepo", "ship: add the refund page")
        code, out, _ = self.cli("tasks", env=self.fake_env())
        self.assertEqual(code, 0)
        self.assertIn("t-0001", out)
        self.assertIn("dispatched", out)
        self.assertIn("add the refund page", out)
        self.assertIn("1 task(s), 1 still open", out)

    def test_tasks_open_filter(self) -> None:
        self.dispatch("maker", "myrepo", "ship: add the refund page")
        self.session_turns(count=1, after_dispatch=True)
        self.cli("report", "t-0001", env=self.fake_env())

        _, out, _ = self.cli("tasks", "--open", env=self.fake_env())
        self.assertIn("no tasks", out)
        _, out, _ = self.cli("tasks", "--json", env=self.fake_env())
        payload = json.loads(out)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["tasks"][0]["status"], "reported")

    def test_tasks_on_an_empty_store(self) -> None:
        code, out, _ = self.cli("tasks", env=self.fake_env())
        self.assertEqual(code, 0)
        self.assertIn("no tasks", out)

    def test_unknown_task_id_exits_one(self) -> None:
        code, _, err = self.cli("report", "t-4242", env=self.fake_env())
        self.assertEqual(code, 1)
        self.assertIn("t-4242", err)

    # -- report ------------------------------------------------------------

    def session_turns(self, count: int = 1, after_dispatch: bool = True) -> None:
        now_ms = int(time.time() * 1000)
        turns = []
        if after_dispatch:
            turns.append(
                {
                    "at": now_ms - 300_000,
                    "text": "an older task's answer",
                    "usage": default_usage(9999, 999),
                }
            )
        for index in range(count):
            turns.append(
                {
                    "at": now_ms + 1000 + index,
                    "text": f"the answer ({index})",
                    "usage": default_usage(1000, 200, cost_total=0.002),
                }
            )
        write_session(self.session_file, cwd=str(self.workspace / "myrepo"), turns=turns)

    def test_report_reads_the_session_file(self) -> None:
        self.dispatch("maker", "myrepo", "ship: add the refund page")
        self.session_turns()
        code, out, _ = self.cli("report", "t-0001", env=self.fake_env())

        self.assertEqual(code, 0)
        self.assertIn("Re: ship: add the refund page", out)
        self.assertIn("maker | myrepo | main checkout", out)
        self.assertIn("the answer (0)", out)
        self.assertIn("1.2k", out)

        task = self.only_task()
        self.assertEqual(task["status"], "reported")
        self.assertEqual(task["answer"], "the answer (0)")
        self.assertEqual(task["answer_source"], "session")
        self.assertIsNotNone(task["reported_at"])
        self.assertEqual(task["usage"]["turns"], 1, "earlier turns are excluded")
        self.assertEqual(task["usage"]["total_tokens"], 1200)

    def test_report_does_not_settle_a_working_agent(self) -> None:
        self.dispatch("maker", "myrepo", "ship: add the refund page")
        self.session_turns()
        code, out, _ = self.cli(
            "report", "t-0001", env=self.fake_env(CLOWDER_FAKE_STATUS="working")
        )
        self.assertEqual(code, 0)
        self.assertIn("working", out)
        task = self.only_task()
        self.assertEqual(task["status"], "dispatched")
        self.assertIsNone(task["answer"])

    def test_report_before_any_answer_says_so(self) -> None:
        self.dispatch("maker", "myrepo", "ship: add the refund page")
        code, out, _ = self.cli("report", "t-0001", env=self.fake_env())
        self.assertEqual(code, 0)
        self.assertIn("no answer yet", out)

    def test_report_no_save_leaves_the_record_alone(self) -> None:
        self.dispatch("maker", "myrepo", "ship: add the refund page")
        self.session_turns()
        before = self.state.read_text(encoding="utf-8")
        code, out, _ = self.cli("report", "t-0001", "--no-save", env=self.fake_env())
        self.assertEqual(code, 0)
        self.assertIn("the answer (0)", out)
        self.assertEqual(self.state.read_text(encoding="utf-8"), before)

    def test_report_records_one_open_decision(self) -> None:
        self.dispatch("maker", "myrepo", "ship: add the refund page")
        self.session_turns()
        _, out, _ = self.cli(
            "report",
            "t-0001",
            "--open-decision",
            "14 days or 30?",
            env=self.fake_env(),
        )
        self.assertIn("Open decision: 14 days or 30?", out)
        self.assertEqual(self.only_task()["open_decision"], "14 days or 30?")

    def test_report_decide_answers_a_decision_and_it_leaves_the_board(self) -> None:
        self.dispatch("maker", "myrepo", "ship: add the refund page")
        self.session_turns()
        self.cli("report", "t-0001", "--open-decision", "14 days or 30?", env=self.fake_env())
        page = (self.root / "board.html").read_text(encoding="utf-8")
        self.assertIn("14 days or 30?", page, "the open decision waits on the owner")

        code, _, err = self.cli("report", "t-0001", "--decide", "14 days", env=self.fake_env())
        self.assertEqual(code, 0, err)
        task = self.only_task()
        self.assertEqual(task["decision_answer"], "14 days")
        self.assertTrue(task["decision_answered_at"])
        page = (self.root / "board.html").read_text(encoding="utf-8")
        self.assertNotIn("14 days or 30?", page, "an answered decision leaves his section")

    def test_report_json_carries_state_usage_and_answer(self) -> None:
        self.dispatch("maker", "myrepo", "ship: add the refund page")
        self.session_turns()
        code, out, _ = self.cli("report", "t-0001", "--json", env=self.fake_env())
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["task"]["status"], "reported")
        self.assertEqual(payload["answer"], "the answer (0)")
        self.assertEqual(payload["usage"]["turns"], 1)
        self.assertEqual(payload["agent_status"], "idle")
        self.assertEqual(payload["session_file"], str(self.session_file))

    def test_report_verbose_shows_the_breakdown(self) -> None:
        self.dispatch("maker", "myrepo", "ship: add the refund page")
        self.session_turns()
        _, out, _ = self.cli("report", "t-0001", "--verbose", env=self.fake_env())
        self.assertIn("turns: 1", out)
        self.assertIn("test-provider", out)
        self.assertIn(str(self.session_file), out)

    def test_report_survives_a_missing_session_file(self) -> None:
        self.dispatch("maker", "myrepo", "ship: add the refund page")
        self.session_file.unlink()
        code, out, _ = self.cli("report", "t-0001", env=self.fake_env())
        self.assertEqual(code, 0)
        self.assertIn("no answer yet", out)

    # -- ensure ------------------------------------------------------------

    def seed_mux(self, agents: list[dict[str, object]]) -> str:
        path = self.root / "mux-state.json"
        write_fake_state(path, agents=agents)
        return str(path)

    def test_ensure_reuses_the_agent_that_already_serves_the_repo(self) -> None:
        self.seed_mux([{"name": "verifier", "cwd": str(self.workspace / "myrepo")}])
        code, out, _ = self.cli("ensure", "myrepo", "--role", "verifier", env=self.state_env())
        self.assertEqual(code, 0)
        self.assertIn("verifier serves myrepo (already live)", out)

    def test_ensure_creates_an_agent_when_the_repo_has_none(self) -> None:
        self.seed_mux([])
        code, out, _ = self.cli("ensure", "myrepo", "--role", "maker", env=self.state_env())
        self.assertEqual(code, 0)
        self.assertIn("myrepo-maker serves myrepo (made)", out)

        # And the next call finds it, rather than making a second one.
        code, out, _ = self.cli("ensure", "myrepo", "--role", "maker", env=self.state_env())
        self.assertEqual(code, 0)
        self.assertIn("(already live)", out)

    def test_ensure_starts_the_agent_in_the_pane_the_move_created(self) -> None:
        # A move gives the pane a new id (seen in a real run: wB:p8 became w1:p7),
        # so the id `pane split` returned is stale. Starting the agent in it fails
        # with agent_pane_not_found, which blocked creating any agent in a repo
        # that already had one.
        self.seed_mux([{"name": "verifier", "cwd": str(self.repo_path), "workspace_id": "w1"}])
        code, out, err = self.cli("ensure", "myrepo", "--role", "maker", env=self.state_env())
        self.assertEqual(code, 0, err)
        self.assertIn("myrepo-maker serves myrepo (made)", out)
        self.assertIn("pane w1:p", out)

        state = json.loads((self.root / "mux-state.json").read_text(encoding="utf-8"))
        split_id = state["moves"][0]["pane_id"]
        made = next(a for a in state["agents"] if a["name"] == "myrepo-maker")
        self.assertNotEqual(made["pane_id"], split_id, "the agent is in the moved pane")
        self.assertTrue(made["pane_id"].startswith("w1:p"), made["pane_id"])
        self.assertEqual(made["workspace_id"], "w1")

    def test_ensure_reports_what_is_missing_without_creating(self) -> None:
        self.seed_mux([])
        code, _, err = self.cli(
            "ensure", "myrepo", "--role", "maker", "--no-create", env=self.state_env()
        )
        self.assertEqual(code, 3)
        self.assertIn("not allowed", err)
        self.assertIn("--no-create", err)

    def test_ensure_names_the_agents_when_it_cannot_choose(self) -> None:
        self.seed_mux(
            [
                {"name": "maker", "cwd": str(self.workspace / "myrepo")},
                {"name": "verifier", "cwd": str(self.workspace / "myrepo")},
            ]
        )
        code, _, err = self.cli("ensure", "myrepo", "--role", "teacher", env=self.state_env())
        self.assertEqual(code, 3)
        self.assertIn("maker, verifier", err)
        self.assertIn("none matches the role", err)

    def test_ensure_refuses_a_live_name_from_another_repo(self) -> None:
        other = self.workspace / "other"
        other.mkdir()
        self.seed_mux([{"name": "myrepo-maker", "cwd": str(other)}])
        code, _, err = self.cli("ensure", "myrepo", "--role", "maker", env=self.state_env())
        self.assertEqual(code, 3)
        self.assertIn("already exists", err)
        self.assertIn(str(other), err)

    def test_ensure_json_says_what_it_did(self) -> None:
        self.seed_mux([])
        code, out, _ = self.cli(
            "ensure", "myrepo", "--role", "verifier", "--json", env=self.state_env()
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["created"])
        self.assertEqual(payload["agent"]["name"], "myrepo-verifier")
        # A made agent works in its own checkout, not in the human's.
        expected = self.repo_path / ".worktrees" / "myrepo-verifier"
        self.assertEqual(Path(payload["agent"]["cwd"]), expected)
        self.assertEqual(payload["worktree"], str(expected))
        self.assertEqual(payload["role"], "verifier")

    def test_ensure_then_dispatch_is_the_whole_flow(self) -> None:
        self.seed_mux([])
        code, _, _ = self.cli("ensure", "myrepo", "--role", "maker", env=self.state_env())
        self.assertEqual(code, 0)
        code, out, err = self.cli(
            "dispatch",
            "myrepo-maker",
            "myrepo",
            "ship: add the refund page",
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        self.assertIn("myrepo-maker", out)

    def test_a_created_agent_can_be_dispatched_to_and_reported_on(self) -> None:
        self.seed_mux([])
        self.cli("ensure", "myrepo", "--role", "maker", env=self.state_env())

        code, _, err = self.cli(
            "dispatch",
            "myrepo-maker",
            "myrepo",
            "ship: add the refund page",
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)

        # The worker answers after it is dispatched, in its own new session file.
        session = Path(str(self.state_env()["CLOWDER_FAKE_SESSION_DIR"])) / "myrepo-maker.jsonl"
        write_session(
            session,
            cwd=str(self.workspace / "myrepo"),
            turns=[
                {
                    "at": int(time.time() * 1000) + 1000,
                    "text": "the refund page is missing",
                    "usage": default_usage(),
                }
            ],
        )

        code, out, _ = self.cli("report", "t-0001", env=self.state_env())
        self.assertEqual(code, 0)
        self.assertIn("the refund page is missing", out)
        self.assertIn("myrepo-maker | myrepo", out)

    # -- jobs --------------------------------------------------------------

    def open_job(self, *extra: str, env: dict[str, object] | None = None):
        return self.cli(
            "job",
            "open",
            "myrepo",
            "--label",
            "refund",
            "--role",
            "maker",
            *extra,
            env=env or self.state_env(),
        )

    def test_job_open_makes_a_worktree_and_switches_a_branch(self) -> None:
        self.seed_mux([])
        code, out, err = self.open_job()
        self.assertEqual(code, 0, err)
        self.assertIn("j-0001 open: task/refund", out)

        worktree = self.repo_path / ".worktrees" / "myrepo-maker"
        self.assertTrue(worktree.is_dir(), "the agent gets a checkout of its own")
        self.assertEqual(gitcmd.current_branch(worktree), "task/refund")
        self.assertTrue(gitcmd.branch_exists(self.repo_path, "task/refund"))
        # The human's checkout is untouched.
        self.assertEqual(gitcmd.current_branch(self.repo_path), "main")

        listing = json.loads(self.cli("job", "list", "--json", env=self.state_env())[1])
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["jobs"][0]["status"], "open")
        self.assertEqual(listing["jobs"][0]["commit"], gitcmd.head_commit(worktree))

    def test_job_open_refuses_a_dirty_worktree(self) -> None:
        # The case that matters: work was left behind with no job open, so the tool
        # cannot tell whose it is.
        self.seed_mux([])
        self.cli("ensure", "myrepo", "--role", "maker", env=self.state_env())
        worktree = self.repo_path / ".worktrees" / "myrepo-maker"
        (worktree / "left-over.txt").write_text("x", encoding="utf-8")

        code, _, err = self.open_job()
        self.assertEqual(code, 2)
        self.assertIn("is not clean", err)
        self.assertIn("left-over.txt", err)
        self.assertIsNone(gitcmd.current_branch(worktree), "still free: nothing switched")

    def test_reopening_a_job_with_work_in_progress_is_allowed(self) -> None:
        # Dirt inside an open job is that job's own work in progress.
        self.seed_mux([])
        self.assertEqual(self.open_job()[0], 0)
        worktree = self.repo_path / ".worktrees" / "myrepo-maker"
        (worktree / "half-done.txt").write_text("x", encoding="utf-8")

        code, out, err = self.open_job(env=self.state_env())
        self.assertEqual(code, 0, err)
        self.assertIn("j-0001 already open", out)
        self.assertEqual(gitcmd.current_branch(worktree), "task/refund")

    def test_job_open_refuses_the_main_checkout(self) -> None:
        # An agent sitting in the repo itself must not have branches switched under
        # the human's feet.
        self.seed_mux([{"name": "maker", "cwd": str(self.repo_path)}])
        code, _, err = self.open_job()
        self.assertEqual(code, 2)
        self.assertIn("main checkout", err)
        self.assertEqual(gitcmd.current_branch(self.repo_path), "main")

    def test_job_close_returns_the_worktree(self) -> None:
        self.seed_mux([])
        self.assertEqual(self.open_job()[0], 0)
        worktree = self.repo_path / ".worktrees" / "myrepo-maker"

        code, out, err = self.cli("job", "close", "j-0001", env=self.state_env())
        self.assertEqual(code, 0, err)
        self.assertIn("space free again on main", out)
        self.assertIsNone(gitcmd.current_branch(worktree), "freed: no branch name")
        self.assertTrue(gitcmd.is_detached(worktree))
        self.assertEqual(gitcmd.head_commit(worktree), gitcmd.head_commit(self.repo_path))
        self.assertTrue(
            gitcmd.branch_exists(self.repo_path, "task/refund"),
            "the branch is the record and is kept",
        )

        listing = json.loads(
            self.cli("job", "list", "--all", "--json", env=self.state_env())[1]
        )
        self.assertEqual(listing["jobs"][0]["status"], "closed")

    def test_a_freed_space_keeps_its_local_state(self) -> None:
        # The whole reason for a space rather than a fresh checkout: the install and
        # the caches stay put. They are ignored by git, so a real venv stands in here.
        self.seed_mux([])
        self.assertEqual(self.open_job()[0], 0)
        worktree = self.repo_path / ".worktrees" / "myrepo-maker"
        (worktree / ".gitignore").write_text(".venv-marker\n", encoding="utf-8")
        gitcmd.run_git(worktree, "add", ".gitignore")
        gitcmd.run_git(worktree, "commit", "-m", "ignore the install")
        (worktree / ".venv-marker").write_text("installed\n", encoding="utf-8")
        (worktree / "tracked.py").write_text("x\n", encoding="utf-8")
        gitcmd.run_git(worktree, "add", "tracked.py")
        gitcmd.run_git(worktree, "commit", "-m", "work")

        code, _, err = self.cli("job", "close", "j-0001", env=self.state_env())
        self.assertEqual(code, 0, err)
        self.assertTrue((worktree / ".venv-marker").is_file(), "the install stayed")
        self.assertFalse((worktree / "tracked.py").is_file(), "the work left with the branch")

    def test_space_reuse_takes_a_fresh_base(self) -> None:
        self.seed_mux([])
        self.assertEqual(self.open_job()[0], 0)
        self.assertEqual(self.cli("job", "close", "j-0001", env=self.state_env())[0], 0)
        # The base moves on while the space is free.
        (self.repo_path / "later.py").write_text("y\n", encoding="utf-8")
        gitcmd.run_git(self.repo_path, "add", "later.py")
        gitcmd.run_git(self.repo_path, "commit", "-m", "base moves on")

        # A different label, so a new branch is made from the base as it is now.
        code, _, err = self.open_job("--label", "second")
        self.assertEqual(code, 0, err)
        worktree = self.repo_path / ".worktrees" / "myrepo-maker"
        self.assertTrue(
            (worktree / "later.py").is_file(),
            "a reused space starts from the current base, not a stale one",
        )

    def test_report_warns_about_work_that_is_on_no_branch(self) -> None:
        # The gate, where a worker makes its claim. A reviewer commits while pinned
        # at the writer's save, so its commit hangs on no branch at all.
        job_id, maker_folder = self.open_a_job()
        # The writer must save, or there is nothing to hand over.
        self.save_something(maker_folder)
        self.assertEqual(
            self.cli(
                "job",
                "handover",
                job_id,
                "--to",
                "verifier",
                "--name",
                "myrepo-verifier",
                env=self.state_env(),
            )[0],
            0,
        )
        self.assertEqual(
            self.cli(
                "dispatch",
                "myrepo-verifier",
                "myrepo",
                "scout: check this",
                "--job",
                job_id,
                env=self.state_env(),
            )[0],
            0,
        )
        verifier_folder = self.repo_path / ".worktrees" / "myrepo-verifier"
        self.save_something(verifier_folder, "review-notes.py")
        self.assertTrue(gitcmd.is_detached(verifier_folder))
        self.assertFalse(
            gitcmd.is_reachable(
                verifier_folder, gitcmd.head_commit(verifier_folder, short=False) or ""
            )
        )

        code, out, _ = self.cli("report", "t-0001", env=self.state_env())
        self.assertEqual(code, 0)
        self.assertIn("is on no branch", out)

        payload = json.loads(self.cli("report", "t-0001", "--json", env=self.state_env())[1])
        self.assertTrue(payload["commit_on_no_branch"])

    def test_a_delivery_mode_that_is_not_built_is_refused(self) -> None:
        self.config = write_config(
            self.root / "pr.toml",
            workspace={"root": str(self.workspace)},
            mux={"bin": str(self.launcher)},
            state={"path": str(self.state)},
            worktree={"delivery": "direct-PR"},
        )
        self.seed_mux([])
        code, _, err = self.open_job()
        self.assertEqual(code, 1)
        self.assertIn("only local-only is built", err)
        self.assertIn("would be a lie", err)

    def test_job_close_refuses_when_dirty(self) -> None:
        self.seed_mux([])
        self.assertEqual(self.open_job()[0], 0)
        worktree = self.repo_path / ".worktrees" / "myrepo-maker"
        (worktree / "unfinished.txt").write_text("x", encoding="utf-8")
        code, _, err = self.cli("job", "close", "j-0001", env=self.state_env())
        self.assertEqual(code, 2)
        self.assertIn("is not clean", err)
        self.assertEqual(gitcmd.current_branch(worktree), "task/refund")

    def test_job_open_refuses_from_the_wrong_branch(self) -> None:
        self.seed_mux([])
        self.assertEqual(self.open_job()[0], 0)
        worktree = self.repo_path / ".worktrees" / "myrepo-maker"
        gitcmd.switch_new_branch(worktree, "scratch", "main")
        code, _, err = self.cli("job", "close", "j-0001", env=self.state_env())
        self.assertEqual(code, 2)
        self.assertIn("is on scratch, not task/refund", err)
        self.assertEqual(gitcmd.current_branch(worktree), "scratch")

    def test_job_open_twice_needs_the_first_one_closed(self) -> None:
        self.seed_mux([])
        self.assertEqual(self.open_job()[0], 0)
        code, _, err = self.cli(
            "job",
            "open",
            "myrepo",
            "--label",
            "billing",
            "--role",
            "maker",
            env=self.state_env(),
        )
        self.assertEqual(code, 2, "a second job would switch out from under the first")
        self.assertIn("j-0001 is still open", err)
        self.assertIn("task/refund", err)

    def test_job_open_is_idempotent_for_the_same_job(self) -> None:
        self.seed_mux([])
        self.assertEqual(self.open_job()[0], 0)
        code, out, err = self.open_job(env=self.state_env())
        self.assertEqual(code, 0, err)
        self.assertIn("j-0001 already open", out)

    def test_a_job_can_resume_an_existing_branch(self) -> None:
        self.seed_mux([])
        self.assertEqual(self.open_job()[0], 0)
        self.assertEqual(self.cli("job", "close", "j-0001", env=self.state_env())[0], 0)
        # Starting the same line of work again resumes the same branch.
        code, out, err = self.open_job(env=self.state_env())
        self.assertEqual(code, 0, err)
        worktree = self.repo_path / ".worktrees" / "myrepo-maker"
        self.assertEqual(gitcmd.current_branch(worktree), "task/refund")
        self.assertIn("j-0002 open", out)

    def test_unknown_job_id_exits_one(self) -> None:
        code, _, err = self.cli("job", "close", "j-9999", env=self.state_env())
        self.assertEqual(code, 1)
        self.assertIn("j-9999", err)

    def test_job_list_on_an_empty_store(self) -> None:
        code, out, _ = self.cli("job", "list", env=self.fake_env())
        self.assertEqual(code, 0)
        self.assertIn("no open jobs", out)

    def test_job_open_refuses_a_repo_that_is_not_git(self) -> None:
        plain = self.workspace / "plain"
        plain.mkdir()
        code, _, err = self.cli(
            "job", "open", "plain", "--label", "thing", env=self.state_env()
        )
        self.assertEqual(code, 2)
        self.assertIn("not a git repository", err)

    def test_ensure_name_puts_the_agent_in_its_own_worktree(self) -> None:
        self.seed_mux([])
        code, out, err = self.cli(
            "ensure",
            "myrepo",
            "--role",
            "maker",
            "--name",
            "myrepo-wt",
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        self.assertIn("myrepo-wt serves myrepo (made)", out)
        worktree = self.repo_path / ".worktrees" / "myrepo-wt"
        self.assertTrue(worktree.is_dir())
        self.assertIn(str(worktree), out)
        # A new space is free: no branch name on it, sitting on the base.
        self.assertIsNone(gitcmd.current_branch(worktree))
        self.assertTrue(gitcmd.is_detached(worktree))
        self.assertEqual(gitcmd.head_commit(worktree), gitcmd.head_commit(self.repo_path))

    def test_ensure_refuses_an_unusable_explicit_name(self) -> None:
        self.seed_mux([])
        code, _, err = self.cli(
            "ensure",
            "myrepo",
            "--role",
            "maker",
            "--name",
            "Bad Name",
            env=self.state_env(),
        )
        self.assertEqual(code, 1)
        self.assertIn("not a usable agent name", err)

    # -- a step in a job ---------------------------------------------------

    def open_a_job(self, label: str = "refund") -> tuple[str, Path]:
        self.seed_mux([])
        code, out, err = self.cli(
            "job",
            "open",
            "myrepo",
            "--label",
            label,
            "--role",
            "maker",
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        return out.split()[0], self.repo_path / ".worktrees" / "myrepo-maker"

    def step(self, *args: str):
        return self.cli("dispatch", "myrepo-maker", "myrepo", *args, env=self.state_env())

    def answer_the_step(self, session_name: str = "myrepo-maker") -> None:
        session = (
            Path(str(self.state_env()["CLOWDER_FAKE_SESSION_DIR"])) / f"{session_name}.jsonl"
        )
        write_session(
            session,
            cwd=str(self.repo_path),
            turns=[
                {
                    "at": int(time.time() * 1000) + 1000,
                    "text": "the refund page is missing",
                    "usage": default_usage(),
                }
            ],
        )

    def owner_pass(self, job_id: str) -> None:
        code, _, err = self.cli(
            "job",
            "pass",
            job_id,
            "--shown",
            "the diff of task/refund",
            "--answer",
            "yes, ship it",
            "--by",
            "lacattano",
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)

    def handover(self, job_id: str) -> None:
        code, _, err = self.cli(
            "job",
            "handover",
            job_id,
            "--to",
            "verifier",
            "--name",
            "myrepo-verifier",
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)

    def test_a_pass_records_what_was_shown_and_when(self) -> None:
        job_id, _ = self.open_a_job()
        self.owner_pass(job_id)
        job = json.loads(self.state.read_text(encoding="utf-8"))["jobs"][job_id]
        self.assertEqual(job["pass_shown"], "the diff of task/refund")
        self.assertEqual(job["pass_answer"], "yes, ship it")
        self.assertEqual(job["pass_by"], "lacattano")
        self.assertTrue(job["pass_at"])

    def test_a_worker_cannot_record_the_owner_s_pass(self) -> None:
        job_id, _ = self.open_a_job()
        code, _, err = self.cli(
            "job",
            "pass",
            job_id,
            "--shown",
            "the diff",
            "--answer",
            "yes",
            "--by",
            "maker",
            env=self.state_env(),
        )
        self.assertEqual(code, 1)
        self.assertIn("worker role", err)
        job = json.loads(self.state.read_text(encoding="utf-8"))["jobs"][job_id]
        self.assertIsNone(job["pass_at"], "a worker's word is not recorded")

    def test_a_reviewer_cannot_record_the_owner_s_pass_or_word(self) -> None:
        job_id, _ = self.open_a_job()
        code, _, err = self.cli(
            "job",
            "pass",
            job_id,
            "--shown",
            "the diff",
            "--answer",
            "yes",
            "--by",
            "verifier",
            env=self.state_env(),
        )
        self.assertEqual(code, 1)
        self.assertIn("worker role", err)
        code, _, err = self.cli(
            "job", "word", job_id, "--word", "merge", "--by", "teacher", env=self.state_env()
        )
        self.assertEqual(code, 1)
        self.assertIn("worker role", err)
        job = json.loads(self.state.read_text(encoding="utf-8"))["jobs"][job_id]
        self.assertIsNone(job["pass_at"])
        self.assertIsNone(job["merge_word_at"])

    def test_publish_is_refused_without_a_pass(self) -> None:
        job_id, _ = self.open_a_job()
        code, _, err = self.cli("job", "publish", job_id, env=self.state_env())
        self.assertEqual(code, 1)
        self.assertIn("no owner's pass", err)
        self.assertIn(f"job pass {job_id}", err)

    def test_publish_proceeds_once_the_pass_is_recorded(self) -> None:
        job_id, _ = self.open_a_job()
        self.owner_pass(job_id)
        with mock.patch.object(cli, "_publish_branch") as publish:
            code, out, err = self.cli("job", "publish", job_id, env=self.state_env())
        self.assertEqual(code, 0, err)
        publish.assert_called_once()
        self.assertIn("published", out)
        job = json.loads(self.state.read_text(encoding="utf-8"))["jobs"][job_id]
        self.assertTrue(job["published_at"])

    def test_merge_is_refused_without_the_owner_s_word(self) -> None:
        job_id, _ = self.open_a_job()
        self.owner_pass(job_id)
        code, _, err = self.cli("job", "merge", job_id, "--pr", "9", env=self.state_env())
        self.assertEqual(code, 1)
        self.assertIn("no owner's merge word", err)
        self.assertIn(f"job word {job_id}", err)

    def test_merge_proceeds_once_the_word_is_recorded(self) -> None:
        job_id, _ = self.open_a_job()
        code, _, err = self.cli(
            "job",
            "word",
            job_id,
            "--word",
            "merge it",
            "--by",
            "lacattano",
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        with mock.patch.object(cli, "_merge_pull_request") as merge:
            code, out, err = self.cli("job", "merge", job_id, "--pr", "9", env=self.state_env())
        self.assertEqual(code, 0, err)
        merge.assert_called_once()
        self.assertIn("merged", out)
        job = json.loads(self.state.read_text(encoding="utf-8"))["jobs"][job_id]
        self.assertTrue(job["merged_at"])
        self.assertEqual(job["merge_word"], "merge it")
        self.assertEqual(job["merge_word_by"], "lacattano")

    def test_deleting_a_branch_needs_the_owner_s_word(self) -> None:
        job_id, _ = self.open_a_job()
        code, _, err = self.cli("job", "close", job_id, "--delete-branch", env=self.state_env())
        self.assertEqual(code, 1)
        self.assertIn("without the owner's word", err)
        self.assertIn(f"job word {job_id}", err)
        job = json.loads(self.state.read_text(encoding="utf-8"))["jobs"][job_id]
        self.assertEqual(job["status"], "open", "a refused close changes nothing")

    def test_job_list_shows_the_two_gates(self) -> None:
        job_id, _ = self.open_a_job()
        self.owner_pass(job_id)
        code, out, err = self.cli("job", "list", env=self.state_env())
        self.assertEqual(code, 0, err)
        self.assertIn("PASS", out)
        self.assertIn("WORD", out)
        self.assertIn("yes", out)

    def test_inbox_lists_a_step_whose_agent_has_finished(self) -> None:
        job_id, _ = self.open_a_job()
        self.assertEqual(self.step("--job", job_id, "ship: add the refund page")[0], 0)
        self.answer_the_step()
        code, out, err = self.cli("inbox", env=self.state_env())
        self.assertEqual(code, 0, err)
        self.assertIn("t-0001", out)
        self.assertIn("the refund page is missing", out)
        self.assertIn("1 step(s) have reported", out)

    def test_inbox_is_empty_before_an_answer(self) -> None:
        job_id, _ = self.open_a_job()
        self.assertEqual(self.step("--job", job_id, "ship: add the refund page")[0], 0)
        code, out, err = self.cli("inbox", env=self.state_env())
        self.assertEqual(code, 0, err)
        self.assertIn("nothing has reported", out)

    def test_inbox_drops_a_step_once_its_report_is_read(self) -> None:
        job_id, _ = self.open_a_job()
        self.assertEqual(self.step("--job", job_id, "ship: add the refund page")[0], 0)
        self.answer_the_step()
        self.assertEqual(self.cli("report", "t-0001", env=self.state_env())[0], 0)
        code, out, _ = self.cli("inbox", env=self.state_env())
        self.assertEqual(code, 0)
        self.assertIn("nothing has reported", out)

    def test_inbox_skips_an_agent_still_working(self) -> None:
        job_id, _ = self.open_a_job()
        self.assertEqual(self.step("--job", job_id, "ship: add the refund page")[0], 0)
        self.answer_the_step()
        # The worker is mid-turn, so its words are not a report yet.
        mux_state = self.root / "mux-state.json"
        payload = json.loads(mux_state.read_text(encoding="utf-8"))
        payload["agents"][0]["status"] = "working"
        mux_state.write_text(json.dumps(payload), encoding="utf-8")
        code, out, _ = self.cli("inbox", env=self.state_env())
        self.assertEqual(code, 0)
        self.assertIn("nothing has reported", out)

    def test_inbox_json_names_the_answer(self) -> None:
        job_id, _ = self.open_a_job()
        self.assertEqual(self.step("--job", job_id, "ship: add the refund page")[0], 0)
        self.answer_the_step()
        code, out, _ = self.cli("inbox", "--json", env=self.state_env())
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["reported"][0]["id"], "t-0001")
        self.assertIn("the refund page is missing", payload["reported"][0]["answer"])

    def test_a_step_records_its_job_branch_and_commit(self) -> None:
        job_id, worktree = self.open_a_job()
        code, out, err = self.step("--job", job_id, "ship: add the refund page")
        self.assertEqual(code, 0, err)
        self.assertIn("myrepo-maker", out)

        task = self.only_task()
        self.assertEqual(task["job"], job_id)
        self.assertEqual(task["branch"], "task/refund")
        self.assertEqual(task["worktree"], str(worktree))
        self.assertEqual(task["commit"], gitcmd.head_commit(worktree))

    def test_a_second_step_waits_for_the_first_to_report(self) -> None:
        job_id, _ = self.open_a_job()
        self.assertEqual(self.step("--job", job_id, "ship: add the refund page")[0], 0)

        code, _, err = self.step("--job", job_id, "ship: now do something else")
        self.assertEqual(code, 2)
        self.assertIn("already has an open step", err)
        self.assertIn("t-0001 (myrepo-maker)", err)

    def test_the_next_step_goes_once_the_first_has_reported(self) -> None:
        job_id, _ = self.open_a_job()
        self.assertEqual(self.step("--job", job_id, "ship: add the refund page")[0], 0)
        self.answer_the_step()
        self.assertEqual(self.cli("report", "t-0001", env=self.state_env())[0], 0)

        code, out, err = self.step("--job", job_id, "ship: now run the tests")
        self.assertEqual(code, 0, err)
        self.assertIn("t-0002", out)
        self.assertEqual(len(self.load_state()["tasks"]), 2)

    def test_a_step_for_the_wrong_agent_is_refused(self) -> None:
        job_id, _ = self.open_a_job()
        code, _, err = self.cli(
            "dispatch",
            "verifier",
            "myrepo",
            "ship: do it",
            "--job",
            job_id,
            env=self.state_env(),
        )
        self.assertEqual(code, 2)
        self.assertIn(f"{job_id} is myrepo-maker's job", err)
        self.assertIn("The writer and its reviewer take the steps", err)

    def test_a_step_must_run_in_the_job_checkout(self) -> None:
        job_id, _ = self.open_a_job()
        state = self.root / "mux-state.json"
        payload = json.loads(state.read_text(encoding="utf-8"))
        payload["agents"][0]["cwd"] = str(self.repo_path)
        state.write_text(json.dumps(payload), encoding="utf-8")

        code, _, err = self.step("--job", job_id, "ship: do it")
        self.assertEqual(code, 2)
        self.assertIn("which holds neither", err)
        self.assertIn(job_id, err)

    def test_job_and_worktree_together_is_refused(self) -> None:
        job_id, worktree = self.open_a_job()
        code, _, err = self.step("--job", job_id, "--worktree", str(worktree), "ship: x")
        self.assertEqual(code, 1)
        self.assertIn("not both", err)

    def test_a_step_on_a_closed_job_is_refused(self) -> None:
        job_id, _ = self.open_a_job()
        self.assertEqual(self.cli("job", "close", job_id, env=self.state_env())[0], 0)
        code, _, err = self.step("--job", job_id, "ship: do it")
        self.assertEqual(code, 2)
        self.assertIn("is closed", err)

    def test_job_close_waits_for_an_open_step(self) -> None:
        job_id, _ = self.open_a_job()
        self.assertEqual(self.step("--job", job_id, "ship: add the refund page")[0], 0)
        code, _, err = self.cli("job", "close", job_id, env=self.state_env())
        self.assertEqual(code, 1)
        self.assertIn("still has open steps", err)
        self.assertIn("t-0001", err)

    def test_report_names_the_branch_and_commit(self) -> None:
        job_id, worktree = self.open_a_job()
        self.assertEqual(self.step("--job", job_id, "ship: add the refund page")[0], 0)
        self.answer_the_step()
        code, out, _ = self.cli("report", "t-0001", env=self.state_env())
        self.assertEqual(code, 0)
        commit = gitcmd.head_commit(worktree)
        self.assertIn(f"task/refund @ {commit}", out)
        self.assertIn("the refund page is missing", out)

    def test_the_commit_a_report_names_is_read_at_report_time(self) -> None:
        # A commit made during the step is what gets reported, not the one the
        # checkout was on when the step was dispatched.
        job_id, worktree = self.open_a_job()
        self.assertEqual(self.step("--job", job_id, "ship: add the refund page")[0], 0)
        before = self.only_task()["commit"]

        (worktree / "refund.py").write_text("pass\n", encoding="utf-8")
        gitcmd.run_git(worktree, "add", "refund.py")
        gitcmd.run_git(worktree, "commit", "-m", "add the refund page")
        self.answer_the_step()
        self.assertEqual(self.cli("report", "t-0001", env=self.state_env())[0], 0)

        after = self.only_task()["commit"]
        self.assertNotEqual(before, after)
        self.assertEqual(after, gitcmd.head_commit(worktree))

    # -- copies you can tell apart, and handing one over --------------------

    def two_agents(self) -> tuple[str, str]:
        """Two agents in one repo, each with a copy of its own."""
        self.seed_mux([])
        self.cli(
            "ensure",
            "myrepo",
            "--role",
            "maker",
            "--name",
            "myrepo-maker",
            env=self.state_env(),
        )
        code, _, err = self.cli(
            "ensure",
            "myrepo",
            "--role",
            "verifier",
            "--name",
            "myrepo-verifier",
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        return (
            str(self.repo_path / ".worktrees" / "myrepo-maker"),
            str(self.repo_path / ".worktrees" / "myrepo-verifier"),
        )

    def test_agents_shows_what_each_copy_holds(self) -> None:
        self.two_agents()
        code, out, _ = self.cli("agents", env=self.state_env())
        self.assertEqual(code, 0)
        self.assertIn("COPY", out)
        self.assertIn("no branch", out, "a free space has no branch name on it")
        self.assertIn(gitcmd.head_commit(self.repo_path) or "", out)

        payload = json.loads(self.cli("agents", "--json", env=self.state_env())[1])
        by_name = {entry["name"]: entry for entry in payload["agents"]}
        maker = by_name["myrepo-maker"]["copy"]
        self.assertIsNone(maker["branch"], "free means free: no branch")
        self.assertTrue(maker["detached"])
        self.assertTrue(maker["saved"])
        self.assertFalse(maker["main_checkout"])
        self.assertEqual(maker["commit"], gitcmd.head_commit(self.repo_path))

    def test_agents_flags_an_agent_in_the_main_checkout(self) -> None:
        self.seed_mux([{"name": "maker", "cwd": str(self.repo_path)}])
        code, out, _ = self.cli("agents", env=self.state_env())
        self.assertEqual(code, 0)
        self.assertIn("the main checkout", out)

    def test_agents_flags_unsaved_work(self) -> None:
        maker_folder, _ = self.two_agents()
        (Path(maker_folder) / "not-saved-yet.txt").write_text("x", encoding="utf-8")
        _, out, _ = self.cli("agents", env=self.state_env())
        self.assertIn("unsaved work", out)

    def save_something(self, folder: str | Path, name: str = "refund.py") -> str:
        path = Path(folder) / name
        path.write_text("pass\n", encoding="utf-8")
        gitcmd.run_git(folder, "add", name)
        gitcmd.run_git(folder, "commit", "-m", f"add {name}")
        return gitcmd.head_commit(folder, short=False) or ""

    def save_as_crew(self, folder: str | Path, name: str = "refund.py") -> str:
        """A save made the way a crew pane makes one: identity in the env."""
        identity = gitcmd.Identity("clowder-bot", "94532220+lacattano@users.noreply.github.com")
        path = Path(folder) / name
        path.write_text("pass\n", encoding="utf-8")
        gitcmd.run_git(folder, "add", name)
        gitcmd.run_git(folder, "commit", "-m", f"add {name}", env=identity.env())
        return gitcmd.head_commit(folder, short=False) or ""

    def test_handover_puts_the_saved_code_in_the_reviewers_copy(self) -> None:
        job_id, maker_folder = self.open_a_job()
        commit = self.save_something(maker_folder)

        code, out, err = self.cli(
            "job",
            "handover",
            job_id,
            "--to",
            "verifier",
            "--name",
            "myrepo-verifier",
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        self.assertIn(f"handed over: task/refund @ {commit[:7]}", out)

        verifier_folder = self.repo_path / ".worktrees" / "myrepo-verifier"
        self.assertEqual(gitcmd.commit_of(verifier_folder, "HEAD"), commit)
        self.assertTrue(gitcmd.is_detached(verifier_folder), "no branch name on the copy")
        self.assertTrue((verifier_folder / "refund.py").is_file())
        # The branch is the record and stays; the writer's space is handed back, so
        # the file is on the branch, not in the working tree at the base.
        self.assertTrue(gitcmd.branch_exists(maker_folder, "task/refund"))
        self.assertEqual(gitcmd.commit_of(maker_folder, "task/refund"), commit)
        self.assertIsNone(gitcmd.current_branch(maker_folder))

        job = json.loads(self.cli("job", "list", "--json", env=self.state_env())[1])["jobs"][0]
        self.assertEqual(job["reviewer"], "myrepo-verifier")
        self.assertEqual(job["review_commit"], commit)
        self.assertTrue(job["released_at"])
        self.assertEqual(job["held_ref"], f"refs/clowder/held/{job['id']}")

    def test_handover_releases_the_writers_space_and_keeps_the_job_open(self) -> None:
        job_id, maker_folder = self.open_a_job()
        self.save_something(maker_folder)
        code, _, err = self.cli(
            "job",
            "handover",
            job_id,
            "--to",
            "verifier",
            "--name",
            "myrepo-verifier",
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)

        job = json.loads(self.state.read_text(encoding="utf-8"))["jobs"][job_id]
        self.assertEqual(job["status"], "open", "the job stays open for the pass")
        self.assertTrue(job["released_at"], "the writer's space is handed back")
        self.assertIsNone(gitcmd.current_branch(maker_folder), "the space is detached")

        # A second job can start in the space now that it is released.
        code, out, err = self.cli(
            "job",
            "open",
            "myrepo",
            "--label",
            "second",
            "--role",
            "maker",
            "--name",
            "myrepo-maker",
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        self.assertIn("open", out)

    def test_publish_and_merge_work_from_a_released_job(self) -> None:
        job_id, maker_folder = self.open_a_job()
        commit = self.save_something(maker_folder)
        self.assertEqual(
            self.cli(
                "job",
                "handover",
                job_id,
                "--to",
                "verifier",
                "--name",
                "myrepo-verifier",
                env=self.state_env(),
            )[0],
            0,
        )
        self.assertEqual(
            self.cli(
                "job",
                "pass",
                job_id,
                "--shown",
                "the diff",
                "--answer",
                "yes",
                "--by",
                "lacattano",
                env=self.state_env(),
            )[0],
            0,
        )
        with mock.patch.object(cli, "_publish_branch") as publish:
            code, _, err = self.cli("job", "publish", job_id, env=self.state_env())
        self.assertEqual(code, 0, err)
        publish.assert_called_once()

        self.assertEqual(
            self.cli(
                "job",
                "word",
                job_id,
                "--word",
                "merge",
                "--by",
                "lacattano",
                env=self.state_env(),
            )[0],
            0,
        )
        with mock.patch.object(cli, "_merge_pull_request") as merge:
            code, _, err = self.cli("job", "merge", job_id, "--pr", "9", env=self.state_env())
        self.assertEqual(code, 0, err)
        merge.assert_called_once()

        job = json.loads(self.state.read_text(encoding="utf-8"))["jobs"][job_id]
        self.assertEqual(job["commit"], commit, "publish reads the commit, not the base HEAD")

    def test_publish_refuses_a_commit_not_authored_by_the_crew(self) -> None:
        job_id, maker_folder = self.open_a_job()
        self.save_something(maker_folder)  # Test <test@example.com>
        self.handover(job_id)
        self.owner_pass(job_id)
        with mock.patch.object(cli.gitcmd, "push_branch") as pushed:
            code, _, err = self.cli("job", "publish", job_id, env=self.state_env())
        self.assertEqual(code, 2)
        self.assertIn("refusing to publish", err)
        self.assertIn("test@example.com", err)
        pushed.assert_not_called()

    def test_publish_allows_a_commit_authored_by_the_crew(self) -> None:
        job_id, maker_folder = self.open_a_job()
        self.save_as_crew(maker_folder)
        self.handover(job_id)
        self.owner_pass(job_id)
        with mock.patch.object(cli.gitcmd, "push_branch") as pushed:
            code, _, err = self.cli("job", "publish", job_id, env=self.state_env())
        self.assertEqual(code, 0, err)
        pushed.assert_called_once()

    def test_job_open_records_the_base_commit(self) -> None:
        self.add_origin_at_head()
        job_id, _ = self.open_a_job()
        base = gitcmd.head_commit(self.repo_path, short=False)
        job = json.loads(self.state.read_text(encoding="utf-8"))["jobs"][job_id]
        self.assertEqual(job["base_commit"], base)

    def test_publish_refuses_a_base_that_was_rewritten(self) -> None:
        origin = self.add_origin_at_head()
        base = gitcmd.head_commit(self.repo_path, short=False)
        job_id, maker_folder = self.open_a_job()
        self.save_as_crew(maker_folder)
        self.handover(job_id)
        self.owner_pass(job_id)

        # Rewrite origin/main to a fresh history that does not contain `base`.
        other = self.root / "other"
        gitcmd.run_git(self.root, "clone", str(origin), str(other))
        gitcmd.run_git(other, "config", "user.email", "test@example.com")
        gitcmd.run_git(other, "config", "user.name", "Test")
        gitcmd.run_git(other, "checkout", "--orphan", "rewritten")
        gitcmd.run_git(other, "rm", "-rf", ".")
        (other / "new.txt").write_text("rewritten\n", encoding="utf-8")
        gitcmd.run_git(other, "add", "new.txt")
        gitcmd.run_git(other, "commit", "-m", "rewritten root")
        gitcmd.run_git(other, "push", "--force", "origin", "rewritten:main")
        gitcmd.run_git(maker_folder, "fetch", "origin")

        with mock.patch.object(cli.gitcmd, "push_branch") as pushed:
            code, _, err = self.cli("job", "publish", job_id, env=self.state_env())
        self.assertEqual(code, 2)
        self.assertIn("not an ancestor", err)
        self.assertIn(base[:7], err)
        self.assertIn("rewritten", err)
        pushed.assert_not_called()

    def test_publish_allows_a_base_that_is_still_an_ancestor(self) -> None:
        self.add_origin_at_head()
        job_id, maker_folder = self.open_a_job()
        self.save_as_crew(maker_folder)
        self.handover(job_id)
        self.owner_pass(job_id)
        with mock.patch.object(cli.gitcmd, "push_branch") as pushed:
            code, _, err = self.cli("job", "publish", job_id, env=self.state_env())
        self.assertEqual(code, 0, err)
        pushed.assert_called_once()

    def test_the_held_ref_keeps_the_commit_when_the_branch_is_deleted(self) -> None:
        job_id, maker_folder = self.open_a_job()
        commit = self.save_something(maker_folder)
        self.assertEqual(
            self.cli(
                "job",
                "handover",
                job_id,
                "--to",
                "verifier",
                "--name",
                "myrepo-verifier",
                env=self.state_env(),
            )[0],
            0,
        )
        gitcmd.run_git(maker_folder, "branch", "-D", "task/refund")
        found = gitcmd.try_git(maker_folder, "rev-parse", f"refs/clowder/held/{job_id}")
        self.assertEqual((found or "").strip(), commit, "the ref holds the commit")
        self.assertTrue(
            gitcmd.is_reachable(maker_folder, commit), "a held commit is not stranded"
        )

    def test_close_still_works_after_handover(self) -> None:
        job_id, maker_folder = self.open_a_job()
        self.save_something(maker_folder)
        self.assertEqual(
            self.cli(
                "job",
                "handover",
                job_id,
                "--to",
                "verifier",
                "--name",
                "myrepo-verifier",
                env=self.state_env(),
            )[0],
            0,
        )
        code, _, err = self.cli("job", "close", job_id, env=self.state_env())
        self.assertEqual(code, 0, err)
        job = json.loads(self.state.read_text(encoding="utf-8"))["jobs"][job_id]
        self.assertEqual(job["status"], "closed")

    def test_handover_refuses_work_that_is_not_saved(self) -> None:
        job_id, maker_folder = self.open_a_job()
        self.save_something(maker_folder)
        (maker_folder / "half-done.txt").write_text("x", encoding="utf-8")
        code, _, err = self.cli("job", "handover", job_id, env=self.state_env())
        self.assertEqual(code, 2)
        self.assertIn("not saved", err)
        self.assertIn("half-done.txt", err)

    def test_handover_refuses_a_job_with_nothing_saved_yet(self) -> None:
        job_id, _ = self.open_a_job()
        code, _, err = self.cli("job", "handover", job_id, env=self.state_env())
        self.assertEqual(code, 2)
        self.assertIn("nothing has been saved", err)

    def test_handover_refuses_a_reviewer_in_the_main_checkout(self) -> None:
        job_id, maker_folder = self.open_a_job()
        self.save_something(maker_folder)
        state = self.root / "mux-state.json"
        payload = json.loads(state.read_text(encoding="utf-8"))
        payload["agents"].append(
            {
                "name": "verifier",
                "pane_id": "w9:p7",
                "cwd": str(self.repo_path),
                "status": "idle",
                "session_file": "",
            }
        )
        state.write_text(json.dumps(payload), encoding="utf-8")

        code, _, err = self.cli(
            "job", "handover", job_id, "--to", "verifier", env=self.state_env()
        )
        self.assertEqual(code, 2)
        self.assertIn("main checkout", err)
        self.assertEqual(gitcmd.current_branch(self.repo_path), "main", "untouched")

    def test_handover_refuses_to_write_over_the_reviewers_work(self) -> None:
        self.two_agents()
        job_id, maker_folder = self.open_a_job()
        self.save_something(maker_folder)
        verifier_folder = self.repo_path / ".worktrees" / "myrepo-verifier"
        (verifier_folder / "their-own-work.txt").write_text("x", encoding="utf-8")

        code, _, err = self.cli(
            "job",
            "handover",
            job_id,
            "--to",
            "verifier",
            "--name",
            "myrepo-verifier",
            env=self.state_env(),
        )
        self.assertEqual(code, 2)
        self.assertIn("not saved", err)
        self.assertIn("their-own-work.txt", err)

    def test_the_reviewer_can_then_take_a_step_on_that_save(self) -> None:
        job_id, maker_folder = self.open_a_job()
        commit = self.save_something(maker_folder)
        self.assertEqual(
            self.cli(
                "job",
                "handover",
                job_id,
                "--to",
                "verifier",
                "--name",
                "myrepo-verifier",
                env=self.state_env(),
            )[0],
            0,
        )

        code, out, err = self.cli(
            "dispatch",
            "myrepo-verifier",
            "myrepo",
            "scout: run the tests on this",
            "--job",
            job_id,
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        task = self.only_task()
        self.assertEqual(task["agent"], "myrepo-verifier")
        self.assertEqual(task["branch"], "task/refund")
        self.assertEqual(task["commit"], commit[:7], "the save it was told to check")

    def test_a_step_sent_to_a_copy_holding_the_wrong_code_is_refused(self) -> None:
        self.two_agents()
        job_id, maker_folder = self.open_a_job()
        self.save_something(maker_folder)
        self.assertEqual(
            self.cli(
                "job",
                "handover",
                job_id,
                "--to",
                "verifier",
                "--name",
                "myrepo-verifier",
                env=self.state_env(),
            )[0],
            0,
        )
        # The reviewer's copy is moved off the save it was given, so a brief would
        # produce a report about code the job never had.
        verifier_folder = self.repo_path / ".worktrees" / "myrepo-verifier"
        gitcmd.switch_new_branch(verifier_folder, "scratch", "main")

        code, _, err = self.cli(
            "dispatch",
            "myrepo-verifier",
            "myrepo",
            "scout: check it",
            "--job",
            job_id,
            env=self.state_env(),
        )
        self.assertEqual(code, 2)
        self.assertIn("which holds neither", err)
        self.assertIn("job handover", err)

    def test_board_writes_a_page(self) -> None:
        self.dispatch("maker", "myrepo", "ship: add the refund page")
        code, out, err = self.cli("board", env=self.fake_env())
        self.assertEqual(code, 0, err)
        target = Path(out.strip())
        self.assertTrue(target.is_file(), target)
        html = target.read_text(encoding="utf-8")
        self.assertIn("clowder board", html)
        self.assertIn("t-0001", html)
        self.assertIn("add the refund page", html)
        self.assertIn("maker", html)
        self.assertIn("the main checkout", html, "the space is marked as the user's own")

    def test_board_json_says_what_is_on_it(self) -> None:
        self.dispatch("maker", "myrepo", "ship: add the refund page")
        code, out, _ = self.cli("board", "--json", env=self.fake_env())
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(Path(payload["board"]).is_file())
        self.assertEqual(payload["tasks"], 1)
        self.assertEqual(payload["open"], 1)
        self.assertEqual(payload["agents"], 1)
        self.assertTrue(payload["live_ok"])

    def test_board_can_be_written_where_you_ask(self) -> None:
        target = self.root / "pages" / "b.html"
        code, out, _ = self.cli("board", "--out", str(target), env=self.fake_env())
        self.assertEqual(code, 0)
        self.assertEqual(Path(out.strip()), target)
        self.assertTrue(target.is_file())

    def test_the_board_still_works_with_no_live_agent_list(self) -> None:
        self.dispatch("maker", "myrepo", "ship: add the refund page")
        code, out, _ = self.cli("board", env=self.fake_env(CLOWDER_FAKE_LIST_FAIL="1"))
        self.assertEqual(code, 0, "state alone is still worth showing")
        html = Path(out.strip()).read_text(encoding="utf-8")
        self.assertIn("could not be read", html)
        self.assertIn("t-0001", html)

    def test_board_on_an_empty_store(self) -> None:
        code, out, _ = self.cli("board", env=self.fake_env())
        self.assertEqual(code, 0)
        html = Path(out.strip()).read_text(encoding="utf-8")
        self.assertIn("Nothing is recorded as waiting on you.", html)

    def test_an_owner_item_shows_and_clears(self) -> None:
        self.dispatch("maker", "myrepo", "ship: add the refund page")
        code, out, err = self.cli(
            "owner",
            "t-0001",
            "--item",
            "the team-page design is ready to read, in this chat",
            env=self.fake_env(),
        )
        self.assertEqual(code, 0, err)
        self.assertIn("waiting on the owner", out)
        html = (self.root / "board.html").read_text(encoding="utf-8")
        self.assertIn("the team-page design is ready to read", html)
        self.assertIn("your move", html)
        self.assertNotIn("Nothing is recorded as waiting on you.", html)

        code, out, err = self.cli("owner", "t-0001", "--clear", env=self.fake_env())
        self.assertEqual(code, 0, err)
        html = (self.root / "board.html").read_text(encoding="utf-8")
        self.assertNotIn("the team-page design is ready to read", html)
        self.assertIn("Nothing is recorded as waiting on you.", html)

    def test_owner_items_are_numbered_on_the_board(self) -> None:
        self.dispatch("maker", "myrepo", "ship: first thing")
        self.cli("owner", "t-0001", "--item", "choose A or B", env=self.fake_env())
        self.dispatch("maker", "myrepo", "ship: second thing")
        self.cli("owner", "t-0002", "--item", "choose C or D", env=self.fake_env())
        html = (self.root / "board.html").read_text(encoding="utf-8")
        self.assertIn("<b>1.</b> choose A or B", html)
        self.assertIn("<b>2.</b> choose C or D", html)

    def test_recording_the_answer_clears_the_owner_item(self) -> None:
        self.dispatch("maker", "myrepo", "ship: add the refund page")
        self.cli(
            "owner",
            "t-0001",
            "--item",
            "choose A or B, it changes X and costs Y",
            env=self.fake_env(),
        )
        self.assertIn("choose A or B", (self.root / "board.html").read_text(encoding="utf-8"))
        code, _, err = self.cli("report", "t-0001", "--decide", "A", env=self.fake_env())
        self.assertEqual(code, 0, err)
        task = self.only_task()
        self.assertIsNone(task["owner_item"])
        self.assertTrue(task["decision_answer"])
        html = (self.root / "board.html").read_text(encoding="utf-8")
        self.assertNotIn("choose A or B", html, "the answered item leaves his section")

    def make_rebased_commits(self) -> tuple[str, str]:
        """One commit a rebase replaced, and one that is genuinely lost."""
        repo = self.repo_path
        gitcmd.switch_new_branch(repo, "task/x", "main")
        (repo / "b.txt").write_text("b\n", encoding="utf-8")
        gitcmd.run_git(repo, "add", "b.txt")
        gitcmd.run_git(repo, "commit", "-m", "add b")
        (repo / "c.txt").write_text("c\n", encoding="utf-8")
        gitcmd.run_git(repo, "add", "c.txt")
        gitcmd.run_git(repo, "commit", "-m", "add c")
        old = gitcmd.head_commit(repo, short=False) or ""
        (repo / "lost.txt").write_text("lost\n", encoding="utf-8")
        gitcmd.run_git(repo, "add", "lost.txt")
        gitcmd.run_git(repo, "commit", "-m", "lost work")
        lost = gitcmd.head_commit(repo, short=False) or ""
        gitcmd.run_git(repo, "reset", "--hard", "HEAD~1")
        gitcmd.switch_branch(repo, "main")
        (repo / "d.txt").write_text("d\n", encoding="utf-8")
        gitcmd.run_git(repo, "add", "d.txt")
        gitcmd.run_git(repo, "commit", "-m", "base two")
        gitcmd.switch_branch(repo, "task/x")
        gitcmd.run_git(repo, "rebase", "main")
        gitcmd.switch_branch(repo, "main")
        return old, lost

    def write_pinned_task(self, task_id: str, commit: str, worktree: str | None = None) -> None:
        store = StateStore(self.state)
        store.add(
            Task(
                id=task_id,
                question="is this commit lost?",
                brief="ship: x",
                shape="ship",
                agent="maker",
                repo="myrepo",
                repo_path=str(self.repo_path),
                worktree=str(worktree or self.repo_path),
                commit=commit,
            )
        )
        store.save()

    def test_the_board_does_not_flag_a_rebased_away_commit(self) -> None:
        old, lost = self.make_rebased_commits()
        self.write_pinned_task("t-0001", old)
        self.write_pinned_task("t-0002", lost)
        code, _, err = self.cli("board", env=self.fake_env())
        self.assertEqual(code, 0, err)
        html = (self.root / "board.html").read_text(encoding="utf-8")
        owner = html.split("Waiting on you", 1)[1].split("Waiting for a worker")[0]
        self.assertIn("t-0002", owner, "a genuinely lost commit is still at risk")
        self.assertNotIn("t-0001", owner, "a rebased-away commit is not at risk")

    def test_report_does_not_flag_a_rebased_away_commit(self) -> None:
        old, lost = self.make_rebased_commits()
        old_wt = self.repo_path / ".worktrees" / "old"
        lost_wt = self.repo_path / ".worktrees" / "lost"
        gitcmd.add_worktree_free(self.repo_path, old_wt, old)
        gitcmd.add_worktree_free(self.repo_path, lost_wt, lost)
        self.write_pinned_task("t-0001", old, worktree=str(old_wt))
        self.write_pinned_task("t-0002", lost, worktree=str(lost_wt))

        code, out, err = self.cli("report", "t-0001", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertNotIn("on no branch", out, "the superseded commit is not lost")

        code, out, err = self.cli("report", "t-0002", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("on no branch", out, "the lost commit is still flagged")

    def test_dispatch_refreshes_the_board_where_the_state_lives(self) -> None:
        # The page is a byproduct of the command, not a step a human remembers.
        self.dispatch("maker", "myrepo", "ship: add the refund page")
        page = self.root / "board.html"
        self.assertTrue(page.is_file(), "dispatch wrote the page next to the state file")
        html = page.read_text(encoding="utf-8")
        self.assertIn("t-0001", html)
        self.assertIn("add the refund page", html)
        self.assertIn("Waiting on you", html, "the page keeps its first section")

    def test_a_board_that_cannot_be_written_does_not_fail_the_command(self) -> None:
        with mock.patch.object(cli, "write_board", side_effect=OSError("disk full")):
            code, out, err = self.cli(
                "dispatch",
                "maker",
                "myrepo",
                "ship: add the refund page",
                env=self.fake_env(),
            )
        self.assertEqual(code, 0, "the page is a byproduct, not the command")
        self.assertIn("t-0001 sent to maker", out)
        self.assertIn("could not refresh the board", err)
        self.assertIn("disk full", err)

    def test_only_state_changing_commands_refresh_the_board(self) -> None:
        parser = cli.build_parser()

        def refreshes(*argv: str) -> bool:
            return bool(getattr(parser.parse_args(list(argv)), "refreshes_board", False))

        self.assertTrue(refreshes("dispatch", "maker", "myrepo", "ship: x"))
        self.assertTrue(refreshes("report", "t-0001"))
        self.assertTrue(refreshes("ensure", "myrepo"))
        self.assertTrue(refreshes("job", "open", "myrepo", "--label", "x"))
        self.assertTrue(refreshes("job", "close", "j-0001"))
        self.assertTrue(refreshes("job", "handover", "j-0001"))
        self.assertTrue(
            refreshes("queue", "add", "myrepo", "ship: x", "--agent", "maker", "--why", "w")
        )
        self.assertTrue(refreshes("queue", "send", "q-0001"))
        self.assertFalse(refreshes("tasks"))
        self.assertFalse(refreshes("agents"))
        self.assertFalse(refreshes("board"))
        self.assertFalse(refreshes("job", "list"))
        self.assertFalse(refreshes("queue", "list"))
        self.assertFalse(refreshes("config"))

    # -- queue -------------------------------------------------------------

    def queue_add(self, *extra: str, env: dict[str, object] | None = None):
        return self.cli(
            "queue",
            "add",
            "myrepo",
            "ship: add the refund page",
            "--why",
            "the space holds an open job",
            *extra,
            env=env or self.fake_env(),
        )

    def test_queue_add_records_a_decided_item(self) -> None:
        code, out, err = self.queue_add("--agent", "maker")
        self.assertEqual(code, 0, err)
        self.assertIn("q-0001 queued for maker", out)

        item = json.loads(self.state.read_text(encoding="utf-8"))["queued"]["q-0001"]
        self.assertEqual(item["brief"], "ship: add the refund page")
        self.assertEqual(item["repo"], "myrepo")
        self.assertEqual(item["agent"], "maker")
        self.assertEqual(item["role"], None)
        self.assertEqual(item["why"], "the space holds an open job")
        self.assertIsNotNone(item["created_at"])

    def test_queue_add_needs_exactly_one_target_and_a_why(self) -> None:
        code, _, err = self.queue_add()
        self.assertNotEqual(code, 0)
        self.assertIn("exactly one agent or role", err)

        code, _, err = self.queue_add("--agent", "maker", "--role", "maker")
        self.assertNotEqual(code, 0)
        self.assertIn("exactly one agent or role", err)

        code, _, err = self.cli(
            "queue",
            "add",
            "myrepo",
            "ship: add the refund page",
            "--agent",
            "maker",
            "--why",
            "   ",
            env=self.fake_env(),
        )
        self.assertNotEqual(code, 0, "a blank --why is refused")
        self.assertIn("--why", err)

    def test_queue_list_shows_the_item_on_a_later_run(self) -> None:
        self.queue_add("--role", "maker")
        code, out, err = self.cli("queue", "list", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("q-0001", out)
        self.assertIn("myrepo", out)
        self.assertIn("maker", out)
        self.assertIn("open job", out)
        self.assertIn("refund page", out)

    def test_queue_list_on_an_empty_queue(self) -> None:
        code, out, _ = self.cli("queue", "list", env=self.fake_env())
        self.assertEqual(code, 0)
        self.assertIn("the queue is empty", out)

    def test_a_queued_item_survives_a_reload(self) -> None:
        self.queue_add("--agent", "maker")
        # A fresh store, as the next command builds: the item is still there.
        from clowder.state import StateStore

        item = StateStore(self.state).get_queued("q-0001")
        self.assertEqual(item.brief, "ship: add the refund page")
        self.assertEqual(item.why, "the space holds an open job")

    def test_a_queued_item_appears_on_the_board(self) -> None:
        self.queue_add("--agent", "maker")
        html = (self.root / "board.html").read_text(encoding="utf-8")
        owner, worker = html.split("Waiting on you", 1)[1].split("Waiting for a worker")
        self.assertIn("class='count'>0", owner, "the owner's count stays honest")
        self.assertNotIn("q-0001", owner, "a queued item is the front door's, not the owner's")
        self.assertIn("q-0001", worker)
        self.assertIn("the space holds an open job", worker)

    def test_queue_send_dispatches_and_removes_the_item(self) -> None:
        self.queue_add("--agent", "maker")
        code, out, err = self.cli("queue", "send", "q-0001", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("t-0001 sent to maker", out)
        self.assertIn("q-0001 sent and removed from the queue", out)

        payload = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(payload["queued"], {})
        self.assertIn("t-0001", payload["tasks"])

    def test_queue_send_resolves_a_role_to_a_live_agent(self) -> None:
        self.queue_add("--role", "maker")
        code, out, err = self.cli("queue", "send", "q-0001", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("sent to maker", out)

    def test_queue_send_keeps_the_item_when_the_dispatch_fails(self) -> None:
        self.queue_add("--agent", "maker")
        code, _, _ = self.cli(
            "queue", "send", "q-0001", env=self.fake_env(CLOWDER_FAKE_PROMPT_FAIL="1")
        )
        self.assertEqual(code, 2)
        payload = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIn("q-0001", payload["queued"], "a failed send stays in the queue")

    def test_queue_send_by_role_with_no_such_agent_refuses(self) -> None:
        self.queue_add("--role", "verifier")
        code, _, err = self.cli("queue", "send", "q-0001", env=self.fake_env())
        self.assertEqual(code, 2)
        self.assertIn("no agent in myrepo plays 'verifier'", err)
        payload = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIn("q-0001", payload["queued"])

    def test_queue_send_respects_the_cross_repo_guard(self) -> None:
        other = self.workspace / "other"
        other.mkdir()
        self.queue_add("--agent", "maker")
        code, _, err = self.cli(
            "queue", "send", "q-0001", env=self.fake_env(CLOWDER_FAKE_CWD=str(other))
        )
        self.assertEqual(code, 2)
        self.assertIn("which is not", err)
        payload = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIn("q-0001", payload["queued"])

    def test_queue_send_dry_run_sends_nothing_and_keeps_the_item(self) -> None:
        self.queue_add("--agent", "maker")
        code, out, err = self.cli("queue", "send", "q-0001", "--dry-run", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("would run:", out)
        payload = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIn("q-0001", payload["queued"])

    def test_state_can_be_given_after_the_subcommand(self) -> None:
        other = self.root / "elsewhere.json"
        code, out, _ = self.cli("config", "--json", "--state", str(other), env=self.fake_env())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["state_path"], str(other))

    def test_a_state_given_before_the_subcommand_is_not_clobbered(self) -> None:
        # The subparser also carries --state, and its default must not overwrite a
        # value given earlier in the command line.
        code, out, _ = self.cli(
            "--state", str(self.state), "config", "--json", env=self.fake_env()
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["state_path"], str(self.state))

    def test_config_can_be_given_after_the_subcommand(self) -> None:
        code, out, _ = self.cli(
            "config", "--json", "--config", str(self.config), env=self.fake_env()
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["config_source"], str(self.config))

    # -- agents and config -------------------------------------------------

    def test_agents_lists_the_live_roster(self) -> None:
        code, out, _ = self.cli("agents", env=self.fake_env())
        self.assertEqual(code, 0)
        self.assertIn("NAME", out)
        self.assertIn("maker", out)
        self.assertIn("w9:p1", out)

    def test_agents_json(self) -> None:
        _, out, _ = self.cli("agents", "--json", env=self.fake_env())
        agents = json.loads(out)["agents"]
        self.assertEqual(agents[0]["name"], "maker")
        self.assertEqual(agents[0]["session_file"], str(self.session_file))

    def test_config_shows_resolved_paths(self) -> None:
        code, out, _ = self.cli("config", env=self.fake_env())
        self.assertEqual(code, 0)
        self.assertIn(str(self.workspace), out)
        self.assertIn(str(self.state), out)
        self.assertIn(str(self.config), out)
        self.assertIn("state file does not exist yet", out)

    def test_state_leaves_no_temp_fragments(self) -> None:
        self.dispatch("maker", "myrepo", "ship: add the refund page")
        self.session_turns()
        self.cli("report", "t-0001", env=self.fake_env())
        siblings = sorted(p.name for p in self.root.iterdir() if p.name.startswith("state"))
        # state.json and its lock. A leftover writer temp would be named
        # state.json.<pid>.<thread>.<token>.tmp and would fail this check.
        self.assertEqual(siblings, ["state.json", "state.json.lock"])

    def test_env_state_beats_the_config(self) -> None:
        other = self.root / "other-state.json"
        code, _, _ = self.cli(
            "dispatch",
            "maker",
            "myrepo",
            "ship: add the refund page",
            env=self.fake_env(CLOWDER_STATE=str(other)),
        )
        self.assertEqual(code, 0)
        self.assertTrue(other.exists())
        self.assertFalse(self.state.exists())

    # -- agent reset -------------------------------------------------------

    def seed_agent(self, name: str = "myrepo-maker") -> Path:
        """A stateful fake with one agent that has a real session file."""
        sessions = self.root / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        session = sessions / f"{name}.jsonl"
        session.write_text("", encoding="utf-8")
        self.seed_mux(
            [{"name": name, "cwd": str(self.repo_path), "session_file": str(session)}]
        )
        return session

    def test_agent_reset_changes_the_session_and_names_it(self) -> None:
        self.seed_agent()
        code, out, err = self.cli(
            "agent", "reset", "myrepo-maker", "--timeout", "2", env=self.state_env()
        )
        self.assertEqual(code, 0, err)
        self.assertIn("reset: new session", out)
        self.assertIn("myrepo-maker-reset", out)

    def test_agent_reset_refuses_while_a_step_is_unreported(self) -> None:
        job_id, _ = self.open_a_job()
        self.assertEqual(self.step("--job", job_id, "ship: add the refund page")[0], 0)
        code, _, err = self.cli(
            "agent", "reset", "myrepo-maker", "--timeout", "2", env=self.state_env()
        )
        self.assertEqual(code, 1)
        self.assertIn("unreported step", err)
        self.assertIn("t-0001", err)

    def test_agent_reset_fails_when_the_session_does_not_change(self) -> None:
        self.seed_agent()
        code, _, err = self.cli(
            "agent",
            "reset",
            "myrepo-maker",
            "--timeout",
            "0.2",
            env=self.state_env(CLOWDER_FAKE_RESET_FAIL="1"),
        )
        self.assertEqual(code, 1)
        self.assertIn("did not change", err)

    def test_agent_reset_refuses_an_unknown_agent(self) -> None:
        self.seed_mux([])
        code, _, err = self.cli("agent", "reset", "ghost", env=self.state_env())
        self.assertEqual(code, 2)
        self.assertIn("no live agent named", err)

    # -- job pin -----------------------------------------------------------

    def test_job_pin_puts_the_reviewed_commit_in_the_reviewers_copy(self) -> None:
        job_id, maker_folder = self.open_a_job()
        commit = self.save_something(maker_folder)
        self.assertEqual(
            self.cli(
                "job",
                "handover",
                job_id,
                "--to",
                "verifier",
                "--name",
                "myrepo-verifier",
                env=self.state_env(),
            )[0],
            0,
        )
        code, out, err = self.cli(
            "job",
            "pin",
            job_id,
            "--to",
            "verifier",
            "--name",
            "myrepo-verifier",
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        self.assertIn(f"pinned: task/refund @ {commit[:7]}", out)
        verifier_folder = self.repo_path / ".worktrees" / "myrepo-verifier"
        self.assertEqual(gitcmd.commit_of(verifier_folder, "HEAD"), commit)
        self.assertTrue(gitcmd.is_detached(verifier_folder))

    def test_job_pin_can_name_a_commit_after_the_writer_moved_on(self) -> None:
        job_id, maker_folder = self.open_a_job()
        commit = self.save_something(maker_folder)
        # The writer's space moves on: it no longer holds the job branch, so
        # `handover` could not read the commit from it.
        gitcmd.detach_at(maker_folder, "main")
        code, out, err = self.cli(
            "job",
            "pin",
            job_id,
            "--commit",
            commit,
            "--to",
            "verifier",
            "--name",
            "myrepo-verifier",
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        self.assertIn(commit[:7], out)
        job = json.loads(self.state.read_text(encoding="utf-8"))["jobs"][job_id]
        self.assertEqual(job["review_commit"], commit)
        self.assertEqual(job["held_ref"], f"refs/clowder/held/{job_id}")
        self.assertTrue(gitcmd.is_reachable(maker_folder, commit), "the pinned save is held")

    # -- checkouts and a stale base ----------------------------------------

    def add_origin(self) -> None:
        """Give the repo an origin that is one commit ahead of its local main."""
        origin = self.root / "origin.git"
        gitcmd.run_git(self.root, "init", "--bare", "-b", "main", str(origin))
        gitcmd.run_git(self.repo_path, "remote", "add", "origin", str(origin))
        gitcmd.run_git(self.repo_path, "push", "-u", "origin", "main")
        other = self.root / "other"
        gitcmd.run_git(self.root, "clone", str(origin), str(other))
        gitcmd.run_git(other, "config", "user.email", "test@example.com")
        gitcmd.run_git(other, "config", "user.name", "Test")
        (other / "ahead.txt").write_text("x\n", encoding="utf-8")
        gitcmd.run_git(other, "add", "ahead.txt")
        gitcmd.run_git(other, "commit", "-m", "ahead")
        gitcmd.run_git(other, "push", "origin", "main")

    def add_origin_at_head(self) -> Path:
        """An origin whose main equals local main, with no extra commit."""
        origin = self.root / "origin.git"
        gitcmd.run_git(self.root, "init", "--bare", "-b", "main", str(origin))
        gitcmd.run_git(self.repo_path, "remote", "add", "origin", str(origin))
        gitcmd.run_git(self.repo_path, "push", "-u", "origin", "main")
        return origin

    def test_job_open_refuses_a_base_that_is_behind_its_remote(self) -> None:
        self.add_origin()
        self.seed_mux([])
        code, _, err = self.cli(
            "job",
            "open",
            "myrepo",
            "--label",
            "refund",
            "--role",
            "maker",
            env=self.state_env(),
        )
        self.assertEqual(code, 2)
        self.assertIn("behind", err)
        self.assertIn("pull --ff-only", err)

    def test_job_open_with_force_ignores_a_stale_base(self) -> None:
        self.add_origin()
        self.seed_mux([])
        code, out, err = self.cli(
            "job",
            "open",
            "myrepo",
            "--label",
            "refund",
            "--role",
            "maker",
            "--force",
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        self.assertIn("open", out)

    def test_checkouts_reports_a_checkout_behind_its_remote(self) -> None:
        self.add_origin()
        code, out, err = self.cli("checkouts", "myrepo", "--fetch", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("behind 1", out)

    def test_checkouts_json_serialises_every_field(self) -> None:
        self.add_origin()
        code, out, err = self.cli(
            "checkouts", "myrepo", "--fetch", "--json", env=self.fake_env()
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["count"], len(payload["checkouts"]))
        self.assertTrue(any(item["behind"] == 1 for item in payload["checkouts"]))

    # -- state repair ------------------------------------------------------

    def write_unknown_field(self, field: str = "surprise") -> tuple[str, str]:
        self.dispatch("maker", "myrepo", "ship: add the refund page")
        payload = json.loads(self.state.read_text(encoding="utf-8"))
        tid = next(iter(payload["tasks"]))
        payload["tasks"][tid][field] = "left by a stale copy"
        self.state.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return tid, field

    def test_state_repair_drops_an_unknown_field_with_backup_and_audit(self) -> None:
        tid, field = self.write_unknown_field()
        backup = self.root / "state.backup.json"
        code, out, err = self.cli(
            "state",
            "repair",
            "--drop-unknown",
            field,
            "--backup",
            str(backup),
            "--why",
            "a stale copy wrote it",
            "--by",
            "topcat",
            env=self.fake_env(),
        )
        self.assertEqual(code, 0, err)
        self.assertIn(f"dropped {field!r}", out)
        self.assertTrue(backup.is_file())
        after = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertNotIn(field, after["tasks"][tid])

        audit_file = self.root / "state.json.audit"
        self.assertTrue(audit_file.is_file())
        line = json.loads(audit_file.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(line["action"], "drop-unknown")
        self.assertEqual(line["field"], field)
        self.assertEqual(line["by"], "topcat")
        self.assertEqual(line["removed"]["task"], 1)
        self.assertEqual(line["backup"], str(backup))

    def test_state_repair_refuses_without_a_backup(self) -> None:
        _, field = self.write_unknown_field()
        code, _, err = self.cli("state", "repair", "--drop-unknown", field, env=self.fake_env())
        self.assertEqual(code, 1)
        self.assertIn("--backup", err)

    def test_state_repair_refuses_an_existing_backup(self) -> None:
        _, field = self.write_unknown_field()
        backup = self.root / "state.backup.json"
        backup.write_text("do not overwrite me", encoding="utf-8")
        code, _, err = self.cli(
            "state",
            "repair",
            "--drop-unknown",
            field,
            "--backup",
            str(backup),
            env=self.fake_env(),
        )
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        self.assertEqual(backup.read_text(encoding="utf-8"), "do not overwrite me")

    # -- abandoning a dead step --------------------------------------------

    def test_step_abandon_refuses_without_a_why(self) -> None:
        job_id, _ = self.open_a_job()
        self.assertEqual(self.step("--job", job_id, "ship: add the refund page")[0], 0)
        code, _, err = self.cli("step", "abandon", "t-0001", env=self.state_env())
        self.assertEqual(code, 1)
        self.assertIn("--why", err)
        self.assertEqual(self.only_task()["status"], "dispatched")

    def test_step_abandon_frees_the_job_and_keeps_no_answer(self) -> None:
        job_id, _ = self.open_a_job()
        self.assertEqual(self.step("--job", job_id, "ship: add the refund page")[0], 0)
        code, out, err = self.cli(
            "step",
            "abandon",
            "t-0001",
            "--why",
            "the pane died",
            "--by",
            "topcat",
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        self.assertIn("t-0001 abandoned: the pane died", out)

        task = self.only_task()
        self.assertEqual(task["status"], "abandoned")
        self.assertEqual(task["abandon_reason"], "the pane died")
        self.assertTrue(task["abandoned_at"])
        self.assertIsNone(task["answer"])

        # The job is free: the next step dispatches without --force.
        code, out, err = self.step("--job", job_id, "ship: now do something else")
        self.assertEqual(code, 0, err)
        self.assertIn("t-0002", out)
        self.assertEqual(len(self.load_state()["tasks"]), 2)

    def test_step_abandon_writes_an_audit_line(self) -> None:
        job_id, _ = self.open_a_job()
        self.step("--job", job_id, "ship: add the refund page")
        self.cli(
            "step",
            "abandon",
            "t-0001",
            "--why",
            "pane died",
            "--by",
            "topcat",
            env=self.state_env(),
        )
        audit_file = self.root / "state.json.audit"
        self.assertTrue(audit_file.is_file())
        line = json.loads(audit_file.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(line["action"], "step-abandon")
        self.assertEqual(line["step"], "t-0001")
        self.assertEqual(line["job"], job_id)
        self.assertEqual(line["reason"], "pane died")
        self.assertEqual(line["by"], "topcat")

    def test_a_reported_step_cannot_be_abandoned(self) -> None:
        job_id, _ = self.open_a_job()
        self.step("--job", job_id, "ship: add the refund page")
        self.answer_the_step()
        self.assertEqual(self.cli("report", "t-0001", env=self.state_env())[0], 0)
        code, _, err = self.cli(
            "step", "abandon", "t-0001", "--why", "too late", env=self.state_env()
        )
        self.assertEqual(code, 1)
        self.assertIn("not open", err)

    def test_tasks_shows_an_abandoned_step_as_abandoned(self) -> None:
        job_id, _ = self.open_a_job()
        self.step("--job", job_id, "ship: add the refund page")
        self.cli("step", "abandon", "t-0001", "--why", "pane died", env=self.state_env())
        code, out, err = self.cli("tasks", env=self.state_env())
        self.assertEqual(code, 0, err)
        self.assertIn("abandoned", out)
        self.assertIn("1 abandoned", out)
        self.assertIn("0 still open", out)

    def test_report_does_not_capture_an_answer_for_an_abandoned_step(self) -> None:
        job_id, _ = self.open_a_job()
        self.step("--job", job_id, "ship: add the refund page")
        self.cli("step", "abandon", "t-0001", "--why", "pane died", env=self.state_env())
        # The pane said a half sentence before it died; it must not become the answer.
        self.answer_the_step()
        code, out, err = self.cli("report", "t-0001", env=self.state_env())
        self.assertEqual(code, 0, err)
        self.assertIn("abandoned", out)
        task = self.only_task()
        self.assertEqual(task["status"], "abandoned")
        self.assertIsNone(task["answer"])


if __name__ == "__main__":
    unittest.main()
