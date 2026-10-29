"""End to end through the command line, with a stand-in multiplexer."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from clowder import audit, box, cli, gitcmd
from clowder import owner as owner_mod
from clowder.board import BoardData, waiting_on_you
from clowder.board import waiting_on_you as board_waiting_on_you
from clowder.config import load_config
from clowder.mux import MuxResult
from clowder.state import ABANDONED, CLOSED, DISPATCHED, OPEN, REPORTED, Job, StateStore, Task
from clowder.timeutil import now_iso, to_iso
from tests.support import (
    clean_env,
    default_usage,
    make_mux_launcher,
    write_config,
    write_fake_state,
    write_session,
)


def reading(
    *,
    available_gib: float = 20.0,
    commit_gib: float = 40.0,
    carveout_gib: float | None = 32.0,
) -> box.BoxReading:
    """A machine reading a test can state, so the gate never asks the real box.

    The numbers are GiB on purpose: the gate compares GiB, and a test that asserts
    on a refusal has to name the same unit the message does.
    """
    return box.BoxReading(
        read_at="today",
        installed_bytes=int(64 * box.GIB),
        total_bytes=int(32 * box.GIB),
        available_bytes=int(available_gib * box.GIB),
        commit_limit_bytes=int(96 * box.GIB),
        commit_headroom_bytes=int(commit_gib * box.GIB),
        carveout_bytes=None if carveout_gib is None else int(carveout_gib * box.GIB),
    )


def _start_error(code: str, message: str) -> MuxResult:
    """A failed start as the multiplexer reports one: a named code and a reason."""
    return MuxResult(
        argv=("mux", "agent", "start"),
        returncode=5,
        stdout=json.dumps({"error": {"code": code, "message": message}}),
        stderr="",
        duration_ms=1,
    )


def _start_refusal(pane_id: str) -> MuxResult:
    """The refusal the multiplexer gives while a pane is still not a shell."""
    return _start_error(
        "pane_not_shell", f"agent target pane {pane_id} is not an available shell"
    )


class _SlowStartMux:
    """A mux stand-in whose start needs a floor of seconds before it lands.

    The real restart passes its per-attempt timeout to the multiplexer, so this
    records what it was given and answers the way herdr does: the not-a-shell
    refusal while the attempt is too short for this pane, success once it is
    long enough. That is how a slow-but-working start (22 to 45 seconds on the
    owner's box) is tested without waiting those seconds for real.
    """

    def __init__(self, needs_s: float) -> None:
        self.needs_s = needs_s
        self.timeouts: list[float] = []

    def start_agent(
        self,
        name: str,
        pane_id: str,
        kind: str = "pi",
        timeout_s: float | None = None,
        agent_args: tuple[str, ...] = (),
    ) -> MuxResult:
        allowed = float(timeout_s or 0.0)
        self.timeouts.append(allowed)
        if allowed < self.needs_s:
            return _start_refusal(pane_id)
        return MuxResult(
            argv=("mux", "agent", "start", "--pane", pane_id),
            returncode=0,
            stdout="{}",
            stderr="",
            duration_ms=1,
        )


class _SpendsItsTimeoutMux:
    """A mux whose start takes as long as it is allowed, then times out.

    A real start still running when its allowance ends is killed by the
    multiplexer call, so it reports a timeout rather than the not-a-shell
    refusal. This stand-in does the same without a real pane to wait for: it
    spends exactly the timeout it is given, so the wall clock shows what the
    restart allowed an attempt to take.
    """

    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def start_agent(
        self,
        name: str,
        pane_id: str,
        kind: str = "pi",
        timeout_s: float | None = None,
        agent_args: tuple[str, ...] = (),
    ) -> MuxResult:
        allowed = float(timeout_s or 0.0)
        self.timeouts.append(allowed)
        time.sleep(allowed)
        return MuxResult(
            argv=("mux", "agent", "start", "--pane", pane_id),
            returncode=None,
            stdout="",
            stderr="",
            duration_ms=int(allowed * 1000),
            timed_out=True,
        )


class _RefuseThenTimeoutMux:
    """A pane that never becomes a shell, until the budget runs out mid-attempt.

    The first attempt is refused at once and costs no time; the retry gets only
    what is left of the budget, and answers the way a killed attempt does: a
    timeout measured in milliseconds. That is the shape that made the old
    message read "did not return within 9ms".
    """

    def __init__(self) -> None:
        self.calls = 0
        self.timeouts: list[float] = []

    def start_agent(
        self,
        name: str,
        pane_id: str,
        kind: str = "pi",
        timeout_s: float | None = None,
        agent_args: tuple[str, ...] = (),
    ) -> MuxResult:
        self.calls += 1
        self.timeouts.append(float(timeout_s or 0.0))
        if self.calls == 1:
            return _start_refusal(pane_id)
        return MuxResult(
            argv=("mux", "agent", "start", "--pane", pane_id),
            returncode=None,
            stdout="",
            stderr="",
            duration_ms=9,
            timed_out=True,
        )


class _ThenFailsMux:
    """Refuses the first attempt, then answers with a different, named failure.

    This is the reviewer's probe: a retry that fails for its own reason. The
    budget did not stop that start, so the reason must be the failure and not the
    budget, whatever the attempt number.
    """

    def __init__(self, failure: MuxResult) -> None:
        self.failure = failure
        self.calls = 0
        self.timeouts: list[float] = []

    def start_agent(
        self,
        name: str,
        pane_id: str,
        kind: str = "pi",
        timeout_s: float | None = None,
        agent_args: tuple[str, ...] = (),
    ) -> MuxResult:
        self.calls += 1
        self.timeouts.append(float(timeout_s or 0.0))
        if self.calls == 1:
            return _start_refusal(pane_id)
        return self.failure


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
            "--worktree",
            str(self.repo_path),
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
            "--worktree",
            str(self.repo_path),
            env=self.fake_env(),
        )
        sent = self.only_task()["mux_argv"][-1]
        self.assertTrue(
            sent.startswith("[clowder job t-0001 | ship | myrepo | from topcat]\n"), sent
        )
        # The tool, not the brief writer, tells the worker to report over the bus.
        self.assertIn("Send the report to topcat over the agent bus", sent)
        self.assertIn("peer list", sent)
        self.assertIn("clowder inbox", sent)
        self.assertIn("crew skill", sent)
        self.assertTrue(sent.endswith("ship: add the refund page"), sent)

    def test_the_marker_reaches_the_process(self) -> None:
        log = self.root / "calls.jsonl"
        self.cli(
            "dispatch",
            "maker",
            "myrepo",
            "ship: add the refund page now",
            "--worktree",
            str(self.repo_path),
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
        self.dispatch(
            "maker", "myrepo", "ship: do the thing", "--worktree", str(self.repo_path)
        )
        self.assertEqual(self.only_task()["sender"], "the front door")

    def test_no_marker_sends_the_bare_brief(self) -> None:
        self.cli(
            "dispatch",
            "maker",
            "myrepo",
            "ship: do the thing",
            "--no-marker",
            "--worktree",
            str(self.repo_path),
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
        self.dispatch(
            "maker", "myrepo", "ship: do the thing", "--worktree", str(self.repo_path)
        )
        self.assertIsNone(self.only_task()["sender"])

    def test_front_door_name_is_used_as_the_sender(self) -> None:
        self.config = write_config(
            self.root / "named.toml",
            workspace={"root": str(self.workspace)},
            mux={"bin": str(self.launcher)},
            state={"path": str(self.state)},
            front_door={"name": "topcat"},
        )
        self.dispatch(
            "maker", "myrepo", "ship: do the thing", "--worktree", str(self.repo_path)
        )
        self.assertEqual(self.only_task()["sender"], "topcat")

    def test_an_unknown_agent_is_refused_before_anything_is_sent(self) -> None:
        code, _, err = self.cli(
            "dispatch",
            "ghost",
            "myrepo",
            "ship: do it",
            "--worktree",
            str(self.repo_path),
            env=self.fake_env(),
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
            "--worktree",
            str(self.repo_path),
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
            "--worktree",
            str(worktree),
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
            "--worktree",
            str(self.repo_path),
            env=self.fake_env(CLOWDER_FAKE_CWD=str(other)),
        )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_dry_run_sends_nothing(self) -> None:
        code, out, _ = self.cli(
            "dispatch",
            "maker",
            "myrepo",
            "scout: do it",
            "--shape",
            "scout",
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
            "--worktree",
            str(self.repo_path),
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
            "--worktree",
            str(self.repo_path),
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
            "dispatch",
            "maker",
            "myrepo",
            "ship: " + "x" * 9000,
            "--worktree",
            str(self.repo_path),
            env=self.fake_env(),
        )
        self.assertEqual(code, 2)
        self.assertIn("--brief-file", err)
        self.assertFalse(self.state.exists())

    def test_a_brief_that_omits_the_shape_is_noted(self) -> None:
        code, _, err = self.cli(
            "dispatch",
            "maker",
            "myrepo",
            "look into the billing page",
            "--worktree",
            str(self.repo_path),
            env=self.fake_env(),
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
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        code, out, _ = self.cli("tasks", env=self.fake_env())
        self.assertEqual(code, 0)
        self.assertIn("t-0001", out)
        self.assertIn("dispatched", out)
        self.assertIn("add the refund page", out)
        self.assertIn("1 task(s), 1 still open", out)
        self.assertIn("Next: read one with: clowder report t-0001", out)

    def test_tasks_open_filter(self) -> None:
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
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
        self.assertIn("Next: check what has reported with: clowder inbox", out)

    def test_tasks_json_fields_keep_only_the_named_ones(self) -> None:
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        code, out, _ = self.cli("tasks", "--json", "--fields", "id,status", env=self.fake_env())
        self.assertEqual(code, 0)
        record = json.loads(out)["tasks"][0]
        self.assertEqual(set(record), {"id", "status"})
        self.assertEqual(record["id"], "t-0001")

    def test_tasks_json_fields_refuse_an_unknown_name(self) -> None:
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        code, _, err = self.cli("tasks", "--json", "--fields", "id,nope", env=self.fake_env())
        self.assertEqual(code, 1)
        self.assertIn("nope", err)

    def test_unknown_task_id_exits_one(self) -> None:
        code, _, err = self.cli("report", "t-4242", env=self.fake_env())
        self.assertEqual(code, 1)
        self.assertIn("t-4242", err)

    # -- report ------------------------------------------------------------

    def session_turns(
        self, count: int = 1, after_dispatch: bool = True, path: Path | None = None
    ) -> None:
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
        write_session(
            path or self.session_file, cwd=str(self.workspace / "myrepo"), turns=turns
        )

    def live_session(self, name: str) -> Path:
        """A second session file: the one the pane is running after a restart."""
        path = self.root / "sessions" / "--x--" / f"{name}.jsonl"
        write_session(path, cwd=str(self.workspace / "myrepo"), turns=[])
        return path

    def write_window_task(self, task_id: str, agent: str, dispatched_at: str) -> None:
        """A task with an explicit dispatch window, sharing the pane's session."""
        store = StateStore(self.state)
        store.add(
            Task(
                id=task_id,
                question=f"question for {task_id}",
                brief="scout: x",
                shape="scout",
                agent=agent,
                repo="myrepo",
                repo_path=str(self.repo_path),
                agent_session=str(self.session_file),
                status=DISPATCHED,
                dispatched_at=dispatched_at,
            )
        )
        store.save()

    def test_report_reads_the_session_file(self) -> None:
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
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

    def test_report_survives_a_console_that_cannot_encode_the_answer(self) -> None:
        # q-0026: the console on this box is cp1252 and an answer can hold any
        # character. The report must still print (the console drops the glyph it
        # cannot show) and the task must still be marked reported. A StringIO
        # capture cannot show this: only a real cp1252 stream raises.
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        write_session(
            self.session_file,
            cwd=str(self.workspace / "myrepo"),
            turns=[
                {
                    "at": int(time.time() * 1000) + 1000,
                    "text": "shipped \U0001f195 and the owner\u2018s page",
                    "usage": default_usage(1000, 200, cost_total=0.002),
                }
            ],
        )
        buf = io.BytesIO()
        stream = io.TextIOWrapper(buf, encoding="cp1252", errors="strict")
        with clean_env(**self.fake_env()), mock.patch.object(sys, "stdout", stream):
            code = cli.main(["--config", str(self.config), "report", "t-0001"])
        stream.flush()
        printed = buf.getvalue().decode("cp1252")

        self.assertEqual(code, 0, "the report must not crash on an unencodable answer")
        self.assertIn("Re: ship: add the refund page", printed)
        self.assertIn("shipped ?", printed)
        self.assertEqual(self.only_task()["status"], "reported")

    def test_report_scopes_the_answer_to_the_task_window(self) -> None:
        # t-0628: one agent, two tasks, one long-lived session file. The later
        # task's answer must not be saved as the earlier task's answer, and its
        # usage must not be counted. The window ends where the next task began.
        write_session(
            self.session_file,
            cwd=str(self.workspace / "myrepo"),
            turns=[
                {
                    "at": 1_790_512_810_000,
                    "text": "first task answer",
                    "usage": default_usage(1000, 100),
                },
                {
                    "at": 1_790_512_900_000,
                    "text": "second task answer",
                    "usage": default_usage(2000, 200),
                },
            ],
        )
        self.write_window_task("t-0001", "maker", "2026-09-27T12:40:00Z")
        self.write_window_task("t-0002", "maker", "2026-09-27T12:41:00Z")

        code, out, err = self.cli("report", "t-0001", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("first task answer", out)
        self.assertNotIn("second task answer", out)
        task = self.load_state()["tasks"]["t-0001"]
        self.assertEqual(task["answer"], "first task answer")
        self.assertEqual(task["usage"]["turns"], 1)
        self.assertEqual(task["usage"]["input_tokens"], 1000)

    def test_the_latest_task_still_records_with_no_upper_bound(self) -> None:
        # The newest task has no next task, so its window is open above and the
        # last text in the pane is its answer.
        write_session(
            self.session_file,
            cwd=str(self.workspace / "myrepo"),
            turns=[
                {
                    "at": 1_790_512_810_000,
                    "text": "first task answer",
                    "usage": default_usage(1000, 100),
                },
                {
                    "at": 1_790_512_900_000,
                    "text": "second task answer",
                    "usage": default_usage(2000, 200),
                },
            ],
        )
        self.write_window_task("t-0001", "maker", "2026-09-27T12:40:00Z")
        self.write_window_task("t-0002", "maker", "2026-09-27T12:41:00Z")

        code, out, err = self.cli("report", "t-0002", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("second task answer", out)
        task = self.load_state()["tasks"]["t-0002"]
        self.assertEqual(task["answer"], "second task answer")
        self.assertEqual(task["usage"]["input_tokens"], 2000)

    def test_a_later_task_to_another_agent_does_not_bound_the_window(self) -> None:
        self.write_window_task("t-0001", "maker", "2026-09-27T12:40:00Z")
        self.write_window_task("t-0002", "verifier", "2026-09-27T12:41:00Z")
        store = StateStore(self.state)
        task = store.get("t-0001")
        since = cli.to_epoch(task.dispatched_at)
        self.assertIsNone(cli._report_until(store, task, since))

    def test_inbox_scopes_each_answer_to_its_task_window(self) -> None:
        # inbox walks every open step and shares report's read, so it must bound
        # each step the same way: t-0001 shows its own answer, not t-0002's.
        write_session(
            self.session_file,
            cwd=str(self.workspace / "myrepo"),
            turns=[
                {
                    "at": 1_790_512_810_000,
                    "text": "first task answer",
                    "usage": default_usage(1000, 100),
                },
                {
                    "at": 1_790_512_900_000,
                    "text": "second task answer",
                    "usage": default_usage(2000, 200),
                },
            ],
        )
        self.write_window_task("t-0001", "maker", "2026-09-27T12:40:00Z")
        self.write_window_task("t-0002", "maker", "2026-09-27T12:41:00Z")

        code, out, err = self.cli("inbox", "--json", env=self.fake_env())
        self.assertEqual(code, 0, err)
        answers = {item["id"]: item["answer"] for item in json.loads(out)["reported"]}
        self.assertEqual(answers["t-0001"], "first task answer")
        self.assertEqual(answers["t-0002"], "second task answer")

    def test_report_does_not_settle_a_working_agent(self) -> None:
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
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
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        code, out, _ = self.cli("report", "t-0001", env=self.fake_env())
        self.assertEqual(code, 0)
        self.assertIn("no answer yet", out)

    def test_report_no_save_leaves_the_record_alone(self) -> None:
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        self.session_turns()
        before = self.state.read_text(encoding="utf-8")
        code, out, _ = self.cli("report", "t-0001", "--no-save", env=self.fake_env())
        self.assertEqual(code, 0)
        self.assertIn("the answer (0)", out)
        self.assertEqual(self.state.read_text(encoding="utf-8"), before)

    def test_report_records_one_open_decision(self) -> None:
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
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
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
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
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        self.session_turns()
        code, out, _ = self.cli("report", "t-0001", "--json", env=self.fake_env())
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["task"]["status"], "reported")
        self.assertEqual(payload["answer"], "the answer (0)")
        self.assertEqual(payload["usage"]["turns"], 1)
        self.assertEqual(payload["agent_status"], "idle")
        self.assertEqual(payload["session_file"], str(self.session_file))

    def test_report_json_fields_keep_only_the_named_ones(self) -> None:
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        self.session_turns()
        code, out, _ = self.cli(
            "report", "t-0001", "--json", "--fields", "id,status", env=self.fake_env()
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(set(payload["task"]), {"id", "status"})
        self.assertEqual(payload["task"]["id"], "t-0001")

    def test_report_cuts_a_long_answer_and_full_shows_it(self) -> None:
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        answer = "\n".join(f"line {index}" for index in range(1, 61))
        write_session(
            self.session_file,
            cwd=str(self.workspace / "myrepo"),
            turns=[
                {
                    "at": int(time.time() * 1000) + 1000,
                    "text": answer,
                    "usage": default_usage(1000, 200),
                }
            ],
        )
        code, out, _ = self.cli("report", "t-0001", env=self.fake_env())
        self.assertEqual(code, 0)
        self.assertIn("line 40", out)
        self.assertNotIn("line 41", out)
        self.assertIn("20 more line(s) hidden", out)
        self.assertIn("--full", out)

        code, out, _ = self.cli("report", "t-0001", "--full", "--no-save", env=self.fake_env())
        self.assertEqual(code, 0)
        self.assertIn("line 60", out)
        self.assertNotIn("more line(s) hidden", out)

    def test_report_suggests_the_next_step(self) -> None:
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        self.session_turns()
        _, out, _ = self.cli("report", "t-0001", env=self.fake_env())
        self.assertIn("Next: see the other steps with: clowder tasks", out)

    def test_report_with_an_open_decision_suggests_answering_it(self) -> None:
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        self.session_turns()
        _, out, _ = self.cli(
            "report", "t-0001", "--open-decision", "14 days or 30?", env=self.fake_env()
        )
        self.assertIn(
            "Next: answer the open decision with: clowder report t-0001 --decide TEXT", out
        )

    def test_report_verbose_shows_the_breakdown(self) -> None:
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        self.session_turns()
        _, out, _ = self.cli("report", "t-0001", "--verbose", env=self.fake_env())
        self.assertIn("turns: 1", out)
        self.assertIn("test-provider", out)
        self.assertIn(str(self.session_file), out)

    def test_report_shows_the_model_and_the_line_keeps_it(self) -> None:
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        self.session_turns()
        code, out, err = self.cli("report", "t-0001", env=self.fake_env())
        self.assertEqual(code, 0, err)
        # The usage line surfaces which model ran the step, so a free or preview
        # model can be compared against the paid ones.
        self.assertIn("test-model", out)

    def test_a_reported_step_records_the_model_it_ran_on(self) -> None:
        # Recorded from the step's own session, so it is measurement not invention.
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        self.session_turns()
        self.assertEqual(self.cli("report", "t-0001", env=self.fake_env())[0], 0)
        task = self.only_task()
        self.assertEqual(task["model"], "test-model")
        self.assertEqual(task["provider"], "test-provider")

    def test_a_ship_dispatch_with_no_place_is_refused(self) -> None:
        # The other half of the queue refusal: a ship's save belongs on a job's
        # branch or in a folder, so a ship must name one. Nothing is sent, and
        # nothing is recorded.
        code, _, err = self.cli(
            "dispatch",
            "maker",
            "myrepo",
            "ship: add the refund page",
            env=self.fake_env(),
        )
        self.assertEqual(code, 1)
        self.assertIn("needs a place", err)
        self.assertIn("--job", err, "the refusal must name the fix")
        self.assertIn("--worktree", err, "and the deliberate alternative")
        self.assertFalse(self.state.exists(), "a refused dispatch records nothing")

    def test_a_ship_dispatch_dispatches_with_a_job(self) -> None:
        job_id, worktree = self.open_a_job()
        code, out, err = self.cli(
            "dispatch",
            "myrepo-maker",
            "myrepo",
            "ship: add the refund page",
            "--job",
            job_id,
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        self.assertIn("sent to myrepo-maker", out)
        task = self.only_task()
        self.assertEqual(task["job"], job_id)
        self.assertEqual(task["branch"], "task/refund")
        self.assertEqual(task["worktree"], str(worktree))

    def test_a_ship_dispatch_dispatches_with_a_worktree(self) -> None:
        folder = self.root / "elsewhere"
        folder.mkdir()
        code, out, err = self.cli(
            "dispatch",
            "maker",
            "myrepo",
            "ship: add the refund page",
            "--worktree",
            str(folder),
            env=self.fake_env(),
        )
        self.assertEqual(code, 0, err)
        self.assertIn("sent to maker", out)
        self.assertEqual(self.only_task()["worktree"], str(folder))

    def test_a_scout_with_no_place_is_still_allowed(self) -> None:
        # A scout changes nothing, so it has no save to place.
        code, out, err = self.cli(
            "dispatch",
            "maker",
            "myrepo",
            "scout: read the terms page",
            "--shape",
            "scout",
            env=self.fake_env(),
        )
        self.assertEqual(code, 0, err)
        self.assertIn("sent to maker", out)
        self.assertEqual(self.only_task()["shape"], "scout")

    def test_force_does_not_bypass_the_no_place_refusal(self) -> None:
        # A deliberate place is `--worktree`, which is a name rather than an
        # override, so --force is not the way past this.
        code, _, err = self.cli(
            "dispatch",
            "maker",
            "myrepo",
            "ship: add the refund page",
            "--force",
            env=self.fake_env(),
        )
        self.assertEqual(code, 1)
        self.assertIn("needs a place", err)

    def test_two_suites_start_together_when_the_box_has_room(self) -> None:
        # The old flag refused the second suite whatever the machine was doing. The
        # class gate compares footprints, so two suites fit when the box can hold
        # both - and the room is measured, not guessed.
        self.seed_agents("myrepo-maker", "myrepo-verifier")
        code, _, err = self.cli(
            "dispatch",
            "myrepo-maker",
            "myrepo",
            "scout: build the image",
            "--shape",
            "scout",
            "--heavy",
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        with mock.patch.object(box, "read", return_value=reading(available_gib=20.0)):
            code, out, err = self.cli(
                "dispatch",
                "myrepo-verifier",
                "myrepo",
                "scout: run the suite",
                "--shape",
                "scout",
                "--heavy",
                env=self.state_env(),
            )
        self.assertEqual(code, 0, err)
        self.assertIn("sent to myrepo-verifier", out)
        self.assertEqual(len(self.load_state()["tasks"]), 2)

    def test_a_suite_and_a_model_leg_are_refused_with_the_numbers(self) -> None:
        # The case the class table exists for. A suite is running, the box has 8 GiB
        # free, and a browser leg needs its own share plus the 4 GiB floor - so the
        # refusal names the free memory, the held figure and the floor, not a percent.
        self.seed_agents("myrepo-maker", "myrepo-verifier")
        code, _, err = self.cli(
            "dispatch",
            "myrepo-maker",
            "myrepo",
            "scout: run the suite",
            "--shape",
            "scout",
            "--heavy",
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        with mock.patch.object(box, "read", return_value=reading(available_gib=8.0)):
            code, _, err = self.cli(
                "dispatch",
                "myrepo-verifier",
                "myrepo",
                "scout: run the 27B over the stories",
                "--shape",
                "scout",
                "--cost",
                "browser",
                env=self.state_env(),
            )
        self.assertEqual(code, 2)
        self.assertIn("browser", err)
        self.assertIn("8.0 GiB free", err)
        self.assertIn("3.0 GiB held", err)
        self.assertIn("6.0 GiB this browser step needs", err)
        self.assertIn("4.0 GiB must stay free", err)
        self.assertIn("clowder box", err)
        self.assertEqual(len(self.load_state()["tasks"]), 1, "nothing new was sent")

    def test_a_model_leg_takes_the_box_alone(self) -> None:
        # The one state gate that survives, and the reason it does: the GPU counter is
        # too slow for a gate, so two model legs cannot be measured against each other.
        self.seed_agents("myrepo-maker", "myrepo-verifier")
        with mock.patch.object(box, "read", return_value=reading(available_gib=30.0)):
            code, _, err = self.cli(
                "dispatch",
                "myrepo-maker",
                "myrepo",
                "scout: run the 27B",
                "--shape",
                "scout",
                "--cost",
                "model",
                env=self.state_env(),
            )
            self.assertEqual(code, 0, err)
            # A step that states a cost waits. An unmarked one does not: a step with
            # no stated cost is never blocked by the box.
            code, _, err = self.cli(
                "dispatch",
                "myrepo-verifier",
                "myrepo",
                "scout: read the history",
                "--shape",
                "scout",
                "--cost",
                "read",
                env=self.state_env(),
            )
        self.assertEqual(code, 2)
        self.assertIn("takes the box alone", err)
        self.assertIn("t-0001", err)
        self.assertEqual(len(self.load_state()["tasks"]), 1, "nothing new was sent")

    def test_a_reported_or_abandoned_step_does_not_hold_a_class(self) -> None:
        # The gate is state plus one reading, so it clears itself the moment the
        # measured step stops.
        store = StateStore(self.state)
        for task_id, status in (("t-0001", REPORTED), ("t-0002", ABANDONED)):
            store.add(
                Task(
                    id=task_id,
                    question="q",
                    brief="b",
                    shape="scout",
                    agent="maker",
                    repo="myrepo",
                    repo_path=str(self.repo_path),
                    cost="suite",
                    heavy=True,
                    status=status,
                )
            )
        store.save()
        config = load_config(self.config)
        with mock.patch.object(box, "read", return_value=reading(available_gib=20.0)):
            refusal = cli._headroom_refusal(config, "suite", store, "maker")
        self.assertIsNone(refusal)

    def test_a_light_dispatch_is_not_blocked_by_a_heavy_step(self) -> None:
        self.seed_agents("myrepo-maker", "myrepo-verifier")
        code, _, err = self.cli(
            "dispatch",
            "myrepo-maker",
            "myrepo",
            "scout: build the image",
            "--shape",
            "scout",
            "--heavy",
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        code, out, err = self.cli(
            "dispatch",
            "myrepo-verifier",
            "myrepo",
            "scout: read the docs",
            "--shape",
            "scout",
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        self.assertIn("sent to myrepo-verifier", out)

    def test_an_unmarked_step_is_never_refused_by_the_box(self) -> None:
        # `--cost none` is the default, and it is what a step that only edits files
        # states. It must never be refused for memory: a step with no stated cost
        # costs nothing to the box.
        self.seed_agents("myrepo-maker", "myrepo-verifier")
        with mock.patch.object(box, "read", return_value=reading(available_gib=0.1)):
            code, out, err = self.cli(
                "dispatch",
                "myrepo-maker",
                "myrepo",
                "ship: add the refund page",
                "--worktree",
                str(self.repo_path),
                env=self.state_env(),
            )
        self.assertEqual(code, 0, err)
        self.assertIn("sent to myrepo-maker", out)

    def test_a_heavy_step_is_refused_when_the_box_cannot_cover_it(self) -> None:
        # The reading is patched so the test does not depend on the machine it runs on.
        with mock.patch.object(box, "read", return_value=reading(available_gib=5.0)):
            code, _, err = self.cli(
                "dispatch",
                "maker",
                "myrepo",
                "scout: run the suite",
                "--shape",
                "scout",
                "--heavy",
                env=self.fake_env(),
            )
        self.assertEqual(code, 2)
        self.assertIn("5.0 GiB free", err)
        self.assertIn("3.0 GiB this suite step needs", err)
        self.assertIn("4.0 GiB must stay free", err)

    def test_a_class_step_fails_open_when_the_box_cannot_be_read(self) -> None:
        # A broken probe must never block work, so the gate says it could not measure
        # and lets the step go.
        empty = box.BoxReading(read_at="today")
        with mock.patch.object(box, "read", return_value=empty):
            code, out, err = self.cli(
                "dispatch",
                "maker",
                "myrepo",
                "scout: run the suite",
                "--shape",
                "scout",
                "--heavy",
                env=self.fake_env(),
            )
        self.assertEqual(code, 0, err)
        self.assertIn("sent to maker", out)
        self.assertIn("was not measured", err)

    def test_force_sends_and_records_that_it_did(self) -> None:
        with mock.patch.object(box, "read", return_value=reading(available_gib=1.0)):
            code, out, err = self.cli(
                "dispatch",
                "maker",
                "myrepo",
                "scout: run the suite",
                "--shape",
                "scout",
                "--heavy",
                "--force",
                env=self.fake_env(),
            )
        self.assertEqual(code, 0, err)
        self.assertIn("sent to maker", out)
        self.assertIn("--force sent it anyway", err)
        forced = [
            line
            for line in audit.read_audit(self.state)
            if line.get("action") == "dispatch-forced"
        ]
        self.assertEqual(len(forced), 1)
        self.assertEqual(forced[0]["cost"], "suite")
        self.assertTrue(forced[0]["forced"])
        self.assertIn("must stay free", str(forced[0]["refused"]))

    def test_heavy_is_the_old_name_for_a_suite(self) -> None:
        code, _, err = self.cli(
            "dispatch",
            "maker",
            "myrepo",
            "scout: run the suite",
            "--shape",
            "scout",
            "--worktree",
            str(self.repo_path),
            "--heavy",
            env=self.fake_env(),
        )
        self.assertEqual(code, 0, err)
        task = self.only_task()
        self.assertEqual(task["cost"], "suite")
        self.assertTrue(task["heavy"], "the old marker still reads as a suite")

    def test_two_cost_words_at_once_are_refused(self) -> None:
        code, _, err = self.cli(
            "dispatch",
            "maker",
            "myrepo",
            "scout: run the suite",
            "--shape",
            "scout",
            "--worktree",
            str(self.repo_path),
            "--heavy",
            "--cost",
            "browser",
            env=self.fake_env(),
        )
        self.assertEqual(code, 1)
        self.assertIn("--heavy is the old name for --cost suite", err)

    def test_box_prints_the_three_pools_with_their_sources(self) -> None:
        with mock.patch.object(box, "read", return_value=reading(available_gib=20.0)):
            code, out, err = self.cli("box", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("memory: 64.0 GiB installed", out)
        self.assertIn("32.0 GiB is set aside for the GPU", out)
        self.assertIn("GPU side:", out)
        self.assertIn("CPU side: 32.0 GiB visible, 20.0 GiB free", out)
        self.assertIn("GlobalMemoryStatusEx ullAvailPhys", out)
        self.assertIn("gate now, a suite step: sends", out)

    def test_box_json_names_every_pool_and_the_gate(self) -> None:
        with mock.patch.object(box, "read", return_value=reading(available_gib=5.0)):
            code, out, err = self.cli("box", "--json", env=self.fake_env())
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["available_gib"], 5.0)
        self.assertEqual(payload["installed_gib"], 64.0)
        self.assertEqual(payload["gpu_carveout_gib"], 32.0)
        self.assertEqual(payload["floors"]["min_free_gib"], 4.0)
        self.assertTrue(payload["suite"]["allowed"] is False)
        self.assertIn("must stay free", " ".join(payload["suite"]["lines"]))

    def test_start_shows_each_step_s_class_and_what_the_box_has_left(self) -> None:
        store = StateStore(self.state)
        store.add(
            Task(
                id="t-0001",
                question="q",
                brief="b",
                shape="scout",
                agent="maker",
                repo="myrepo",
                repo_path=str(self.repo_path),
                cost="suite",
                heavy=True,
            )
        )
        store.save()
        with mock.patch.object(box, "read", return_value=reading(available_gib=20.0)):
            code, out, err = self.cli("start", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("- t-0001 maker [suite]: q", out)
        self.assertIn("box: 20.0 GiB free", out)
        self.assertIn("3.0 GiB held by 1 running step(s)", out)

    def test_a_no_job_send_to_an_agent_holding_open_work_is_refused(self) -> None:
        # A send lands in the pane and the newest message wins, so a second send
        # to an agent with an open no-job task would drop the first turn.
        self.dispatch("maker", "myrepo", "scout: read the terms page", "--shape", "scout")
        code, _, err = self.dispatch(
            "maker", "myrepo", "scout: read the privacy page", "--shape", "scout"
        )
        self.assertEqual(code, 2)
        self.assertIn("already holds open work", err)
        self.assertIn("t-0001", err)
        self.assertIn("no job", err)
        self.assertEqual(len(self.load_state()["tasks"]), 1, "nothing new was sent")

    def test_an_open_step_blocks_a_no_job_send(self) -> None:
        job_id, _ = self.open_a_job()
        self.assertEqual(self.step("--job", job_id, "ship: add the refund page")[0], 0)
        code, _, err = self.cli(
            "dispatch",
            "myrepo-maker",
            "myrepo",
            "scout: look around",
            "--shape",
            "scout",
            env=self.state_env(),
        )
        self.assertEqual(code, 2)
        self.assertIn("already holds open work", err)
        self.assertIn("t-0001", err)
        self.assertIn(job_id, err)

    def test_an_open_job_holding_the_agents_space_is_refused(self) -> None:
        # The j-0004 window: the agent's own space holds an open job, and the send
        # does not name it, so the message would preempt the open job's turn.
        job_id, worktree = self.open_a_job()
        code, _, err = self.cli(
            "dispatch",
            "myrepo-maker",
            "myrepo",
            "scout: look around",
            "--shape",
            "scout",
            env=self.state_env(),
        )
        self.assertEqual(code, 2)
        self.assertIn("inside open job", err)
        self.assertIn(job_id, err)
        self.assertIn(str(worktree), err)

    def test_a_closed_job_does_not_block_a_send(self) -> None:
        job_id, _ = self.open_a_job()
        self.assertEqual(self.cli("job", "close", job_id, env=self.state_env())[0], 0)
        code, out, err = self.cli(
            "dispatch",
            "myrepo-maker",
            "myrepo",
            "scout: look around",
            "--shape",
            "scout",
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        self.assertIn("sent to myrepo-maker", out)

    def test_force_bypasses_the_open_work_refusal(self) -> None:
        self.dispatch("maker", "myrepo", "scout: read the terms page", "--shape", "scout")
        code, out, err = self.cli(
            "dispatch",
            "maker",
            "myrepo",
            "scout: read the privacy page",
            "--shape",
            "scout",
            "--force",
            env=self.fake_env(),
        )
        self.assertEqual(code, 0, err)
        self.assertIn("sent to maker", out)
        self.assertEqual(len(self.load_state()["tasks"]), 2, "force sent the second step")

    def test_report_survives_a_missing_session_file(self) -> None:
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        self.session_file.unlink()
        code, out, _ = self.cli("report", "t-0001", env=self.fake_env())
        self.assertEqual(code, 0)
        self.assertIn("no answer yet", out)

    def test_report_keeps_the_recorded_session_after_a_restart(self) -> None:
        # A restart gives the pane a fresh session. The answer to the step lives
        # in the session it was dispatched into, so that is the one to read.
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        self.session_turns()
        restarted = self.live_session("restart")
        code, out, _ = self.cli(
            "report", "t-0001", env=self.fake_env(CLOWDER_FAKE_SESSION=str(restarted))
        )
        self.assertEqual(code, 0)
        self.assertIn("the answer (0)", out)
        self.assertEqual(self.only_task()["agent_session"], str(self.session_file))

    def test_report_uses_the_live_session_when_the_recorded_one_is_gone(self) -> None:
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        self.session_file.unlink()
        live = self.live_session("live")
        self.session_turns(path=live)
        code, out, _ = self.cli(
            "report", "t-0001", env=self.fake_env(CLOWDER_FAKE_SESSION=str(live))
        )
        self.assertEqual(code, 0)
        self.assertIn("the answer (0)", out)
        self.assertEqual(self.only_task()["agent_session"], str(live))

    def test_report_does_not_move_the_pointer_to_a_fresh_session(self) -> None:
        # The restart case: the recorded pointer must survive a session that
        # holds nothing, or the answer is unreachable for good.
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        restarted = self.live_session("restart")
        code, out, _ = self.cli(
            "report", "t-0001", env=self.fake_env(CLOWDER_FAKE_SESSION=str(restarted))
        )
        self.assertEqual(code, 0)
        self.assertIn("no answer yet", out)
        self.assertEqual(self.only_task()["agent_session"], str(self.session_file))

    def test_inbox_reads_the_recorded_session(self) -> None:
        # inbox and report share the rule, so they agree about which session a
        # step used.
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        self.session_turns()
        restarted = self.live_session("restart")
        code, out, _ = self.cli("inbox", env=self.fake_env(CLOWDER_FAKE_SESSION=str(restarted)))
        self.assertEqual(code, 0)
        self.assertIn("the answer (0)", out)
        self.assertIn("1 step(s) have reported", out)

    def test_report_reads_the_live_session_when_the_recorded_one_is_empty(self) -> None:
        # The mirror of the restart case: the pane restarted before the worker
        # answered, so the recorded session says nothing about this step and the
        # answer is in the live one. It must be found, and the pointer must move.
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        live = self.live_session("live")
        self.session_turns(path=live)
        code, out, _ = self.cli(
            "report", "t-0001", env=self.fake_env(CLOWDER_FAKE_SESSION=str(live))
        )
        self.assertEqual(code, 0)
        self.assertIn("the answer (0)", out)
        self.assertEqual(self.only_task()["agent_session"], str(live))

    def test_inbox_reads_the_live_session_when_the_recorded_one_is_empty(self) -> None:
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        live = self.live_session("live")
        self.session_turns(path=live)
        code, out, _ = self.cli("inbox", env=self.fake_env(CLOWDER_FAKE_SESSION=str(live)))
        self.assertEqual(code, 0)
        self.assertIn("the answer (0)", out)
        self.assertIn("1 step(s) have reported", out)

    def test_the_recorded_session_wins_over_a_live_session_with_its_own_answer(self) -> None:
        # Precedence, stated: when both sessions hold an answer for this step, the
        # one the step was dispatched into wins, and the pointer does not move.
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        self.session_turns()
        live = self.live_session("live")
        write_session(
            live,
            cwd=str(self.workspace / "myrepo"),
            turns=[
                {
                    "at": int(time.time() * 1000) + 1000,
                    "text": "the live answer",
                    "usage": default_usage(),
                }
            ],
        )
        code, out, _ = self.cli(
            "report", "t-0001", env=self.fake_env(CLOWDER_FAKE_SESSION=str(live))
        )
        self.assertEqual(code, 0)
        self.assertIn("the answer (0)", out)
        self.assertNotIn("the live answer", out)
        self.assertEqual(self.only_task()["agent_session"], str(self.session_file))

    # -- ensure ------------------------------------------------------------

    def seed_mux(
        self,
        agents: list[dict[str, object]],
        panes: dict[str, str] | None = None,
    ) -> str:
        path = self.root / "mux-state.json"
        write_fake_state(path, agents=agents, panes=panes)
        return str(path)

    def seed_agents(self, *names: str, cwd: str | None = None) -> None:
        """A stateful fake with the named agents, each with a session file."""
        sessions = self.root / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        agents = []
        for name in names:
            session = sessions / f"{name}.jsonl"
            session.write_text("", encoding="utf-8")
            agents.append(
                {"name": name, "cwd": cwd or str(self.repo_path), "session_file": str(session)}
            )
        self.seed_mux(agents)

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

    def test_ensure_defaults_to_a_new_tab(self) -> None:
        # No peer in the repo: the new pane still moves to a new tab, and the
        # command names no workspace, so it lands in the caller's own workspace.
        self.seed_mux([])
        code, _, err = self.cli("ensure", "myrepo", "--role", "maker", env=self.state_env())
        self.assertEqual(code, 0, err)
        state = json.loads((self.root / "mux-state.json").read_text(encoding="utf-8"))
        self.assertEqual(len(state["moves"]), 1, state["moves"])
        self.assertTrue(state["moves"][0]["new_tab"])
        self.assertEqual(state["moves"][0]["workspace_id"], "")

    def test_ensure_split_keeps_the_pane_beside_the_caller(self) -> None:
        self.seed_mux([])
        code, _, err = self.cli(
            "ensure", "myrepo", "--role", "maker", "--split", env=self.state_env()
        )
        self.assertEqual(code, 0, err)
        state = json.loads((self.root / "mux-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state.get("moves", []), [], "no move was asked for")

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
            "--worktree",
            str(self.repo_path / ".worktrees" / "myrepo-maker"),
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
            "--worktree",
            str(self.repo_path / ".worktrees" / "myrepo-maker"),
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

    def test_job_open_split_keeps_the_pane_beside_the_caller(self) -> None:
        # The other creation path, end to end: --split must skip the new-tab move.
        self.seed_mux([])
        code, _, err = self.cli(
            "job",
            "open",
            "myrepo",
            "--label",
            "refund",
            "--role",
            "maker",
            "--split",
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        state = json.loads((self.root / "mux-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state.get("moves", []), [], "an explicit split was asked for")
        self.assertEqual(state.get("closed", []), [], "an explicit split is kept")

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

    def test_job_open_writes_the_pane_remote_pi_config(self) -> None:
        job_id, maker_folder = self.open_a_job()
        self.assertTrue(job_id)
        config = maker_folder / ".pi" / "remote-pi" / "config.json"
        self.assertTrue(config.is_file(), "the pane can reach the relay")
        data = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual(data["agent_name"], "myrepo-maker")
        self.assertIs(data["auto_start_relay"], True)

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

    def open_a_job(
        self, label: str = "refund", extra: tuple[str, ...] = ()
    ) -> tuple[str, Path]:
        self.seed_mux([])
        code, out, err = self.cli(
            "job",
            "open",
            "myrepo",
            "--label",
            label,
            "--role",
            "maker",
            *extra,
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
        job_id, maker_folder = self.open_a_job()
        self.save_as_crew(maker_folder)
        self.handover(job_id)
        self.owner_pass(job_id)
        job = json.loads(self.state.read_text(encoding="utf-8"))["jobs"][job_id]
        self.assertEqual(job["pass_shown"], "the diff of task/refund")
        self.assertEqual(job["pass_answer"], "yes, ship it")
        self.assertEqual(job["pass_by"], "lacattano")
        self.assertTrue(job["pass_at"])

    def test_a_pass_is_refused_without_a_reviewed_commit(self) -> None:
        # The pass is bound to the reviewed commit; without one there is nothing to
        # bind, so the pass is refused rather than silently unbinding later.
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
            "lacattano",
            env=self.state_env(),
        )
        self.assertEqual(code, 1)
        self.assertIn("no reviewed commit", err)
        job = json.loads(self.state.read_text(encoding="utf-8"))["jobs"][job_id]
        self.assertIsNone(job["pass_at"], "nothing was recorded")

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
        job_id, maker_folder = self.open_a_job()
        self.save_as_crew(maker_folder)
        self.handover(job_id)
        self.owner_pass(job_id)
        with mock.patch.object(cli, "_publish_branch") as publish:
            code, out, err = self.cli("job", "publish", job_id, env=self.state_env())
        self.assertEqual(code, 0, err)
        publish.assert_called_once()
        self.assertIn("published", out)
        job = json.loads(self.state.read_text(encoding="utf-8"))["jobs"][job_id]
        self.assertTrue(job["published_at"])

    def test_merge_is_refused_without_the_owner_s_word(self) -> None:
        job_id, maker_folder = self.open_a_job()
        self.save_as_crew(maker_folder)
        self.handover(job_id)
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

    def test_a_merged_job_leaves_the_open_list_and_reads_merged(self) -> None:
        # `job merge` records merged_at but left the stored status open, so a
        # merged job stayed in open_jobs() and the jobs table called it in
        # progress. The merge finishes the job: it closes the record and frees the
        # space like `job close`, while keeping the branch.
        job_id, maker_folder = self.open_a_job()
        self.assertEqual(
            self.cli(
                "job",
                "word",
                job_id,
                "--word",
                "merge it",
                "--by",
                "lacattano",
                env=self.state_env(),
            )[0],
            0,
        )
        with mock.patch.object(cli, "_merge_pull_request"):
            code, _, err = self.cli("job", "merge", job_id, "--pr", "9", env=self.state_env())
        self.assertEqual(code, 0, err)
        reloaded = StateStore(self.state).get_job(job_id)
        self.assertEqual(reloaded.status, CLOSED)
        self.assertIsNotNone(reloaded.released_at, "the space is free")
        self.assertIsNone(gitcmd.current_branch(maker_folder), "the space is detached")
        self.assertIsNotNone(
            gitcmd.commit_of(self.repo_path, "task/refund"), "the branch is kept"
        )
        self.assertEqual(StateStore(self.state).open_jobs(), [], "a merged job is closed")

        code, out, err = self.cli("job", "list", "--all", env=self.state_env())
        self.assertEqual(code, 0, err)
        self.assertIn(job_id, out, "it is still in the full list")
        self.assertIn("merged", out, "and its state word says so")
        self.assertNotIn("in progress", out)

    def test_deleting_a_branch_needs_the_owner_s_word(self) -> None:
        job_id, _ = self.open_a_job()
        code, _, err = self.cli("job", "close", job_id, "--delete-branch", env=self.state_env())
        self.assertEqual(code, 1)
        self.assertIn("without the owner's word", err)
        self.assertIn(f"job word {job_id}", err)
        job = json.loads(self.state.read_text(encoding="utf-8"))["jobs"][job_id]
        self.assertEqual(job["status"], "open", "a refused close changes nothing")

    def test_job_list_shows_the_two_gates(self) -> None:
        job_id, maker_folder = self.open_a_job()
        self.save_as_crew(maker_folder)
        self.handover(job_id)
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

    def test_handover_split_keeps_the_reviewers_pane_beside_the_caller(self) -> None:
        # Reviewers are created through the same path. With --split the new pane
        # stays beside the caller instead of moving to a new tab.
        job_id, maker_folder = self.open_a_job()
        self.save_something(maker_folder)
        state_path = self.root / "mux-state.json"
        before = len(json.loads(state_path.read_text(encoding="utf-8")).get("moves", []))

        code, _, err = self.cli(
            "job",
            "handover",
            job_id,
            "--to",
            "verifier",
            "--name",
            "myrepo-verifier",
            "--split",
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        after = json.loads(state_path.read_text(encoding="utf-8")).get("moves", [])
        self.assertEqual(len(after), before, "the reviewer's pane was not moved")

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

    def test_a_fix_step_puts_the_writers_space_back_on_the_branch(self) -> None:
        # `job handover` releases the writer's space to the base. A later fix step
        # must put it back on the job's branch, or the worker edits the base and
        # the dispatch records the wrong commit.
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
        self.assertIsNone(gitcmd.current_branch(maker_folder), "handed back to the base")

        code, _, err = self.cli(
            "dispatch",
            "myrepo-maker",
            "myrepo",
            "ship: fix the refund page",
            "--job",
            job_id,
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(gitcmd.current_branch(maker_folder), "task/refund")
        self.assertEqual(self.only_task()["commit"], commit[:7], "the branch tip, not the base")

    def test_a_fix_step_refuses_when_the_space_cannot_take_the_branch(self) -> None:
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
        # An untracked file the branch tracks would be overwritten by the switch.
        (maker_folder / "refund.py").write_text("conflict\n", encoding="utf-8")
        code, _, err = self.cli(
            "dispatch",
            "myrepo-maker",
            "myrepo",
            "ship: fix the refund page",
            "--job",
            job_id,
            env=self.state_env(),
        )
        self.assertEqual(code, 2)
        self.assertIn("switch task/refund", err, "the refusal names the exact command")
        self.assertIsNone(gitcmd.current_branch(maker_folder), "the space was not moved")

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
        # The remedy is a fresh job, not a rebase: the guard reads the recorded base.
        self.assertIn("A rebase does not clear this guard", err)
        self.assertIn("fresh job", err)
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

    def test_publish_allows_a_branch_that_is_merely_behind(self) -> None:
        origin = self.add_origin_at_head()
        job_id, maker_folder = self.open_a_job()
        self.save_as_crew(maker_folder)
        self.handover(job_id)
        self.owner_pass(job_id)
        # origin/main advances normally. The base commit is still in its history,
        # so the guard allows the publish even though the branch is behind.
        self.advance_origin(origin)
        with mock.patch.object(cli.gitcmd, "push_branch") as pushed:
            code, _, err = self.cli("job", "publish", job_id, env=self.state_env())
        self.assertEqual(code, 0, err)
        pushed.assert_called_once()

    def test_handover_merges_the_base_and_keeps_the_writer_commits(self) -> None:
        origin = self.add_origin_at_head()
        job_id, maker_folder = self.open_a_job()
        writer = self.save_as_crew(maker_folder)
        self.advance_origin(origin)

        self.handover(job_id)

        tip = gitcmd.commit_of(maker_folder, "task/refund")
        self.assertIsNotNone(tip)
        assert tip is not None
        parents = gitcmd.run_git(
            maker_folder, "log", "-1", "--format=%P", "task/refund"
        ).split()
        self.assertEqual(len(parents), 2, "the sync made one merge commit")
        self.assertTrue(
            gitcmd.is_ancestor(maker_folder, writer, tip), "the writer's commit is unchanged"
        )
        self.assertEqual(
            gitcmd.count_commits(maker_folder, "task/refund..origin/main"),
            0,
            "the branch now contains origin/main",
        )
        job = json.loads(self.state.read_text(encoding="utf-8"))["jobs"][job_id]
        self.assertEqual(job["review_commit"], tip, "the reviewer is pinned at the merged tip")

    def test_handover_no_sync_leaves_the_branch_where_it_was(self) -> None:
        origin = self.add_origin_at_head()
        job_id, maker_folder = self.open_a_job()
        writer = self.save_as_crew(maker_folder)
        self.advance_origin(origin)

        code, _, err = self.cli(
            "job",
            "handover",
            job_id,
            "--to",
            "verifier",
            "--name",
            "myrepo-verifier",
            "--no-sync",
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(
            gitcmd.commit_of(maker_folder, "task/refund"),
            writer,
            "the branch was not merged with the base",
        )

    def test_handover_merge_conflict_aborts_and_leaves_a_clean_tree(self) -> None:
        origin = self.add_origin_at_head()
        job_id, maker_folder = self.open_a_job()
        writer = self.save_as_crew(maker_folder)
        self.advance_origin_editing(origin, "refund.py", "theirs\n")

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
        self.assertIn("conflicts", err)
        self.assertIn("refund.py", err)
        self.assertEqual(gitcmd.status_entries(maker_folder), [], "the tree is clean")
        self.assertIsNone(gitcmd.commit_of(maker_folder, "MERGE_HEAD"), "no merge is left")
        self.assertEqual(
            gitcmd.commit_of(maker_folder, "task/refund"), writer, "the branch was not moved"
        )

    def test_the_handover_merge_commit_carries_the_crew_identity(self) -> None:
        origin = self.add_origin_at_head()
        job_id, maker_folder = self.open_a_job()
        self.save_as_crew(maker_folder)
        self.advance_origin(origin)
        self.handover(job_id)

        shown = gitcmd.run_git(
            maker_folder, "log", "-1", "--format=%an <%ae>|%cn <%ce>", "task/refund"
        )
        self.assertIn("clowder-bot", shown)
        self.assertIn("94532220+lacattano@users.noreply.github.com", shown)

    def test_job_sync_merges_the_base_without_rewriting(self) -> None:
        origin = self.add_origin_at_head()
        job_id, maker_folder = self.open_a_job()
        writer = self.save_as_crew(maker_folder)
        self.advance_origin(origin)

        code, out, err = self.cli("job", "sync", job_id, env=self.state_env())
        self.assertEqual(code, 0, err)
        self.assertIn("synced", out)
        tip = gitcmd.commit_of(maker_folder, "task/refund")
        self.assertTrue(gitcmd.is_ancestor(maker_folder, writer, tip))
        self.assertEqual(gitcmd.count_commits(maker_folder, "task/refund..origin/main"), 0)

    def test_job_sync_after_handover_works_from_the_released_space(self) -> None:
        origin = self.add_origin_at_head()
        job_id, maker_folder = self.open_a_job()
        self.save_as_crew(maker_folder)
        self.handover(job_id)
        self.advance_origin(origin)

        code, _, err = self.cli("job", "sync", job_id, env=self.state_env())
        self.assertEqual(code, 0, err)
        self.assertEqual(gitcmd.count_commits(maker_folder, "task/refund..origin/main"), 0)
        self.assertIsNone(gitcmd.current_branch(maker_folder), "the space stays released")

    def test_job_sync_refuses_on_conflict_and_leaves_a_clean_tree(self) -> None:
        origin = self.add_origin_at_head()
        job_id, maker_folder = self.open_a_job()
        writer = self.save_as_crew(maker_folder)
        self.advance_origin_editing(origin, "refund.py", "theirs\n")

        code, _, err = self.cli("job", "sync", job_id, env=self.state_env())
        self.assertEqual(code, 2)
        self.assertIn("conflicts", err)
        self.assertEqual(gitcmd.status_entries(maker_folder), [], "the tree is clean")
        self.assertEqual(gitcmd.commit_of(maker_folder, "task/refund"), writer)

    def test_publish_allows_a_clean_sync_after_the_pass(self) -> None:
        origin = self.add_origin_at_head()
        job_id, maker_folder = self.open_a_job()
        self.save_as_crew(maker_folder)
        self.handover(job_id)
        self.owner_pass(job_id)
        self.advance_origin(origin)
        code, _, err = self.cli("job", "sync", job_id, env=self.state_env())
        self.assertEqual(code, 0, err)
        with mock.patch.object(cli.gitcmd, "push_branch") as pushed:
            code, _, err = self.cli("job", "publish", job_id, env=self.state_env())
        self.assertEqual(code, 0, err)
        pushed.assert_called_once()

    def test_publish_is_refused_when_the_branch_moved_past_the_pass(self) -> None:
        self.add_origin_at_head()
        job_id, maker_folder = self.open_a_job()
        self.save_as_crew(maker_folder)
        self.handover(job_id)
        self.owner_pass(job_id)
        # New writer work after the pass: the owner has not seen this commit.
        gitcmd.switch_branch(maker_folder, "task/refund")
        self.save_as_crew(maker_folder, "more.py")
        with mock.patch.object(cli.gitcmd, "push_branch") as pushed:
            code, _, err = self.cli("job", "publish", job_id, env=self.state_env())
        self.assertEqual(code, 1)
        self.assertIn("moved past the commit the owner passed", err)
        pushed.assert_not_called()

    def test_publish_is_refused_when_the_pass_commit_is_not_an_ancestor(self) -> None:
        self.add_origin_at_head()
        job_id, maker_folder = self.open_a_job()
        passed = self.save_as_crew(maker_folder)
        self.handover(job_id)
        self.owner_pass(job_id)
        # A rewrite after the pass: the branch no longer contains the passed commit.
        gitcmd.switch_branch(maker_folder, "task/refund")
        gitcmd.run_git(maker_folder, "reset", "--hard", "main")
        self.save_as_crew(maker_folder, "rewritten.py")
        self.assertFalse(gitcmd.is_ancestor(maker_folder, passed, "task/refund"))
        with mock.patch.object(cli.gitcmd, "push_branch") as pushed:
            code, _, err = self.cli("job", "publish", job_id, env=self.state_env())
        self.assertEqual(code, 1)
        self.assertIn("moved past the commit the owner passed", err)
        pushed.assert_not_called()

    def test_publish_refuses_an_appended_commit_when_the_base_ref_is_absent(self) -> None:
        # The remote-tracking base was pruned, renamed, or never fetched. The pass
        # binding must over-refuse, not fail open, on an appended writer commit.
        self.add_origin_at_head()
        job_id, maker_folder = self.open_a_job()
        self.save_as_crew(maker_folder)
        self.handover(job_id)
        self.owner_pass(job_id)
        gitcmd.switch_branch(maker_folder, "task/refund")
        self.save_as_crew(maker_folder, "more.py")
        gitcmd.run_git(maker_folder, "update-ref", "-d", "refs/remotes/origin/main")
        self.assertIsNone(gitcmd.commit_of(maker_folder, "origin/main"), "the ref is gone")
        with mock.patch.object(cli.gitcmd, "push_branch") as pushed:
            code, _, err = self.cli("job", "publish", job_id, env=self.state_env())
        self.assertEqual(code, 1)
        self.assertIn("moved past the commit the owner passed", err)
        pushed.assert_not_called()

    def test_a_stale_local_ref_does_not_hide_a_rewrite(self) -> None:
        origin = self.add_origin_at_head()
        base = gitcmd.head_commit(self.repo_path, short=False)
        job_id, maker_folder = self.open_a_job()
        self.save_as_crew(maker_folder)
        self.handover(job_id)
        self.owner_pass(job_id)
        # Rewrite origin/main and do NOT fetch. The local origin/main still names
        # the old history, which is the case that used to slip past the guard.
        self.rewrite_origin(origin)
        stale = gitcmd.commit_of(maker_folder, "refs/remotes/origin/main")
        self.assertEqual(stale, base, "the local remote-tracking ref is stale")

        with mock.patch.object(cli.gitcmd, "push_branch") as pushed:
            code, _, err = self.cli("job", "publish", job_id, env=self.state_env())
        self.assertEqual(code, 2)
        self.assertIn("not an ancestor", err)
        self.assertIn(base[:7], err)
        pushed.assert_not_called()

    def test_publish_skips_a_job_with_no_recorded_base(self) -> None:
        self.add_origin_at_head()
        job_id, maker_folder = self.open_a_job()
        self.save_as_crew(maker_folder)
        self.handover(job_id)
        self.owner_pass(job_id)
        # An older job has no recorded base. There is nothing to prove.
        payload = json.loads(self.state.read_text(encoding="utf-8"))
        payload["jobs"][job_id]["base_commit"] = None
        self.state.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        with mock.patch.object(cli.gitcmd, "push_branch") as pushed:
            code, _, err = self.cli("job", "publish", job_id, env=self.state_env())
        self.assertEqual(code, 0, err)
        pushed.assert_called_once()

    def test_publish_skips_a_base_with_no_origin_ref(self) -> None:
        # The remote has no main branch, so there is no ref to compare against.
        origin = self.root / "origin.git"
        gitcmd.run_git(self.root, "init", "--bare", "-b", "main", str(origin))
        gitcmd.run_git(self.repo_path, "remote", "add", "origin", str(origin))
        job_id, maker_folder = self.open_a_job()
        self.save_as_crew(maker_folder)
        self.handover(job_id)
        self.owner_pass(job_id)
        with mock.patch.object(cli.gitcmd, "push_branch") as pushed:
            code, _, err = self.cli("job", "publish", job_id, env=self.state_env())
        self.assertEqual(code, 0, err)
        pushed.assert_called_once()

    def test_job_open_records_the_true_fork_point_for_an_existing_branch(self) -> None:
        self.seed_mux([])
        self.assertEqual(self.open_job()[0], 0)
        fork = gitcmd.head_commit(self.repo_path, short=False)
        self.assertEqual(self.cli("job", "close", "j-0001", env=self.state_env())[0], 0)
        # The base moves on after the branch was cut. Resuming the branch must
        # record where it forked, not where the base is now.
        (self.repo_path / "later.txt").write_text("x\n", encoding="utf-8")
        gitcmd.run_git(self.repo_path, "add", "later.txt")
        gitcmd.run_git(self.repo_path, "commit", "-m", "later")
        code, _, err = self.open_job(env=self.state_env())
        self.assertEqual(code, 0, err)
        job = json.loads(self.state.read_text(encoding="utf-8"))["jobs"]["j-0002"]
        self.assertEqual(job["base_commit"], fork)

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

    def test_job_list_reads_under_review_while_the_review_runs(self) -> None:
        # The owner is not invited to walk while the review step is still open.
        job_id, maker_folder = self.open_a_job()
        self.save_something(maker_folder)
        self.handover(job_id)

        code, out, err = self.cli("job", "list", env=self.state_env())
        self.assertEqual(code, 0, err)
        self.assertIn("waiting on a walkthrough", out, "no review step yet")

        code, _, err = self.cli(
            "dispatch",
            "myrepo-verifier",
            "myrepo",
            "scout: check the save",
            "--job",
            job_id,
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("job", "list", env=self.state_env())
        self.assertEqual(code, 0, err)
        self.assertIn("under review", out)
        self.assertNotIn("waiting on a walkthrough", out)

        self.answer_the_step("myrepo-verifier")
        self.assertEqual(self.cli("report", "t-0001", env=self.state_env())[0], 0)
        code, out, err = self.cli("job", "list", env=self.state_env())
        self.assertEqual(code, 0, err)
        self.assertIn("waiting on a walkthrough", out, "the step reported")
        self.assertNotIn("under review", out)

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
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
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
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        code, out, _ = self.cli("board", "--json", env=self.fake_env())
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(Path(payload["board"]).is_file())
        self.assertEqual(payload["tasks"], 1)
        self.assertEqual(payload["open"], 1)
        self.assertEqual(payload["agents"], 1)
        self.assertFalse(payload["live_ok"], "the page is drawn from the state file")
        code, out, _ = self.cli("board", "--json", "--live", env=self.fake_env())
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["live_ok"], "--live asks for the real roster")

    def test_board_can_be_written_where_you_ask(self) -> None:
        target = self.root / "pages" / "b.html"
        code, out, _ = self.cli("board", "--out", str(target), env=self.fake_env())
        self.assertEqual(code, 0)
        self.assertEqual(Path(out.strip()), target)
        self.assertTrue(target.is_file())

    def test_the_board_still_works_with_no_live_agent_list(self) -> None:
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        # The default page never asks, so it draws whatever the multiplexer is doing.
        code, out, _ = self.cli("board", env=self.fake_env(CLOWDER_FAKE_LIST_FAIL="1"))
        self.assertEqual(code, 0, "state alone is still worth showing")
        html = Path(out.strip()).read_text(encoding="utf-8")
        self.assertIn("t-0001", html)
        self.assertIn("roster from the state file", html)

        # And `--live` against a failing multiplexer says so, and still draws.
        code, out, _ = self.cli(
            "board", "--live", env=self.fake_env(CLOWDER_FAKE_LIST_FAIL="1")
        )
        self.assertEqual(code, 0)
        html = Path(out.strip()).read_text(encoding="utf-8")
        self.assertIn("could not be read", html)
        self.assertIn("t-0001", html)

    def test_board_on_an_empty_store(self) -> None:
        code, out, _ = self.cli("board", env=self.fake_env())
        self.assertEqual(code, 0)
        html = Path(out.strip()).read_text(encoding="utf-8")
        self.assertIn("Nothing is recorded as waiting on you.", html)

    def test_an_owner_item_shows_and_clears(self) -> None:
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
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
        self.dispatch("maker", "myrepo", "ship: first thing", "--worktree", str(self.repo_path))
        self.cli("owner", "t-0001", "--item", "choose A or B", env=self.fake_env())
        # Report the first step first: a second send to the same agent while it is
        # open is refused by the preempt guard, so t-0002 would never exist.
        self.session_turns()
        self.assertEqual(self.cli("report", "t-0001", env=self.fake_env())[0], 0)
        self.dispatch(
            "maker", "myrepo", "ship: second thing", "--worktree", str(self.repo_path)
        )
        self.cli("owner", "t-0002", "--item", "choose C or D", env=self.fake_env())
        html = (self.root / "board.html").read_text(encoding="utf-8")
        self.assertIn("<b>1.</b> <span class='owner-title'>choose A or B</span>", html)
        self.assertIn("<b>2.</b> <span class='owner-title'>choose C or D</span>", html)

    def test_recording_the_answer_clears_the_owner_item(self) -> None:
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
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

    def test_an_owner_item_records_a_detail(self) -> None:
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        detail = "A: 14 days, costs X.\nB: 30 days, costs Y.\nRecommend A."
        code, _, err = self.cli(
            "owner",
            "t-0001",
            "--item",
            "choose A or B",
            "--detail",
            detail,
            env=self.fake_env(),
        )
        self.assertEqual(code, 0, err)
        task = self.only_task()
        self.assertEqual(task["owner_item"], "choose A or B")
        self.assertEqual(task["owner_detail"], detail)
        html = (self.root / "board.html").read_text(encoding="utf-8")
        self.assertIn("choose A or B", html)
        self.assertIn("Recommend A.", html)

    def test_an_owner_detail_can_come_from_a_file(self) -> None:
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        path = self.root / "detail.md"
        path.write_text("A: 14 days.\nB: 30 days.\nRecommend B.", encoding="utf-8")
        code, _, err = self.cli(
            "owner",
            "t-0001",
            "--item",
            "choose A or B",
            "--detail-file",
            str(path),
            env=self.fake_env(),
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(
            self.only_task()["owner_detail"], "A: 14 days.\nB: 30 days.\nRecommend B."
        )

    def test_owner_clear_removes_the_item_and_its_detail(self) -> None:
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        self.cli(
            "owner",
            "t-0001",
            "--item",
            "choose A or B",
            "--detail",
            "A costs X.",
            env=self.fake_env(),
        )
        code, _, err = self.cli("owner", "t-0001", "--clear", env=self.fake_env())
        self.assertEqual(code, 0, err)
        task = self.only_task()
        self.assertIsNone(task["owner_item"])
        self.assertIsNone(task["owner_detail"])

    def test_an_owner_item_alone_records_no_detail(self) -> None:
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        self.cli("owner", "t-0001", "--item", "choose A or B", env=self.fake_env())
        self.assertIsNone(self.only_task()["owner_detail"])

    def test_an_owner_detail_needs_an_item(self) -> None:
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        code, _, err = self.cli(
            "owner", "t-0001", "--detail", "A costs X.", env=self.fake_env()
        )
        self.assertEqual(code, 1)
        self.assertIn("--detail needs --item", err)

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

    def make_orphaned_merge_commit(self) -> str:
        """A merge commit on no branch, the shape a later rebase can leave behind.

        The job branch is deleted and main never saw the merge, so the commit is
        reachable from nothing. Its two parents give it no patch-id of its own, so
        the board can only call it a merge.
        """
        repo = self.repo_path
        gitcmd.switch_new_branch(repo, "task/merged", "main")
        (repo / "one.txt").write_text("one\n", encoding="utf-8")
        gitcmd.run_git(repo, "add", "one.txt")
        gitcmd.run_git(repo, "commit", "-m", "one")
        gitcmd.switch_new_branch(repo, "task/side", "main")
        (repo / "two.txt").write_text("two\n", encoding="utf-8")
        gitcmd.run_git(repo, "add", "two.txt")
        gitcmd.run_git(repo, "commit", "-m", "two")
        gitcmd.switch_branch(repo, "task/merged")
        gitcmd.run_git(repo, "merge", "--no-ff", "-m", "merge side", "task/side")
        merged = gitcmd.head_commit(repo, short=False) or ""
        gitcmd.switch_branch(repo, "main")
        gitcmd.run_git(repo, "branch", "-D", "task/merged")
        gitcmd.run_git(repo, "branch", "-D", "task/side")
        return merged

    def write_pinned_task(
        self,
        task_id: str,
        commit: str,
        worktree: str | None = None,
        job: str | None = None,
        status: str = DISPATCHED,
    ) -> None:
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
                job=job,
                commit=commit,
                status=status,
            )
        )
        store.save()

    def write_job(
        self,
        job_id: str,
        branch: str,
        commit: str | None = None,
        held_ref: str | None = None,
        reviewer: str | None = None,
        review_commit: str | None = None,
        handed_over_at: str | None = None,
        merged_at: str | None = None,
        closed_at: str | None = None,
        pass_at: str | None = None,
        published_at: str | None = None,
        merge_word_at: str | None = None,
        status: str = OPEN,
    ) -> None:
        store = StateStore(self.state)
        store.add_job(
            Job(
                id=job_id,
                label="refund",
                repo="myrepo",
                repo_path=str(self.repo_path),
                worktree=str(self.repo_path),
                branch=branch,
                base="main",
                agent="maker",
                commit=commit,
                held_ref=held_ref,
                reviewer=reviewer,
                review_commit=review_commit,
                handed_over_at=handed_over_at,
                merged_at=merged_at,
                closed_at=closed_at,
                pass_at=pass_at,
                published_at=published_at,
                merge_word_at=merge_word_at,
                status=status,
            )
        )
        store.save()

    def write_held_job(self, job_id: str, branch: str, moved: bool) -> tuple[str, str]:
        """A branch holding a reviewed commit, optionally moved past it."""
        repo = self.repo_path
        gitcmd.switch_new_branch(repo, branch, "main")
        (repo / f"{job_id}.txt").write_text("reviewed\n", encoding="utf-8")
        gitcmd.run_git(repo, "add", f"{job_id}.txt")
        gitcmd.run_git(repo, "commit", "-m", f"reviewed {job_id}")
        checked = gitcmd.head_commit(repo, short=False) or ""
        tip = checked
        if moved:
            (repo / f"{job_id}-more.txt").write_text("more\n", encoding="utf-8")
            gitcmd.run_git(repo, "add", f"{job_id}-more.txt")
            gitcmd.run_git(repo, "commit", "-m", f"moved {job_id}")
            tip = gitcmd.head_commit(repo, short=False) or ""
        self.write_job(
            job_id,
            branch=branch,
            reviewer="verifier",
            review_commit=checked,
            handed_over_at="2026-10-01T00:00:00Z",
        )
        return checked, tip

    def test_the_board_calls_a_moved_after_review_tip_needs_rereview(self) -> None:
        checked, tip = self.write_held_job("j-0001", "task/stale", moved=True)
        code, _, err = self.cli("board", env=self.fake_env())
        self.assertEqual(code, 0, err)
        owner = (self.root / "board.html").read_text(encoding="utf-8")
        owner = owner.split("For you", 1)[1].split("Running now", 1)[0]
        owner = owner.split("Filters")[0]
        self.assertIn("moved to", owner)
        self.assertIn("the reviewer must check it again", owner)
        self.assertNotIn("waiting for your walkthrough", owner)
        self.assertIn(checked[:7], owner, "the checked commit is named")
        self.assertIn(tip[:7], owner, "the current tip is named")

    def test_the_board_keeps_held_for_review_when_the_tip_was_checked(self) -> None:
        checked, tip = self.write_held_job("j-0001", "task/held", moved=False)
        self.assertEqual(checked, tip)
        code, _, err = self.cli("board", env=self.fake_env())
        self.assertEqual(code, 0, err)
        owner = (self.root / "board.html").read_text(encoding="utf-8")
        owner = owner.split("For you", 1)[1].split("Running now", 1)[0]
        owner = owner.split("Filters")[0]
        self.assertIn("waiting for your walkthrough", owner)
        self.assertNotIn("moved to", owner)

    def test_job_list_calls_a_moved_after_review_tip_needs_rereview(self) -> None:
        self.write_held_job("j-0001", "task/stale", moved=True)
        code, out, err = self.cli("job", "list", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("needs re-review", out)
        self.assertNotIn("waiting on a walkthrough", out)

    def test_job_list_keeps_waiting_on_a_walkthrough_when_the_tip_was_checked(self) -> None:
        self.write_held_job("j-0001", "task/held", moved=False)
        code, out, err = self.cli("job", "list", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("waiting on a walkthrough", out)
        self.assertNotIn("needs re-review", out)

    def test_job_list_calls_an_unreadable_tip_review_unconfirmed(self) -> None:
        # A read failure must not fall back to "waiting on a walkthrough": that is
        # the stale-walk claim this state exists to prevent.
        self.write_held_job("j-0001", "task/hold", moved=False)
        with mock.patch.object(gitcmd, "commit_of", return_value=None):
            code, out, err = self.cli("job", "list", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("review unconfirmed", out)
        self.assertNotIn("waiting on a walkthrough", out)

    def test_the_board_calls_an_unreadable_tip_review_unconfirmed(self) -> None:
        self.write_held_job("j-0001", "task/hold", moved=False)
        with mock.patch.object(gitcmd, "commit_of", return_value=None):
            code, _, err = self.cli("board", env=self.fake_env())
        self.assertEqual(code, 0, err)
        owner = (self.root / "board.html").read_text(encoding="utf-8")
        owner = owner.split("For you", 1)[1].split("Running now", 1)[0]
        owner = owner.split("Filters")[0]
        self.assertIn("could not be read", owner)
        self.assertNotIn("waiting for your walkthrough", owner)

    def test_the_board_does_not_flag_a_rebased_away_commit(self) -> None:
        old, lost = self.make_rebased_commits()
        self.write_pinned_task("t-0001", old)
        self.write_pinned_task("t-0002", lost)
        code, _, err = self.cli("board", env=self.fake_env())
        self.assertEqual(code, 0, err)
        html = (self.root / "board.html").read_text(encoding="utf-8")
        worker = html.split("Waiting for a worker", 1)[1].split("What landed", 1)[0]
        self.assertIn("t-0002", worker, "a genuinely lost commit is still at risk")
        self.assertNotIn("t-0001", worker, "a rebased-away commit is not at risk")

    def test_a_superseded_step_commit_is_not_flagged(self) -> None:
        # The job's branch moved past the step's commit (an amend or a reversed
        # change), so the later commit carries the job's work; the old hash is not
        # at risk and gets no line.
        _, lost = self.make_rebased_commits()
        self.write_job("j-0001", branch="task/x")
        self.write_pinned_task("t-0001", lost, job="j-0001")
        code, _, err = self.cli("board", env=self.fake_env())
        self.assertEqual(code, 0, err)
        worker = (self.root / "board.html").read_text(encoding="utf-8")
        worker = worker.split("Waiting for a worker", 1)[1].split("What landed", 1)[0]
        self.assertNotIn("t-0001", worker, "the job's branch moved past the commit")

    def test_a_reported_step_on_a_merged_job_leaves_the_worker_queue(self) -> None:
        # t-0613 and t-0615 on job j-0124: a later rebase orphaned the recorded
        # commit, but the step reported and the job merged long ago. The commit's
        # reachability alone must not keep it in the worker queue.
        merged = self.make_orphaned_merge_commit()
        self.write_job("j-0001", branch="task/merged", merged_at="2026-09-28T10:00:00Z")
        self.write_pinned_task("t-0001", merged, job="j-0001", status=REPORTED)
        code, _, err = self.cli("board", env=self.fake_env())
        self.assertEqual(code, 0, err)
        html = (self.root / "board.html").read_text(encoding="utf-8")
        worker = html.split("Waiting for a worker", 1)[1].split("To Do", 1)[0]
        self.assertNotIn("t-0001", worker, "a reported step on a merged job is done")
        self.assertNotIn("merge commit", worker, "no stale merge warning either")
        owner = html.split("For you", 1)[1].split("Running now", 1)[0]
        self.assertNotIn("t-0001", owner, "it is not work at risk for the owner")

    def test_an_orphaned_merge_commit_on_an_open_job_still_waits(self) -> None:
        # The gate is the step's status and the job's state, not blanket silence:
        # a merge commit on no branch for a step that has not landed still warns.
        merged = self.make_orphaned_merge_commit()
        self.write_job("j-0001", branch="task/merged")
        self.write_pinned_task("t-0001", merged, job="j-0001", status=REPORTED)
        code, _, err = self.cli("board", env=self.fake_env())
        self.assertEqual(code, 0, err)
        html = (self.root / "board.html").read_text(encoding="utf-8")
        worker = html.split("Waiting for a worker", 1)[1].split("To Do", 1)[0]
        self.assertIn("t-0001", worker)
        self.assertIn("merge commit", worker)

    def test_an_orphaned_commit_on_a_closed_job_still_warns(self) -> None:
        # Closing a job can release a space whose commit never merged. Only a
        # merge puts work in history, so a closed-but-unmerged job must not
        # silence a commit that is on no branch.
        merged = self.make_orphaned_merge_commit()
        self.write_job(
            "j-0001",
            branch="task/merged",
            status=CLOSED,
            closed_at="2026-09-28T10:00:00Z",
        )
        self.write_pinned_task("t-0001", merged, job="j-0001", status=REPORTED)
        code, _, err = self.cli("board", env=self.fake_env())
        self.assertEqual(code, 0, err)
        html = (self.root / "board.html").read_text(encoding="utf-8")
        worker = html.split("Waiting for a worker", 1)[1].split("To Do", 1)[0]
        self.assertIn("t-0001", worker, "a closed job did not merge the work")
        self.assertIn("merge commit", worker)

    def test_a_genuinely_stranded_commit_names_the_fix(self) -> None:
        _, lost = self.make_rebased_commits()
        self.write_pinned_task("t-0001", lost, worktree=str(self.repo_path))
        code, _, err = self.cli("board", env=self.fake_env())
        self.assertEqual(code, 0, err)
        page = (self.root / "board.html").read_text(encoding="utf-8")
        # Keeping the commit is the front door's act, so the warning is in the worker
        # section and not in his queue.
        worker = page.split("Waiting for a worker", 1)[1].split("What landed", 1)[0]
        self.assertIn("t-0001", worker)
        self.assertIn("on no branch", worker)
        self.assertIn(f"branch keep-t-0001 {lost}", worker)

    def test_two_tasks_sharing_a_commit_cost_one_look(self) -> None:
        # A board write asks about every task that holds a commit, and each look
        # costs a git process. Two steps on one commit must ask git once.
        commit = gitcmd.head_commit(self.repo_path, short=False) or ""
        self.write_pinned_task("t-0001", commit)
        self.write_pinned_task("t-0002", commit)
        asked: list[str] = []
        real = gitcmd.commit_risk

        def counting(path: str, asked_commit: str) -> str:
            asked.append(asked_commit)
            return real(path, asked_commit)

        with mock.patch.object(gitcmd, "commit_risk", counting):
            code, _, err = self.cli("board", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertEqual(asked.count(commit), 1, "one commit, one look")

    def test_a_warm_memo_still_reports_a_commit_that_lost_its_branch(self) -> None:
        # The memo must never hide a genuinely lost commit, including when the
        # answer was remembered while the commit was still safe.
        repo = self.repo_path
        gitcmd.switch_new_branch(repo, "task/lost", "main")
        (repo / "gone.txt").write_text("gone\n", encoding="utf-8")
        gitcmd.run_git(repo, "add", "gone.txt")
        gitcmd.run_git(repo, "commit", "-m", "work to lose")
        commit = gitcmd.head_commit(repo, short=False) or ""
        self.write_pinned_task("t-0001", commit)

        code, _, err = self.cli("board", env=self.fake_env())
        self.assertEqual(code, 0, err)
        page = (self.root / "board.html").read_text(encoding="utf-8")
        worker = page.split("Waiting for a worker", 1)[1].split("What landed", 1)[0]
        self.assertNotIn("t-0001", worker, "on its branch, so not at risk yet")

        gitcmd.switch_branch(repo, "main")
        gitcmd.run_git(repo, "branch", "-D", "task/lost")
        code, _, err = self.cli("board", env=self.fake_env())
        self.assertEqual(code, 0, err)
        page = (self.root / "board.html").read_text(encoding="utf-8")
        worker = page.split("Waiting for a worker", 1)[1].split("What landed", 1)[0]
        self.assertIn("t-0001", worker, "the warm memo did not hide the lost commit")

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

    def test_report_does_not_flag_a_superseded_step_commit(self) -> None:
        # The same rule as the board: a job whose branch moved past the step's
        # commit means the old hash is superseded, not lost.
        _, lost = self.make_rebased_commits()
        self.write_job("j-0001", branch="task/x")
        self.write_pinned_task("t-0001", lost, job="j-0001")
        code, out, err = self.cli("report", "t-0001", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertNotIn("on no branch", out, "the job's branch moved past the commit")

    def test_report_does_not_flag_a_reported_step_on_a_merged_job(self) -> None:
        # The report and the board must agree. A closed branch and a merged job
        # mean the step's work has landed, so the stale warning must not print.
        # The branch is gone, so `_job_moved_past` alone cannot clear it. Pin the
        # step in its own detached worktree, or `report` would replace the commit
        # with the worktree HEAD and the guard would never be asked.
        _, lost = self.make_rebased_commits()
        lost_wt = self.repo_path / ".worktrees" / "lost"
        gitcmd.add_worktree_free(self.repo_path, lost_wt, lost)
        self.write_job("j-0001", branch="task/gone", merged_at="2026-09-28T10:00:00Z")
        self.write_pinned_task(
            "t-0001", lost, worktree=str(lost_wt), job="j-0001", status=REPORTED
        )
        code, out, err = self.cli("report", "t-0001", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertNotIn("on no branch", out, "the merged job owns the work")

    def test_dispatch_refreshes_the_board_where_the_state_lives(self) -> None:
        # The page is a byproduct of the command, not a step a human remembers.
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        page = self.root / "board.html"
        self.assertTrue(page.is_file(), "dispatch wrote the page next to the state file")
        html = page.read_text(encoding="utf-8")
        self.assertIn("t-0001", html)
        self.assertIn("add the refund page", html)
        self.assertIn("For you", html, "the page keeps its first section")

    def test_a_board_that_cannot_be_written_does_not_fail_the_command(self) -> None:
        with mock.patch.object(cli, "write_board", side_effect=OSError("disk full")):
            code, out, err = self.cli(
                "dispatch",
                "maker",
                "myrepo",
                "ship: add the refund page",
                "--worktree",
                str(self.repo_path),
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
        self.assertTrue(refreshes("queue", "drop", "q-0001", "--why", "done"))
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

    def queue_scout(self, *extra: str, env: dict[str, object] | None = None):
        """A queued item that changes nothing, so it needs no job to be sent.

        A write item carries no worktree, so the queue refuses to send one with no
        job; the tests below are about the send path, not about that refusal, so they
        queue a scout.
        """
        return self.cli(
            "queue",
            "add",
            "myrepo",
            "scout: read the refund page",
            "--why",
            "the space holds an open job",
            "--shape",
            "scout",
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

    def test_queue_add_records_labels(self) -> None:
        code, _, err = self.queue_add("--agent", "maker", "--label", "sev:high")
        self.assertEqual(code, 0, err)
        item = json.loads(self.state.read_text(encoding="utf-8"))["queued"]["q-0001"]
        self.assertEqual(item["labels"], ["sev:high"])

    def test_queue_add_records_a_heavy_step(self) -> None:
        code, _, err = self.queue_add("--agent", "maker", "--heavy")
        self.assertEqual(code, 0, err)
        item = json.loads(self.state.read_text(encoding="utf-8"))["queued"]["q-0001"]
        self.assertEqual(item["cost"], "suite")
        self.assertTrue(item["heavy"])

    def test_a_heavy_queued_step_sends_heavy(self) -> None:
        # The class travels with the item, so the task it becomes costs the same.
        self.queue_scout("--agent", "maker", "--heavy")
        with mock.patch.object(box, "read", return_value=reading(available_gib=20.0)):
            code, _, err = self.cli("queue", "send", "q-0001", env=self.fake_env())
        self.assertEqual(code, 0, err)
        task = json.loads(self.state.read_text(encoding="utf-8"))["tasks"]["t-0001"]
        self.assertEqual(task["cost"], "suite")
        self.assertTrue(task["heavy"])

    def test_a_queued_send_is_gated_by_the_box_like_a_dispatch(self) -> None:
        # The hole the gate exists to close: a step sent through the queue is measured
        # the same way, so its class cannot slip past the box's headroom.
        self.seed_agents("myrepo-maker", "myrepo-verifier")
        code, _, err = self.cli(
            "dispatch",
            "myrepo-maker",
            "myrepo",
            "scout: build the image",
            "--shape",
            "scout",
            "--heavy",
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        self.queue_scout(
            "--agent", "myrepo-verifier", "--cost", "browser", env=self.state_env()
        )
        with mock.patch.object(box, "read", return_value=reading(available_gib=10.0)):
            code, _, err = self.cli("queue", "send", "q-0001", env=self.state_env())
        self.assertEqual(code, 2)
        self.assertIn("6.0 GiB this browser step needs", err)
        payload = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIn("q-0001", payload["queued"], "a refused send stays queued")

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
        owner, worker = html.split("For you", 1)[1].split("Waiting for a worker", 1)
        self.assertIn("class='count'>0", owner, "the owner's count stays honest")
        self.assertNotIn("q-0001", owner, "a queued item is the front door's, not the owner's")
        self.assertIn("q-0001", worker)
        self.assertIn("the space holds an open job", worker)

    def test_queue_drop_removes_the_item_and_writes_one_audit_line(self) -> None:
        self.queue_add("--agent", "maker")
        code, out, err = self.cli(
            "queue",
            "drop",
            "q-0001",
            "--why",
            "already merged under PR #27",
            "--by",
            "topcat",
            env=self.fake_env(),
        )
        self.assertEqual(code, 0, err)
        self.assertIn("q-0001 dropped: already merged under PR #27", out)

        payload = self.load_state()
        self.assertEqual(payload["queued"], {}, "the item left the waiting list")

        audit_file = self.root / "state.json.audit"
        line = json.loads(audit_file.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(line["action"], "queue-drop")
        self.assertEqual(line["item"], "q-0001")
        self.assertEqual(line["reason"], "already merged under PR #27")
        self.assertEqual(line["by"], "topcat")
        self.assertEqual(line["repo"], "myrepo")
        self.assertEqual(line["question"], "ship: add the refund page")

    def test_queue_drop_refuses_without_a_why(self) -> None:
        self.queue_add("--agent", "maker")
        code, _, err = self.cli("queue", "drop", "q-0001", env=self.fake_env())
        self.assertEqual(code, 1)
        self.assertIn("--why", err)
        self.assertIn("q-0001", self.load_state()["queued"], "the item stays put")

    def test_queue_drop_of_an_unknown_item_refuses(self) -> None:
        code, _, err = self.cli(
            "queue",
            "drop",
            "q-9999",
            "--why",
            "already done",
            "--by",
            "topcat",
            env=self.fake_env(),
        )
        self.assertEqual(code, 1)
        self.assertIn("q-9999", err)

    def test_a_dropped_item_moves_to_the_board_history(self) -> None:
        self.queue_add("--agent", "maker")
        code, _, err = self.cli(
            "queue",
            "drop",
            "q-0001",
            "--why",
            "already merged under PR #27",
            "--by",
            "topcat",
            env=self.fake_env(),
        )
        self.assertEqual(code, 0, err)
        html = (self.root / "board.html").read_text(encoding="utf-8")
        self.assertIn("Dropped from the queue", html)
        worker = html.split("Waiting for a worker", 1)[1].split("To Do", 1)[0]
        self.assertNotIn("q-0001", worker, "a dropped item is out of the waiting list")
        history = html.split("Dropped from the queue", 1)[1]
        self.assertIn("q-0001", history)
        self.assertIn("already merged under PR #27", history)
        self.assertIn("topcat", history)

    def test_queue_drop_refuses_without_a_by(self) -> None:
        # The owner's decision at t-0525: there is no guest, so there is always a
        # person or agent to attribute the drop to. "unknown" is not an answer.
        self.queue_add("--agent", "maker")
        code, _, err = self.cli("queue", "drop", "q-0001", "--why", "done", env=self.fake_env())
        self.assertEqual(code, 1)
        self.assertIn("--by", err)
        self.assertIn("q-0001", self.load_state()["queued"], "the item stays put")
        self.assertFalse((self.root / "state.json.audit").exists(), "no line is written")

    def test_queue_send_dispatches_and_removes_the_item(self) -> None:
        self.queue_scout("--agent", "maker")
        code, out, err = self.cli("queue", "send", "q-0001", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("t-0001 sent to maker", out)
        self.assertIn("q-0001 sent and removed from the queue", out)

        payload = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(payload["queued"], {})
        self.assertIn("t-0001", payload["tasks"])

    def test_queue_send_resolves_a_role_to_a_live_agent(self) -> None:
        self.queue_scout("--role", "maker")
        code, out, err = self.cli("queue", "send", "q-0001", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("sent to maker", out)

    def test_queue_send_keeps_the_item_when_the_dispatch_fails(self) -> None:
        self.queue_scout("--agent", "maker")
        code, _, _ = self.cli(
            "queue", "send", "q-0001", env=self.fake_env(CLOWDER_FAKE_PROMPT_FAIL="1")
        )
        self.assertEqual(code, 2)
        payload = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIn("q-0001", payload["queued"], "a failed send stays in the queue")

    def test_queue_send_by_role_with_no_such_agent_refuses(self) -> None:
        self.queue_scout("--role", "verifier")
        code, _, err = self.cli("queue", "send", "q-0001", env=self.fake_env())
        self.assertEqual(code, 2)
        self.assertIn("no agent in myrepo plays 'verifier'", err)
        payload = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIn("q-0001", payload["queued"])

    def test_queue_send_respects_the_cross_repo_guard(self) -> None:
        other = self.workspace / "other"
        other.mkdir()
        self.queue_scout("--agent", "maker")
        code, _, err = self.cli(
            "queue", "send", "q-0001", env=self.fake_env(CLOWDER_FAKE_CWD=str(other))
        )
        self.assertEqual(code, 2)
        self.assertIn("which is not", err)
        payload = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIn("q-0001", payload["queued"])

    def test_queue_send_dry_run_sends_nothing_and_keeps_the_item(self) -> None:
        self.queue_scout("--agent", "maker")
        code, out, err = self.cli("queue", "send", "q-0001", "--dry-run", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("would run:", out)
        payload = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIn("q-0001", payload["queued"])

    def test_queue_send_gives_a_write_item_its_job_at_send_time(self) -> None:
        # The block that stopped the first attempt has cleared, so the item can be
        # given the job it belongs to - and its save lands on that job's branch.
        job_id, worktree = self.open_a_job()
        self.assertEqual(
            self.cli(
                "queue",
                "add",
                "myrepo",
                "ship: add the refund page",
                "--why",
                "the space holds an open job",
                "--agent",
                "myrepo-maker",
                env=self.state_env(),
            )[0],
            0,
        )
        code, out, err = self.cli(
            "queue", "send", "q-0001", "--job", job_id, env=self.state_env()
        )
        self.assertEqual(code, 0, err)
        self.assertIn("q-0001 sent and removed from the queue", out)

        task = self.only_task()
        self.assertEqual(task["job"], job_id)
        self.assertEqual(task["branch"], "task/refund")
        self.assertEqual(task["worktree"], str(worktree))
        self.assertTrue(task["commit"])

    def test_queue_send_refuses_a_write_item_with_no_place(self) -> None:
        # A queued item carries no worktree, so its job is the only place its save
        # can go. Sending it with neither would put it on no branch.
        self.queue_add("--agent", "maker")
        code, _, err = self.cli("queue", "send", "q-0001", env=self.fake_env())
        self.assertEqual(code, 1)
        self.assertIn("no job", err)
        self.assertIn("--job", err)
        payload = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIn("q-0001", payload["queued"], "a refused item stays in the queue")
        self.assertEqual(payload["tasks"], {})

    def test_a_no_job_step_records_the_agents_own_space(self) -> None:
        # The sibling defect: a step sent to an agent whose space holds a job was
        # recorded with no folder, and a report then called it the main checkout.
        # A ship now has to name a place, so this case is a scout - the shape that
        # still needs no place, and still has to record where it ran. The job is
        # closed first: an open job in the space now refuses the send (t-0742).
        job_id, worktree = self.open_a_job()
        self.assertEqual(self.cli("job", "close", job_id, env=self.state_env())[0], 0)
        self.assertEqual(self.step("--shape", "scout", "scout: add the refund page")[0], 0)
        task = self.only_task()
        self.assertIsNone(task["job"], "the step is not part of a job")
        self.assertEqual(task["worktree"], str(worktree), "the folder it ran in is recorded")

        code, out, err = self.cli("report", "t-0001", env=self.state_env())
        self.assertEqual(code, 0, err)
        self.assertIn("worktree myrepo-maker", out)
        self.assertNotIn("main checkout", out)

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
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
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
            "--worktree",
            str(self.repo_path),
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
            [{"name": name, "cwd": str(self.repo_path), "session_file": str(session)}],
            panes={"w9:p1": str(self.repo_path)},
        )
        return session

    def seed_agent_model(self, session: Path, provider: str, model: str, thinking: str) -> None:
        """Give a seeded session the model Pi would have recorded."""
        records = [
            {"type": "session", "version": 3, "id": "old-session", "cwd": str(self.repo_path)},
            {"type": "model_change", "provider": provider, "modelId": model},
            {"type": "thinking_level_change", "thinkingLevel": thinking},
        ]
        session.write_text(
            "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
        )

    @staticmethod
    def start_count(path: Path) -> int:
        """How many `agent start` calls were made, counted by the launcher.

        The launcher writes this before the fake runs, so a start killed
        mid-attempt is counted too. Reading the fake's own log would race that
        kill and see nothing.
        """
        if not path.is_file():
            return 0
        return len(path.read_text(encoding="utf-8").splitlines())

    @staticmethod
    def reseed_help() -> str:
        """The `agent reseed --help` text, as the owner reads it."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.suppress(SystemExit):
            cli.build_parser().parse_args(["agent", "reseed", "--help"])
        return out.getvalue()

    def test_agent_reset_changes_the_session_and_names_it(self) -> None:
        self.seed_agent()
        code, out, err = self.cli(
            "agent", "reset", "myrepo-maker", "--timeout", "2", env=self.state_env()
        )
        self.assertEqual(code, 0, err)
        self.assertIn("reset: new session", out)
        self.assertIn("myrepo-maker-reset", out)

    def test_agent_reset_reports_and_records_the_model(self) -> None:
        # A fresh session takes the startup default, so the reset can silently
        # swap the model. It must say so and record it, and it must still run.
        session = self.seed_agent()
        self.seed_agent_model(session, "old-provider", "old-model", "high")
        code, out, err = self.cli(
            "agent",
            "reset",
            "myrepo-maker",
            "--timeout",
            "2",
            env=self.state_env(
                CLOWDER_FAKE_RESET_PROVIDER="new-provider",
                CLOWDER_FAKE_RESET_MODEL="new-model",
                CLOWDER_FAKE_RESET_THINKING="off",
            ),
        )
        self.assertEqual(code, 0, err)
        self.assertIn(
            "model: old-provider/old-model (thinking high) -> "
            "new-provider/new-model (thinking off)",
            out,
        )
        line = json.loads(
            (self.root / "state.json.audit").read_text(encoding="utf-8").splitlines()[-1]
        )
        self.assertEqual(line["action"], "agent-reset")
        self.assertEqual(line["agent"], "myrepo-maker")
        self.assertEqual(line["model_before"], "old-provider/old-model (thinking high)")
        self.assertEqual(line["model_after"], "new-provider/new-model (thinking off)")

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

    # -- agent reseed ------------------------------------------------------

    def test_agent_reseed_exits_exports_and_resumes(self) -> None:
        self.seed_agent()
        log = self.root / "calls.jsonl"
        code, out, err = self.cli(
            "agent",
            "reseed",
            "myrepo-maker",
            "--timeout",
            "30",
            env=self.state_env(CLOWDER_FAKE_LOG=str(log)),
        )
        self.assertEqual(code, 0, err)
        self.assertIn("reseeded", out)
        self.assertIn("clowder-bot", out)
        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

        quit_calls = [c for c in calls if c[:2] == ["agent", "send-keys"]]
        self.assertEqual(quit_calls[0][3:], ["/", "q", "u", "i", "t", "enter"])

        # The export runs only once Pi has left the roster, so the command cannot
        # race into Pi's editor.
        quit_at = next(i for i, c in enumerate(calls) if c[:2] == ["agent", "send-keys"])
        run_at = next(i for i, c in enumerate(calls) if c[:2] == ["pane", "run"])
        between = [tuple(c[:2]) for c in calls[quit_at:run_at]]
        self.assertIn(("agent", "list"), between, "wait for Pi to leave before the export")

        waits = [c for c in calls if c[:2] == ["pane", "wait-output"]]
        self.assertEqual(len(waits), 1, "wait for the export marker")
        self.assertIn("clowder-identity-seeded", waits[0])

        runs = [c for c in calls if c[:2] == ["pane", "run"]]
        self.assertEqual(len(runs), 1)
        export = runs[0][-1]
        for key in (
            "GIT_AUTHOR_NAME",
            "GIT_AUTHOR_EMAIL",
            "GIT_COMMITTER_NAME",
            "GIT_COMMITTER_EMAIL",
        ):
            self.assertIn(key, export)
        self.assertIn("clowder-identity-seeded", export)
        self.assertIn("$env:GIT_AUTHOR_NAME='clowder-bot'", export)
        self.assertIn("Write-Output 'clowder-identity-seeded'", export)

        starts = [c for c in calls if c[:2] == ["agent", "start"]]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0][-2:], ["--", "--continue"])
        self.assertEqual(len([c for c in calls if c[:2] == ["agent", "wait"]]), 1)

    def test_agent_reseed_refuses_while_a_step_is_unreported(self) -> None:
        job_id, _ = self.open_a_job()
        self.assertEqual(self.step("--job", job_id, "ship: add the refund page")[0], 0)
        code, _, err = self.cli(
            "agent", "reseed", "myrepo-maker", "--timeout", "2", env=self.state_env()
        )
        self.assertEqual(code, 1)
        self.assertIn("unreported step", err)
        self.assertIn("t-0001", err)

    def test_agent_reseed_resumes_the_session(self) -> None:
        self.seed_agent()
        log = self.root / "calls.jsonl"
        code, _, err = self.cli(
            "agent",
            "reseed",
            "myrepo-maker",
            "--timeout",
            "30",
            env=self.state_env(CLOWDER_FAKE_LOG=str(log)),
        )
        self.assertEqual(code, 0, err)
        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        start = next(c for c in calls if c[:2] == ["agent", "start"])
        self.assertEqual(start[-2:], ["--", "--continue"], "Pi resumes the same session")

    def test_agent_reseed_retries_while_the_pane_is_not_yet_a_shell(self) -> None:
        # The quit and the export have both worked, but the multiplexer has not
        # yet seen the pane as a shell. A single start would fail and leave the
        # pane down, so the start is retried until one lands.
        self.seed_agent()
        log = self.root / "calls.jsonl"
        code, out, err = self.cli(
            "agent",
            "reseed",
            "myrepo-maker",
            "--timeout",
            "30",
            env=self.state_env(CLOWDER_FAKE_LOG=str(log), CLOWDER_FAKE_START_BUSY="2"),
        )
        self.assertEqual(code, 0, err)
        self.assertIn("reseeded", out)
        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        starts = [c for c in calls if c[:2] == ["agent", "start"]]
        self.assertEqual(len(starts), 3, "two refusals, then the start that lands")
        for start in starts:
            self.assertEqual(start[-2:], ["--", "--continue"], "Pi resumes the session")
        self.assertEqual(len([c for c in calls if c[:2] == ["agent", "wait"]]), 1)

    def test_agent_reseed_names_the_manual_start_when_the_pane_never_settles(self) -> None:
        # A genuine timeout must say where the pane stands and print the exact
        # command that brings Pi back. The pane is left alone: no second quit.
        self.seed_agent()
        log = self.root / "calls.jsonl"
        code, out, err = self.cli(
            "agent",
            "reseed",
            "myrepo-maker",
            "--timeout",
            "3",
            env=self.state_env(CLOWDER_FAKE_LOG=str(log), CLOWDER_FAKE_START_BUSY="always"),
        )
        self.assertEqual(code, 2, err)
        self.assertIn("left at its shell", err)
        self.assertIn(
            "agent start myrepo-maker --kind pi --pane w9:p1 -- --continue",
            err,
            "the message names the manual start command",
        )
        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([c for c in calls if c[:2] == ["agent", "wait"]], [])
        self.assertEqual(
            len([c for c in calls if c[:2] == ["agent", "send-keys"]]),
            1,
            "the pane is not quit a second time",
        )
        self.assertEqual(
            len([c for c in calls if c[:2] == ["pane", "close"]]), 0, "the pane stays open"
        )

    def test_a_slow_start_that_fits_the_budget_lands(self) -> None:
        # A pane on this box can take 22 to 45s to come up. The first attempt must
        # get the whole --timeout budget: a cap of the restart helper's own would
        # cut that slow-but-working start short, which is the case the retry
        # exists to cover.
        mux = _SlowStartMux(needs_s=25.0)
        started = cli._start_agent_when_ready(mux, "myrepo-maker", "w9:p1", 60.0)
        self.assertTrue(started.ok, started.result.error_text())
        self.assertEqual(len(mux.timeouts), 1, "the first attempt lands; no retry is spent")
        self.assertFalse(started.clipped, "the only attempt had the whole budget")
        self.assertGreaterEqual(
            mux.timeouts[0], 25.0, "the whole budget reached the start, not a smaller cap"
        )

    def test_a_start_that_outlasts_the_whole_budget_fails_with_the_manual_command(self) -> None:
        # The budget bounds the start itself, not just the retries. A start still
        # running when the budget ends is a failure, and it says how to finish by
        # hand. Its own first attempt got the whole budget, so its own reason is
        # the real one and is quoted.
        self.seed_agent()
        starts = self.root / "starts.txt"
        code, _, err = self.cli(
            "agent",
            "reseed",
            "myrepo-maker",
            "--timeout",
            "3",
            env=self.state_env(
                CLOWDER_FAKE_START_COUNT=str(starts),
                CLOWDER_FAKE_START_DELAY="20",
            ),
        )
        self.assertEqual(code, 2, err)
        self.assertIn("left at its shell", err)
        self.assertIn(
            "agent start myrepo-maker --kind pi --pane w9:p1 -- --continue",
            err,
            "the message names the manual start command",
        )
        self.assertIn(
            "did not return within",
            err,
            "the first attempt had the whole budget, so its own reason is quoted",
        )
        self.assertNotIn("ran out of its", err, "nothing was clipped, so no budget excuse")
        self.assertEqual(self.start_count(starts), 1, "one attempt, cut at the budget")

        # The last attempt is clipped to what is left of the budget, so the whole
        # call finishes near it rather than running past it.
        timed = _SpendsItsTimeoutMux()
        spent_from = time.monotonic()
        result = cli._start_agent_when_ready(timed, "myrepo-maker", "w9:p1", 0.4)
        elapsed = time.monotonic() - spent_from
        self.assertFalse(result.ok)
        self.assertFalse(result.clipped, "the only attempt had the whole budget")
        self.assertEqual(len(timed.timeouts), 1, "one attempt, given all that is left")
        self.assertLessEqual(
            timed.timeouts[0], 0.4, "the attempt was given no more than the budget"
        )
        self.assertLess(elapsed, 2.0, "the call ended near the budget, not long after it")

        # The waits default to a slow start's worth, and the help says the number
        # is per wait, not for the whole command.
        parsed = cli.build_parser().parse_args(["agent", "reseed", "someone"])
        self.assertEqual(parsed.timeout, 60.0)
        help_text = self.reseed_help()
        self.assertIn("budget for each wait", help_text)
        for wait in ("quit", "export", "start", "ready"):
            self.assertIn(wait, help_text, f"the help names the {wait} wait")
        self.assertIn("four minutes", help_text, "the worst case is named")

    def test_a_clipped_final_attempt_names_the_whole_budget_not_a_sliver(self) -> None:
        # The reviewer's case: after retries, the final attempt is cut to the last
        # few milliseconds of the budget. Quoting that sliver as the reason would
        # tell the owner the start failed in 9ms, which never happened. The budget
        # that ran out is the fact, and it is named in full.
        # The start step is stubbed on purpose: a real clipped attempt needs two
        # fake mux calls inside one budget, and that depends on how loaded the box
        # is. Everything around it - the quit, the export, the message - is real.
        self.seed_agent()
        sliver = cli.RestartResult(
            result=MuxResult(
                argv=("mux", "agent", "start"),
                returncode=None,
                stdout="",
                stderr="",
                duration_ms=9,
                timed_out=True,
            ),
            attempts=3,
            budget_s=60.0,
        )
        self.assertEqual(cli._restart_reason(sliver), "the restart ran out of its 60s budget")
        with mock.patch.object(cli, "_start_agent_when_ready", return_value=sliver):
            code, _, err = self.cli(
                "agent", "reseed", "myrepo-maker", "--timeout", "60", env=self.state_env()
            )
        self.assertEqual(code, 2, err)
        self.assertIn("ran out of its 60s budget", err)
        self.assertNotIn(
            "did not return within", err, "a clipped sliver is not quoted as the reason"
        )
        self.assertIn("left at its shell", err)
        self.assertIn("agent start myrepo-maker --kind pi --pane w9:p1 -- --continue", err)

        # And the clipping itself, which is what makes the sliver above possible:
        # the second attempt gets less than the first, so the budget is going.
        mux = _RefuseThenTimeoutMux()
        restart = cli._start_agent_when_ready(mux, "myrepo-maker", "w9:p1", 0.6)
        self.assertFalse(restart.ok)
        self.assertTrue(restart.clipped, "a second attempt is clipped by the budget")
        self.assertEqual(restart.attempts, 2)
        self.assertLess(
            mux.timeouts[1], mux.timeouts[0], "the second attempt got what was left"
        )
        self.assertEqual(cli._restart_reason(restart), "the restart ran out of its 0.6s budget")

    def test_a_different_failure_after_a_refusal_keeps_its_own_reason(self) -> None:
        # The reviewer's probe: the first attempt is refused, the retry fails with
        # agent_not_ready. The budget did not stop that start - the agent blocked
        # during startup did - so the message must name that failure and must not
        # claim the budget ran out.
        self.seed_agent()
        blocked = _start_error("agent_not_ready", "the agent blocked during startup")
        mux = _ThenFailsMux(blocked)
        restart = cli._start_agent_when_ready(mux, "myrepo-maker", "w9:p1", 30.0)
        self.assertFalse(restart.ok)
        self.assertTrue(restart.clipped, "a second attempt did not get the whole budget")
        self.assertEqual(restart.attempts, 2)
        reason = cli._restart_reason(restart)
        self.assertIn("agent_not_ready: the agent blocked during startup", reason)
        self.assertNotIn("ran out of its", reason, "the budget is not what stopped it")

        # And through the real command path, so the owner-facing sentence is the
        # one asserted, not just the reason fragment.
        with mock.patch.object(cli, "_start_agent_when_ready", return_value=restart):
            code, _, err = self.cli(
                "agent", "reseed", "myrepo-maker", "--timeout", "30", env=self.state_env()
            )
        self.assertEqual(code, 2, err)
        self.assertIn("agent_not_ready: the agent blocked during startup", err)
        self.assertNotIn("ran out of its", err)
        self.assertIn("left at its shell", err)
        self.assertIn("agent start myrepo-maker --kind pi --pane w9:p1 -- --continue", err)

    def test_a_different_start_failure_is_not_retried(self) -> None:
        # Only the not-yet-a-shell refusal is worth waiting out. A start that
        # fails for another reason is the answer, so it is sent once and reported
        # with its own reason.
        self.seed_agent()
        log = self.root / "calls.jsonl"
        code, _, err = self.cli(
            "agent",
            "reseed",
            "myrepo-maker",
            "--timeout",
            "30",
            env=self.state_env(CLOWDER_FAKE_LOG=str(log), CLOWDER_FAKE_START_FAIL="1"),
        )
        self.assertEqual(code, 2, err)
        self.assertIn("agent_not_ready: the agent blocked during startup", err)
        self.assertNotIn("ran out of its", err, "a real reason is not replaced by the budget")
        self.assertIn("agent start myrepo-maker --kind pi --pane w9:p1 -- --continue", err)
        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(
            len([c for c in calls if c[:2] == ["agent", "start"]]),
            1,
            "a different failure is not retried",
        )
        self.assertEqual([c for c in calls if c[:2] == ["agent", "wait"]], [])

    def test_the_reseed_export_matches_the_shell(self) -> None:
        # The pane's shell decides the syntax: PowerShell uses $env:, POSIX uses
        # export. Both print the marker so the wait can confirm the command ran.
        identity = gitcmd.Identity("clowder-bot", "94532220+lacattano@users.noreply.github.com")
        pwsh = cli._reseed_export(identity, "pwsh.exe")
        self.assertIn("$env:GIT_AUTHOR_NAME='clowder-bot'", pwsh)
        self.assertIn("Write-Output 'clowder-identity-seeded'", pwsh)
        bash = cli._reseed_export(identity, "bash.exe")
        self.assertIn("export GIT_AUTHOR_NAME=clowder-bot", bash)
        self.assertIn("echo clowder-identity-seeded", bash)

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

    def advance_origin(self, origin: Path) -> None:
        """Add one normal commit to origin/main, so a branch becomes behind."""
        other = self.root / "other"
        gitcmd.run_git(self.root, "clone", str(origin), str(other))
        gitcmd.run_git(other, "config", "user.email", "test@example.com")
        gitcmd.run_git(other, "config", "user.name", "Test")
        (other / "later.txt").write_text("x\n", encoding="utf-8")
        gitcmd.run_git(other, "add", "later.txt")
        gitcmd.run_git(other, "commit", "-m", "later")
        gitcmd.run_git(other, "push", "origin", "main")

    def advance_origin_editing(self, origin: Path, name: str, text: str) -> None:
        """Advance origin/main by adding or editing a file, to force a conflict."""
        other = self.root / "conflict-clone"
        gitcmd.run_git(self.root, "clone", str(origin), str(other))
        gitcmd.run_git(other, "config", "user.email", "test@example.com")
        gitcmd.run_git(other, "config", "user.name", "Test")
        (other / name).write_text(text, encoding="utf-8")
        gitcmd.run_git(other, "add", name)
        gitcmd.run_git(other, "commit", "-m", f"conflicting {name}")
        gitcmd.run_git(other, "push", "origin", "main")

    def rewrite_origin(self, origin: Path) -> None:
        """Replace origin/main with a fresh history that shares no commit."""
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

    def test_a_stale_local_base_does_not_block_a_job(self) -> None:
        # The main checkout is one commit behind origin/main. The job forks from
        # origin/main, so the stale local branch does not block it, and the branch
        # does not carry old code.
        self.add_origin()
        self.seed_mux([])
        code, out, err = self.open_job()
        self.assertEqual(code, 0, err)
        self.assertIn("open", out)

        worktree = self.repo_path / ".worktrees" / "myrepo-maker"
        origin_tip = gitcmd.commit_of(self.repo_path, "origin/main", short=False)
        local_tip = gitcmd.commit_of(self.repo_path, "main", short=False)
        self.assertNotEqual(local_tip, origin_tip, "the local base really is stale")
        self.assertEqual(
            gitcmd.commit_of(worktree, "task/refund", short=False),
            origin_tip,
            "the branch forks from origin/main, not the stale local main",
        )
        job = next(iter(json.loads(self.state.read_text(encoding="utf-8"))["jobs"].values()))
        self.assertEqual(job["base"], "main")
        self.assertEqual(job["base_commit"], origin_tip, "the recorded base is origin/main")

    def test_a_failed_fetch_still_forks_from_origin(self) -> None:
        # The reviewer (t-0544) found that a failed fetch skipped the origin fork
        # and fell back to the stale local base. `origin/main` is used whenever it
        # exists locally, with a warning that it may itself be stale.
        self.add_origin()
        # Refresh origin/main once, so it is ahead of the stale local main.
        gitcmd.fetch(self.repo_path)
        origin_tip = gitcmd.commit_of(self.repo_path, "origin/main", short=False)
        local_tip = gitcmd.commit_of(self.repo_path, "main", short=False)
        self.assertNotEqual(local_tip, origin_tip, "the local base really is stale")
        # The remote is now unreachable, so the fetch inside `job open` fails.
        gitcmd.run_git(
            self.repo_path, "remote", "set-url", "origin", str(self.root / "gone.git")
        )
        self.seed_mux([])
        code, out, err = self.open_job()
        self.assertEqual(code, 0, err)
        worktree = self.repo_path / ".worktrees" / "myrepo-maker"
        self.assertEqual(
            gitcmd.commit_of(worktree, "task/refund", short=False),
            origin_tip,
            "the failed fetch did not fall back to the stale local base",
        )
        job = next(iter(json.loads(self.state.read_text(encoding="utf-8"))["jobs"].values()))
        self.assertEqual(job["base_commit"], origin_tip)
        # The base could not be refreshed, so the reader is told it may be stale.
        self.assertIn("could not fetch", err)
        self.assertIn("may be stale", err)

    def test_job_open_refuses_a_rewritten_base(self) -> None:
        origin = self.add_origin_at_head()
        self.rewrite_origin(origin)
        self.seed_mux([])
        code, _, err = self.open_job()
        self.assertEqual(code, 2)
        self.assertIn("rewritten", err)

    def test_job_open_with_force_ignores_a_rewritten_base(self) -> None:
        origin = self.add_origin_at_head()
        self.rewrite_origin(origin)
        self.seed_mux([])
        code, out, err = self.open_job("--force")
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
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
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

    # -- closing a dead step, keeping its answer ---------------------------

    def close_the_job(self, job_id: str) -> None:
        """Close a job while a step on it is still open.

        This is the stale state the close path exists for: 79 open steps sit on
        jobs that are closed or that never existed. `--force` is how the record
        got that way.
        """
        code, _, err = self.cli("job", "close", job_id, "--force", env=self.state_env())
        self.assertEqual(code, 0, err)

    def keep_an_answer(self, text: str = "the refund page is missing") -> None:
        """Put an answer on the open step's record.

        Written through the store on purpose: `report` marks a step reported, and a
        reported step is not open, so it could not be closed.
        """
        store = StateStore(self.state)
        task = store.get("t-0001")
        task.answer = text
        store.save()

    def test_step_close_refuses_without_a_why(self) -> None:
        job_id, _ = self.open_a_job()
        self.step("--job", job_id, "ship: add the refund page")
        self.close_the_job(job_id)
        code, _, err = self.cli("step", "close", "t-0001", env=self.state_env())
        self.assertEqual(code, 1)
        self.assertIn("--why", err)
        self.assertEqual(self.only_task()["status"], "dispatched")

    def test_step_close_keeps_the_answer_text_and_the_record(self) -> None:
        job_id, _ = self.open_a_job()
        self.step("--job", job_id, "ship: add the refund page")
        self.keep_an_answer()
        self.close_the_job(job_id)

        code, out, err = self.cli(
            "step",
            "close",
            "t-0001",
            "--why",
            "the job is long done",
            "--by",
            "topcat",
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        self.assertIn("t-0001 closed: the job is long done", out)

        task = self.only_task()
        self.assertEqual(task["status"], "closed")
        self.assertEqual(task["answer"], "the refund page is missing", "the answer stays")
        self.assertEqual(task["close_reason"], "the job is long done")
        self.assertTrue(task["closed_at"])
        self.assertEqual(len(self.load_state()["tasks"]), 1, "the task record is kept")

    def test_report_shows_a_closed_steps_answer(self) -> None:
        # An abandoned step reads as if no answer will come. A closed step keeps
        # the answer it has, which is the whole point of the second exit.
        job_id, _ = self.open_a_job()
        self.step("--job", job_id, "ship: add the refund page")
        self.keep_an_answer()
        self.close_the_job(job_id)
        self.cli(
            "step", "close", "t-0001", "--why", "the job is long done", env=self.state_env()
        )

        code, out, err = self.cli("report", "t-0001", env=self.state_env())
        self.assertEqual(code, 0, err)
        self.assertIn("the refund page is missing", out)

    def test_step_close_refuses_a_step_on_an_open_job(self) -> None:
        # The safety rule: never close a step whose job is still in flight.
        job_id, _ = self.open_a_job()
        self.step("--job", job_id, "ship: add the refund page")
        code, _, err = self.cli(
            "step", "close", "t-0001", "--why", "tidy up", env=self.state_env()
        )
        self.assertEqual(code, 1)
        self.assertIn(job_id, err)
        self.assertIn("still open", err)
        self.assertEqual(self.only_task()["status"], "dispatched")

    def test_job_open_records_a_title_and_an_effect(self) -> None:
        # Named in the owner's words at open time, with the label as the fallback.
        job_id, _ = self.open_a_job(
            label="page-context",
            extra=("--title", "the page-context fix", "--effect", "a page opens in context"),
        )
        store = StateStore(self.state)
        job = store.get_job(job_id)
        self.assertEqual(job.title, "the page-context fix")
        self.assertEqual(job.effect, "a page opens in context")

    def test_job_open_records_labels(self) -> None:
        # `job open` has no free `--label` (it names the branch), so its reserved
        # tag families come in with a repeatable `--tag`.
        job_id, _ = self.open_a_job(
            label="page-context",
            extra=("--tag", "sev:blocker", "--tag", "phase:pre-launch"),
        )
        job = StateStore(self.state).get_job(job_id)
        self.assertEqual(job.labels, ["sev:blocker", "phase:pre-launch"])

    def test_job_open_falls_back_to_the_label_when_no_title(self) -> None:
        job_id, _ = self.open_a_job(label="page-context")
        job = StateStore(self.state).get_job(job_id)
        self.assertEqual(job.title, "page-context")
        self.assertEqual(job.name_in_words, "page-context")
        self.assertIsNone(job.effect)
        self.assertEqual(job.effect_in_words, job.branch)

    def test_a_report_names_the_change_in_words(self) -> None:
        job_id, _ = self.open_a_job(
            label="page-context",
            extra=(
                "--title",
                "the page-context fix",
            ),
        )
        self.assertEqual(self.step("--job", job_id, "ship: add the refund page")[0], 0)
        self.session_turns()
        code, out, err = self.cli("report", "t-0001", env=self.state_env())
        self.assertEqual(code, 0, err)
        self.assertIn("the page-context fix", out, "the change is named first, in words")

    def test_step_close_frees_the_agent_for_a_reset_and_a_rename(self) -> None:
        job_id, _ = self.open_a_job()
        # A step with no job of its own: the other half of the stale record, and
        # the case where the step alone is what blocks the agent. A scout, since a
        # ship now has to name a place. The job is closed first: an open job in the
        # space refuses the send.
        self.assertEqual(self.cli("job", "close", job_id, env=self.state_env())[0], 0)
        self.assertEqual(self.step("--shape", "scout", "scout: add the refund page")[0], 0)

        # While the step reads as open, both are refused for that agent.
        code, _, err = self.cli(
            "agent", "reset", "myrepo-maker", "--timeout", "2", env=self.state_env()
        )
        self.assertEqual(code, 1)
        self.assertIn("unreported step", err)
        code, _, err = self.cli(
            "agent", "rename", "myrepo-maker", "--to", "myrepo-maker-1", env=self.state_env()
        )
        self.assertEqual(code, 1)
        self.assertIn("open step", err)

        code, _, err = self.cli(
            "step", "close", "t-0001", "--why", "the pane died", env=self.state_env()
        )
        self.assertEqual(code, 0, err)

        # The reset goes through now, and the rename is no longer refused for it.
        code, out, err = self.cli(
            "agent", "reset", "myrepo-maker", "--timeout", "2", env=self.state_env()
        )
        self.assertEqual(code, 0, err)
        self.assertIn("reset: new session", out)
        _, _, err = self.cli(
            "agent", "rename", "myrepo-maker", "--to", "myrepo-maker-1", env=self.state_env()
        )
        self.assertNotIn("open step", err)
        self.assertNotIn("open job", err)

    def test_step_close_writes_an_audit_line(self) -> None:
        job_id, _ = self.open_a_job()
        self.step("--job", job_id, "ship: add the refund page")
        self.close_the_job(job_id)
        self.cli(
            "step",
            "close",
            "t-0001",
            "--why",
            "the job is long done",
            "--by",
            "topcat",
            env=self.state_env(),
        )
        audit_file = self.root / "state.json.audit"
        self.assertTrue(audit_file.is_file())
        line = json.loads(audit_file.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(line["action"], "step-close")
        self.assertEqual(line["step"], "t-0001")
        self.assertEqual(line["job"], job_id)
        self.assertEqual(line["reason"], "the job is long done")
        self.assertEqual(line["by"], "topcat")
        self.assertFalse(line["answer_kept"], "no answer was on the record")

    def test_a_closed_step_cannot_be_closed_again(self) -> None:
        job_id, _ = self.open_a_job()
        self.step("--job", job_id, "ship: add the refund page")
        self.close_the_job(job_id)
        self.cli("step", "close", "t-0001", "--why", "done", env=self.state_env())
        code, _, err = self.cli(
            "step", "close", "t-0001", "--why", "again", env=self.state_env()
        )
        self.assertEqual(code, 1)
        self.assertIn("already closed", err)

    def test_tasks_counts_a_closed_step(self) -> None:
        job_id, _ = self.open_a_job()
        self.step("--job", job_id, "ship: add the refund page")
        self.close_the_job(job_id)
        self.cli("step", "close", "t-0001", "--why", "done", env=self.state_env())
        code, out, err = self.cli("tasks", env=self.state_env())
        self.assertEqual(code, 0, err)
        self.assertIn("1 closed", out)
        self.assertIn("0 still open", out)

    # -- a step closes itself when it reports (2026-10-24) -------------------

    def test_a_step_that_reports_is_closed_without_any_read(self) -> None:
        # The owner's complaint, in full: a finished step stayed `dispatched`, the
        # one-open-step guard then refused the next step, and the only way past was
        # to read the report for its side effect - nine times in one day. The cheap
        # check the front door already runs closes it instead.
        job_id, _ = self.open_a_job()
        self.assertEqual(self.step("--job", job_id, "ship: add the refund page")[0], 0)
        self.answer_the_step()

        code, out, err = self.cli("inbox", env=self.state_env())
        self.assertEqual(code, 0, err)
        self.assertIn("t-0001", out, "the step is listed as having reported")

        task = StateStore(self.state).get("t-0001")
        self.assertEqual(task.status, CLOSED, "reported, and closed without a read")
        self.assertEqual(task.answer, "the refund page is missing", "the answer is kept")
        self.assertIsNotNone(task.usage, "the usage window is kept")
        self.assertIsNotNone(task.reported_at)
        self.assertEqual(task.close_reason, "the step reported")

        # And the guard frees itself: the next step on the same job goes out.
        code, out, err = self.step("--job", job_id, "ship: add the refund terms")
        self.assertEqual(code, 0, err)
        self.assertIn("t-0002 sent", out)

        # A read afterwards still shows the answer and the usage, and does not
        # reopen the step it closed.
        code, out, err = self.cli("report", "t-0001", env=self.state_env())
        self.assertEqual(code, 0, err)
        self.assertIn("the refund page is missing", out)
        self.assertEqual(StateStore(self.state).get("t-0001").status, CLOSED)

    def test_an_unreadable_roster_closes_nothing(self) -> None:
        # The roster is the evidence that the worker has stopped. A list that could
        # not be read proves nothing, so a step is never closed on it - an open step
        # stays open, and the guard stays up, rather than closing work in flight.
        job_id, _ = self.open_a_job()
        self.assertEqual(self.step("--job", job_id, "ship: add the refund page")[0], 0)
        self.answer_the_step()
        env = self.state_env(CLOWDER_FAKE_LIST_FAIL="1")
        code, _, err = self.cli("inbox", env=env)
        self.assertEqual(code, 0, err)
        self.assertEqual(StateStore(self.state).get("t-0001").status, DISPATCHED)

    def test_catchup_shows_only_what_is_new_and_then_says_nothing(self) -> None:
        code, _, err = self.cli(
            "queue",
            "add",
            "myrepo",
            "scout: read the terms page",
            "--agent",
            "maker",
            "--why",
            "the space holds an open job",
            env=self.fake_env(),
        )
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("catchup", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("Queued next:", out)
        self.assertIn("not sent yet: the space holds an open job", out)
        self.assertIn("First read", out, "the first read says what it is")
        self.assertIn("last 7 days", out)
        # Read once: the next run has nothing new.
        code, out, err = self.cli("catchup", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("Nothing new", out)
        self.assertNotIn("Queued next:", out)
        # The standing picture shows it again on demand.
        code, out, err = self.cli("catchup", "--all", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("Queued next:", out)
        self.assertIn("read the terms page", out)

    def test_catchup_names_the_word_that_answers_him(self) -> None:
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        code, _, err = self.cli(
            "owner",
            "t-0001",
            "--item",
            "Which page should the refund link go on?",
            env=self.fake_env(),
        )
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("catchup", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("Waiting on you:", out)
        self.assertIn("Which page should the refund link go on? - type 1", out)

    def test_a_landing_appears_after_a_read_with_no_refresh(self) -> None:
        code, out, err = self.cli("catchup", env=self.fake_env())
        self.assertEqual(code, 0, err)
        job_id, _ = self.open_a_job("evidence")
        self.merge_word(job_id)
        with mock.patch.object(cli, "_merge_pull_request"):
            code, _, err = self.cli("job", "merge", job_id, "--pr", "87", env=self.state_env())
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("catchup", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("What landed:", out)
        self.assertIn("Now live: evidence", out)
        self.assertNotIn("never", out, "not the first read any more")

    def test_catchup_json_marks_nothing_read(self) -> None:
        # A machine read is not the owner reading. The JSON view reports and marks
        # nothing, so a relay cannot eat the line before he sees it.
        self.cli(
            "queue",
            "add",
            "myrepo",
            "scout: x",
            "--agent",
            "maker",
            "--why",
            "waiting",
            env=self.fake_env(),
        )
        code, out, _ = self.cli("catchup", "--json", env=self.fake_env())
        payload = json.loads(out)
        self.assertEqual(payload["new"], 1)
        self.assertTrue(payload["items"][0]["new"])
        self.assertEqual(payload["items"][0]["kind"], "queued")
        code, out, _ = self.cli("catchup", "--json", env=self.fake_env())
        self.assertEqual(json.loads(out)["new"], 1, "still unread after a machine read")
        # The human run marks it read, and then the machine read agrees.
        code, out, err = self.cli("catchup", env=self.fake_env())
        self.assertIn("First read", out)
        self.assertIn("1 thing(s)", out)
        code, out, _ = self.cli("catchup", "--json", env=self.fake_env())
        payload = json.loads(out)
        self.assertEqual(payload["new"], 0)
        self.assertIn("last_read", payload)

    def test_a_risk_that_returns_is_shown_again(self) -> None:
        # The reviewer's case: read the risk, the job link hides it, the job closes
        # without merging and it comes back with the same last_seen. It must be news.
        for _ in range(2):
            self.cli(
                "defects",
                "add",
                "--line",
                "a bundle carries a machine name",
                env=self.fake_env(),
            )
        code, out, err = self.cli("catchup", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("Still happening: a bundle carries a machine name", out)

        job_id, _ = self.open_a_job("evidence", extra=("--defect", "d-0001"))
        # While the job holds it, it is not a risk. Checked without a catch-up run, so
        # nothing between the two reads can hide the returning line.
        code, out, _ = self.cli("defects", "list", "--repeated", env=self.fake_env())
        self.assertNotIn("d-0001", out, "in flight, so not a risk")

        code, _, err = self.cli("job", "close", job_id, env=self.state_env())
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("catchup", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("Still happening: a bundle carries a machine name", out)

    def test_the_running_line_is_in_his_nouns(self) -> None:
        self.dispatch(
            "maker",
            "myrepo",
            "ship (t-1016): make the board tell him what changed",
            "--worktree",
            str(self.repo_path),
        )
        code, out, err = self.cli("catchup", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("make the board tell him what changed", out)
        self.assertNotIn("t-1016", out, "no handle in a line he reads")
        self.assertNotIn("ship (", out)

    def test_a_first_read_is_bounded_and_says_so(self) -> None:
        # The owner's own state listed 244 past landings on the first read. A first
        # read is the last seven days, and it says that.
        store = StateStore(self.state)
        recent = Job(
            id="j-0001",
            label="recent",
            repo="myrepo",
            repo_path=str(self.repo_path),
            worktree=str(self.repo_path),
            branch="task/recent",
            base="main",
            agent="maker",
            title="the refund page",
            effect="a customer can read the policy before paying",
            merged_at=now_iso(),
            status=CLOSED,
        )
        store.add_job(recent)
        store.add_job(
            Job(
                id="j-0002",
                label="ancient",
                repo="myrepo",
                repo_path=str(self.repo_path),
                worktree=str(self.repo_path),
                branch="task/ancient",
                base="main",
                agent="maker",
                title="the old page",
                merged_at="2026-01-01T09:00:00Z",
                status=CLOSED,
            )
        )
        store.save()

        code, out, err = self.cli("catchup", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("First read", out)
        self.assertIn("last 7 days", out)
        self.assertIn("Now live: a customer can read the policy before paying", out)
        self.assertNotIn(
            "Now live: the old page", out, "history older than a week is not listed"
        )
        self.assertIn("landings are the last 7 days", out)
        self.assertIn("the rest stands today", out)
        self.assertIn("1 older landing(s) are not listed", out)
        # The history it did not list is marked read, so it cannot flood the next read.
        code, out, err = self.cli("catchup", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("Nothing new", out)
        self.assertNotIn("Now live: the old page", out)
        # --all still shows the whole standing picture.
        code, out, err = self.cli("catchup", "--all", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("Now live: the old page", out)

    def test_a_first_read_shows_an_old_risk(self) -> None:
        store = StateStore(self.state)
        old = Task(
            id="t-0001",
            question="q",
            brief="b",
            shape="scout",
            agent="maker",
            repo="myrepo",
            repo_path=str(self.repo_path),
        )
        store.add(old)
        store.save()
        code, _, err = self.cli(
            "defects",
            "add",
            "--line",
            "a bundle carries a machine name",
            "--first-seen",
            "2026-09-01",
            "--last-seen",
            "2026-09-02",
            "--count",
            "3",
            "--status",
            "open",
            env=self.fake_env(),
        )
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("catchup", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("Still happening: a bundle carries a machine name", out)

    def test_all_on_a_first_read_says_everything_not_a_week(self) -> None:
        job_id, _ = self.open_a_job("arrival-keys")
        self.merge_word(job_id)
        with mock.patch.object(cli, "_merge_pull_request"):
            code, _, err = self.cli("job", "merge", job_id, "--pr", "87", env=self.state_env())
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("catchup", "--all", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("everything on record", out)
        self.assertNotIn("last 7 days", out)

    def test_a_landed_line_uses_the_title_not_the_label(self) -> None:
        job_id, _ = self.open_a_job(
            "arrival-keys", extra=("--title", "the arrival keys are checked first")
        )
        self.merge_word(job_id)
        with mock.patch.object(cli, "_merge_pull_request"):
            code, _, err = self.cli("job", "merge", job_id, "--pr", "87", env=self.state_env())
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("catchup", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("Now live: the arrival keys are checked first", out)
        self.assertNotIn("Now live: arrival-keys", out)

    def test_a_job_with_no_title_lands_with_its_label(self) -> None:
        job_id, _ = self.open_a_job("arrival-keys")
        self.merge_word(job_id)
        with mock.patch.object(cli, "_merge_pull_request"):
            code, _, err = self.cli("job", "merge", job_id, "--pr", "87", env=self.state_env())
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("catchup", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("Now live: arrival-keys", out)

    def test_a_later_read_is_unchanged_by_the_first_read_bound(self) -> None:
        code, _, err = self.cli("catchup", env=self.fake_env())  # the first read
        self.assertEqual(code, 0, err)
        job_id, _ = self.open_a_job(
            "arrival-keys", extra=("--title", "the arrival keys are checked first")
        )
        self.merge_word(job_id)
        with mock.patch.object(cli, "_merge_pull_request"):
            code, _, err = self.cli("job", "merge", job_id, "--pr", "87", env=self.state_env())
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("catchup", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertNotIn("First read", out, "a later read is a normal read")
        self.assertIn("Now live: the arrival keys are checked first", out)

    def test_the_board_opens_with_the_catch_up_text(self) -> None:
        self.cli(
            "queue",
            "add",
            "myrepo",
            "scout: read the terms page",
            "--agent",
            "maker",
            "--why",
            "busy",
            env=self.fake_env(),
        )
        code, _, err = self.cli("board", env=self.fake_env())
        self.assertEqual(code, 0, err)
        html = (self.root / "board.html").read_text(encoding="utf-8")
        self.assertLess(html.index("Since you last looked"), html.index("For you"))
        self.assertIn("not sent yet: busy", html)
        # Drawing the page does not mark his read: only catchup does.
        code, out, _ = self.cli("catchup", env=self.fake_env())
        self.assertIn("First read", out)
        self.assertIn("not sent yet: busy", out)

    def test_a_step_report_without_the_owner_line_is_refused(self) -> None:
        job_id, _ = self.open_a_job()
        self.assertEqual(self.step("--job", job_id, "ship: add the refund page")[0], 0)
        body = self.root / "answer.md"
        body.write_text("Re: the refund page\nThe terms page is empty.\n", encoding="utf-8")
        code, _, err = self.cli(
            "step",
            "report",
            "t-0001",
            "--headline",
            "the refund page is missing",
            "--answer-file",
            str(body),
            env=self.state_env(),
        )
        self.assertEqual(code, 1)
        self.assertIn("For the owner:", err)
        self.assertIn("--force", err)
        self.assertEqual(
            StateStore(self.state).get("t-0001").status, DISPATCHED, "nothing closed"
        )
        # Forcing it closes, and the record says the line was missing.
        code, _, err = self.cli(
            "step",
            "report",
            "t-0001",
            "--headline",
            "the refund page is missing",
            "--answer-file",
            str(body),
            "--force",
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(StateStore(self.state).get("t-0001").status, CLOSED)

    def test_a_step_reported_twice_closes_once(self) -> None:
        # Both intake paths, twice each. `step report` is the worker writing the
        # record itself; `inbox` is the front door's cheap check. Neither may write
        # a second close, a second audit line, or a new timestamp.
        job_id, _ = self.open_a_job()
        self.assertEqual(self.step("--job", job_id, "ship: add the refund page")[0], 0)
        body = self.root / "answer.md"
        body.write_text(
            "Re: the refund page\nThe terms page is empty.\n"
            "For the owner: The refund page is live now. It shows the policy.\n",
            encoding="utf-8",
        )

        code, _, err = self.cli(
            "step",
            "report",
            "t-0001",
            "--headline",
            "the refund page is missing",
            "--answer-file",
            str(body),
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        first = StateStore(self.state).get("t-0001")
        self.assertEqual(first.status, CLOSED)
        self.assertIn("The terms page is empty.", first.answer or "", "the file is the body")

        code, out, err = self.cli(
            "step", "report", "t-0001", "--headline", "again", env=self.state_env()
        )
        self.assertEqual(code, 0, err)
        self.assertIn("already", out)
        self.assertEqual(self.cli("inbox", env=self.state_env())[0], 0)

        after = StateStore(self.state).get("t-0001")
        self.assertEqual(after.closed_at, first.closed_at, "closed once")
        self.assertEqual(after.answer, first.answer, "the first report stands")
        lines = (self.root / "state.json.audit").read_text(encoding="utf-8").splitlines()
        reports = [json.loads(line) for line in lines if '"step-report"' in line]
        self.assertEqual(len(reports), 1, "one line for one close")
        self.assertEqual(reports[0]["step"], "t-0001")
        self.assertTrue(reports[0]["answer_kept"])

    def test_a_step_report_needs_one_line_and_cannot_revive_an_abandoned_step(self) -> None:
        job_id, _ = self.open_a_job()
        self.assertEqual(self.step("--job", job_id, "ship: add the refund page")[0], 0)
        code, _, err = self.cli(
            "step", "report", "t-0001", "--headline", "one\ntwo", env=self.state_env()
        )
        self.assertEqual(code, 1)
        self.assertIn("--answer-file", err)
        code, _, _ = self.cli(
            "step", "abandon", "t-0001", "--why", "pane died", env=self.state_env()
        )
        self.assertEqual(code, 0)
        code, _, err = self.cli(
            "step", "report", "t-0001", "--headline", "done", env=self.state_env()
        )
        self.assertEqual(code, 1)
        self.assertIn("abandoned", err)
        self.assertEqual(StateStore(self.state).get("t-0001").status, ABANDONED)

    # -- one line on the board, detail behind it (2026-10-24) ----------------

    def test_a_long_or_multi_line_owner_item_is_refused_with_the_detail_advice(self) -> None:
        # The board "shows a massive wall of text", and it got there one item at a
        # time: the item was the only field, so everything went into it. The line is
        # now enforced at the place it is recorded, and the body has a field.
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        too_long = "read every word of this: " + "x" * cli.OWNER_ITEM_LIMIT
        code, _, err = self.cli("owner", "t-0001", "--item", too_long, env=self.fake_env())
        self.assertEqual(code, 1)
        self.assertIn("--detail", err)
        self.assertIsNone(self.only_task()["owner_item"], "nothing was recorded")

        code, _, err = self.cli(
            "owner", "t-0001", "--item", "choose A or B\nand here is why", env=self.fake_env()
        )
        self.assertEqual(code, 1)
        self.assertIn("--detail", err)
        self.assertIsNone(self.only_task()["owner_item"])

        # The heading and the body are accepted together.
        code, _, err = self.cli(
            "owner",
            "t-0001",
            "--item",
            "choose A or B",
            "--detail",
            "A: 14 days, costs X.\nB: 30 days, costs Y.\nRecommend A.",
            env=self.fake_env(),
        )
        self.assertEqual(code, 0, err)
        task = self.only_task()
        self.assertEqual(task["owner_item"], "choose A or B")
        self.assertIn("Recommend A.", task["owner_detail"])

    def test_the_board_shows_an_item_line_with_its_detail_under_it(self) -> None:
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        self.cli(
            "owner",
            "t-0001",
            "--item",
            "choose A or B",
            "--detail",
            "A: 14 days, costs X.\nB: 30 days, costs Y.\nRecommend A.",
            env=self.fake_env(),
        )
        html = (self.root / "board.html").read_text(encoding="utf-8")
        heading = html.index("owner-title'>choose A or B")
        body = html.index("owner-detail'>A: 14 days")
        self.assertLess(heading, body, "the item is the heading, the detail is under it")
        self.assertIn("costs X.\nB: 30 days, costs Y.", html, "the detail keeps its lines")

    def test_a_merge_decision_named_by_an_owner_item_is_shown_once(self) -> None:
        # A job in "awaiting merge" and an owner item saying the same thing were two
        # lines in his section: one decision, listed twice. The item is the line he
        # reads and answers, so the derived line steps aside.
        passed = Job(
            id="j-0001",
            label="refund",
            repo="myrepo",
            repo_path=str(self.repo_path),
            worktree=str(self.repo_path),
            branch="task/refund",
            base="main",
            agent="maker",
            pass_at=now_iso(),
            published_at=now_iso(),
        )
        item = Task(
            id="t-0001",
            question="does the site need a refund policy?",
            brief="ship: add the refund page",
            shape="ship",
            agent="maker",
            repo="myrepo",
            repo_path=str(self.repo_path),
            job="j-0001",
            status=REPORTED,
            owner_item="merge the refund change",
            owner_item_at=now_iso(),
        )
        waiting = waiting_on_you(
            BoardData(
                tasks=[item],
                jobs=[passed],
                agents=[],
                state_path=str(self.state),
                generated_at="2026-10-25 12:00",
            )
        )
        self.assertEqual(len(waiting), 1, "one decision, one line")
        self.assertIn("merge the refund change", waiting[0])
        self.assertNotIn("your word", waiting[0])

    # -- a job that awaits a merge word frees its space (2026-10-24) --------

    def publish_a_pinned_job(self) -> tuple[str, Path]:
        """A passed, published job whose writer's space still holds its branch.

        `job pin` is the path that leaves the branch in the writer's space: the
        reviewer holds the save in its own copy, and nothing releases the writer's
        checkout. That is the job that used to hold the space while it waited.
        """
        job_id, maker_folder = self.open_a_job()
        saved = self.save_as_crew(maker_folder)
        code, _, err = self.cli(
            "job",
            "pin",
            job_id,
            "--to",
            "verifier",
            "--name",
            "myrepo-verifier",
            "--commit",
            saved,
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        self.owner_pass(job_id)
        with mock.patch.object(cli, "_publish_branch"):
            code, _, err = self.cli("job", "publish", job_id, env=self.state_env())
        self.assertEqual(code, 0, err)
        return job_id, maker_folder

    def test_a_published_job_can_be_parked_and_its_space_takes_a_new_job(self) -> None:
        job_id, maker_folder = self.publish_a_pinned_job()

        # Today's refusal, kept as the record of the problem: the space is held.
        code, _, err = self.cli(
            "job",
            "open",
            "myrepo",
            "--label",
            "terms",
            "--role",
            "maker",
            env=self.state_env(),
        )
        self.assertEqual(code, 2)
        self.assertIn("still open on this worktree", err)

        code, out, err = self.cli("job", "park", job_id, env=self.state_env())
        self.assertEqual(code, 0, err)
        self.assertIn("parked", out)
        parked = StateStore(self.state).get_job(job_id)
        self.assertEqual(parked.park_commit, parked.commit)
        self.assertTrue(parked.is_parked)
        self.assertIsNotNone(parked.released_at)
        self.assertEqual(
            parked.state_in_words, "waiting on your merge word", "his gate is unchanged"
        )
        self.assertIsNone(gitcmd.current_branch(maker_folder), "the checkout is released")
        self.assertIsNotNone(
            gitcmd.commit_of(self.repo_path, "task/refund"), "the branch is kept"
        )

        # A step cannot be sent to a parked job: its space belongs to what came next,
        # so a step would read the wrong code or take the checkout back.
        code, _, err = self.step("--job", job_id, "ship: add the refund page")
        self.assertEqual(code, 2)
        self.assertIn("parked", err)

        # The next job starts in the same space: no second pane, no second checkout.
        new_id, _ = self.open_a_job(label="terms")
        self.assertNotEqual(new_id, job_id)
        self.assertEqual(gitcmd.current_branch(maker_folder), "task/terms")

        # The owner's merge word still lands on the recorded commit, while the
        # space holds the new job.
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
        merged = StateStore(self.state).get_job(job_id)
        self.assertIsNotNone(merged.merged_at)
        self.assertEqual(merged.commit, parked.park_commit, "the parked commit is merged")
        self.assertEqual(
            gitcmd.current_branch(maker_folder),
            "task/terms",
            "the new job's checkout is not disturbed by the merge",
        )

    def test_a_parked_branch_that_moved_is_not_merged(self) -> None:
        job_id, _ = self.publish_a_pinned_job()
        self.assertEqual(self.cli("job", "park", job_id, env=self.state_env())[0], 0)
        parked = StateStore(self.state).get_job(job_id)
        self.assertEqual(
            self.cli(
                "job",
                "word",
                job_id,
                "--word",
                "merge it",
                "--by",
                "lacattano",
                env=self.state_env(),
            )[0],
            0,
        )
        # Someone moves the branch by hand while it waits. The parked space cannot:
        # it is detached, and the branch is not checked out there any more.
        moved = self.save_as_crew(self.repo_path, name="moved.py")
        gitcmd.run_git(self.repo_path, "branch", "-f", "task/refund", moved)
        with mock.patch.object(cli, "_merge_pull_request") as merge:
            code, _, err = self.cli("job", "merge", job_id, "--pr", "9", env=self.state_env())
        self.assertEqual(code, 1)
        self.assertIn("has moved", err)
        self.assertIn(parked.park_commit[:7], err)
        merge.assert_not_called()
        self.assertIsNone(StateStore(self.state).get_job(job_id).merged_at)

    # -- the learning loop: one record, a generated handover, one start command --

    def test_start_names_the_handover_and_its_age_and_marks_it_stale(self) -> None:
        # A handover that is stale must read as stale. Before this, a handover that
        # was never written was silently absent, and nobody noticed.
        code, out, err = self.cli("start", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("handover: none", out)
        self.assertIn("no handover exists", out)

        days = 3
        moment = datetime.now(UTC) - timedelta(days=days)
        path = self.root / f"HANDOVER-{moment.date().isoformat()}.md"
        path.write_text(
            f"# Handover - {moment.date().isoformat()}\n\n"
            f"generated: {to_iso(moment)}\n\nno work recorded since.\n",
            encoding="utf-8",
        )
        code, out, err = self.cli("start", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn(str(path), out)
        self.assertIn(f"{days} day(s) old", out)
        self.assertIn("current", out)

        # A session ran after it (a dispatch is recorded), and no new handover came.
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        code, out, err = self.cli("start", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("STALE", out)
        self.assertIn("no handover has been written since", out)
        self.assertIn("clowder handover write", out)

        # And what waits on the front door is named, from the record.
        self.cli(
            "defects",
            "add",
            "--line",
            "a step does not close when it reports",
            env=self.fake_env(),
        )
        self.cli(
            "defects",
            "add",
            "--line",
            "a step does not close when it reports",
            env=self.fake_env(),
        )
        code, out, err = self.cli("start", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("defects seen twice with no open job: 1", out)
        self.assertIn("d-0001", out)
        self.assertIn("steps in flight: 1", out)
        self.assertIn("t-0001", out)

    def test_the_defects_commands_write_and_read_one_record(self) -> None:
        code, out, err = self.cli(
            "defects", "add", "--line", "one line only", env=self.fake_env()
        )
        self.assertEqual(code, 0, err)
        self.assertIn("d-0001 recorded", out)
        # The next sighting of the same words is the same defect, punctuation and
        # case included; a second sighting with no job is the front door's to act on.
        code, out, err = self.cli(
            "defects", "add", "--line", "One line only.", env=self.fake_env()
        )
        self.assertEqual(code, 0, err)
        self.assertIn("seen again: 2 times", out)
        self.assertIn("opens a job or records an accepted workaround", out)

        code, out, err = self.cli("defects", "list", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("d-0001", out)
        self.assertIn("one line only", out)

        code, out, err = self.cli("defects", "list", "--repeated", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("d-0001", out)

        code, out, err = self.cli("defects", "show", "d-0001", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("one line only", out)
        self.assertIn("2", out)

        code, _, err = self.cli("defects", "show", "d-9999", env=self.fake_env())
        self.assertEqual(code, 1)
        self.assertIn("no defect", err)

        # One record, beside the state file. Nothing else stores a defect.
        stored = json.loads((self.root / "defects.json").read_text(encoding="utf-8"))
        self.assertEqual(list(stored["defects"]), ["d-0001"])

    def test_a_defect_needs_one_line_a_reason_for_an_acceptance_and_a_real_job(self) -> None:
        code, _, err = self.cli("defects", "add", "--line", "one\ntwo", env=self.fake_env())
        self.assertEqual(code, 1)
        self.assertIn("one line", err)

        code, _, err = self.cli(
            "defects",
            "add",
            "--line",
            "a known cost",
            "--status",
            "accepted-workaround",
            env=self.fake_env(),
        )
        self.assertEqual(code, 1)
        self.assertIn("--reason", err)

        code, _, err = self.cli(
            "defects",
            "add",
            "--line",
            "a known cost",
            "--status",
            "has-job",
            env=self.fake_env(),
        )
        self.assertEqual(code, 1)
        self.assertIn("--job", err)

        code, _, err = self.cli(
            "defects",
            "add",
            "--line",
            "a known cost",
            "--job",
            "j-9999",
            env=self.fake_env(),
        )
        self.assertEqual(code, 1)
        self.assertIn("j-9999", err)

    def stored_defect(self, defect_id: str) -> dict:
        stored = json.loads((self.root / "defects.json").read_text(encoding="utf-8"))
        return stored["defects"][defect_id]

    def merge_word(self, job_id: str) -> None:
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

    def test_defect_link_names_the_job_that_fixes_it(self) -> None:
        self.cli(
            "defects", "add", "--line", "a bundle carries a machine name", env=self.fake_env()
        )
        job_id, _ = self.open_a_job()
        code, out, err = self.cli(
            "defects", "link", "d-0001", "--job", job_id, env=self.state_env()
        )
        self.assertEqual(code, 0, err)
        self.assertIn(f"d-0001 linked to {job_id}", out)
        defect = self.stored_defect("d-0001")
        self.assertEqual(defect["state"], "has-job")
        self.assertEqual(defect["jobs"], [job_id])
        # Linking twice is harmless: one defect, one link.
        code, out, _ = self.cli(
            "defects", "link", "d-0001", "--job", job_id, env=self.state_env()
        )
        self.assertEqual(code, 0, err)
        self.assertIn("already linked", out)
        self.assertEqual(self.stored_defect("d-0001")["jobs"], [job_id])

    def test_a_merge_marks_its_linked_defects_and_only_those(self) -> None:
        for line in ("a bundle carries a machine name", "the board clips a long answer"):
            code, _, err = self.cli("defects", "add", "--line", line, env=self.fake_env())
            self.assertEqual(code, 0, err)
        job_id, _ = self.open_a_job("evidence", extra=("--defect", "d-0001"))
        self.merge_word(job_id)
        with mock.patch.object(cli, "_merge_pull_request"):
            code, out, err = self.cli(
                "job", "merge", job_id, "--pr", "87", env=self.state_env()
            )
        self.assertEqual(code, 0, err)
        self.assertIn("defects marked fixed: d-0001", out)

        fixed = self.stored_defect("d-0001")
        self.assertEqual(fixed["state"], "fixed")
        self.assertEqual(fixed["job"], job_id)
        self.assertEqual(fixed["fixed_pr"], "87")
        self.assertTrue(fixed["fixed_at"], "the merge date is recorded")
        self.assertEqual(fixed["jobs"], [])
        self.assertEqual(self.stored_defect("d-0002")["state"], "open", "not this job's")

        lines = [
            line
            for line in audit.read_audit(self.state)
            if line.get("action") == "defects-fixed"
        ]
        self.assertEqual(len(lines), 1, "one audit line for the merge")
        self.assertEqual(lines[0]["job"], job_id)
        self.assertEqual(lines[0]["pr"], "87")
        self.assertEqual(lines[0]["defects"], ["d-0001"])

    def test_a_merge_with_no_linked_defect_leaves_the_record_alone(self) -> None:
        self.cli(
            "defects", "add", "--line", "a bundle carries a machine name", env=self.fake_env()
        )
        before = (self.root / "defects.json").read_text(encoding="utf-8")
        job_id, _ = self.open_a_job()
        self.merge_word(job_id)
        with mock.patch.object(cli, "_merge_pull_request"):
            code, out, err = self.cli("job", "merge", job_id, "--pr", "9", env=self.state_env())
        self.assertEqual(code, 0, err)
        self.assertNotIn("defects marked fixed", out)
        self.assertEqual((self.root / "defects.json").read_text(encoding="utf-8"), before)
        lines = [
            line
            for line in audit.read_audit(self.state)
            if line.get("action") == "defects-fixed"
        ]
        self.assertEqual(lines, [])

    def test_a_linked_job_suppresses_the_repeat_alarm_until_it_merges(self) -> None:
        for _ in range(2):
            self.cli(
                "defects",
                "add",
                "--line",
                "a bundle carries a machine name",
                env=self.fake_env(),
            )
        code, out, _ = self.cli("defects", "list", "--repeated", env=self.fake_env())
        self.assertIn("d-0001", out, "seen twice with no job, so it needs one")

        job_id, _ = self.open_a_job("evidence", extra=("--defect", "d-0001"))
        code, out, err = self.cli("defects", "list", "--repeated", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("no defect has been seen twice with no open job", out)
        code, out, _ = self.cli("defects", "list", env=self.fake_env())
        self.assertIn("has-job", out, "the list shows the job it has")

        # A job closed without merging fixed nothing, so the alarm comes back.
        code, _, err = self.cli("job", "close", job_id, env=self.state_env())
        self.assertEqual(code, 0, err)
        code, out, _ = self.cli("defects", "list", "--repeated", env=self.fake_env())
        self.assertIn("d-0001", out)
        code, out, _ = self.cli("defects", "list", env=self.fake_env())
        self.assertIn("open", out)
        self.assertEqual(self.stored_defect("d-0001")["jobs"], [], "the dead link is dropped")

    def test_a_defect_linked_to_two_jobs_is_fixed_by_the_later_merge(self) -> None:
        self.cli(
            "defects", "add", "--line", "a bundle carries a machine name", env=self.fake_env()
        )
        first, _ = self.open_a_job("evidence", extra=("--defect", "d-0001"))
        second = "j-0002"
        self.write_job(second, "task/evidence")
        code, out, err = self.cli(
            "defects", "link", "d-0001", "--job", second, env=self.state_env()
        )
        self.assertEqual(code, 0, err)
        self.merge_word(first)
        self.merge_word(second)

        with mock.patch.object(cli, "_merge_pull_request"):
            code, out, err = self.cli("job", "merge", first, "--pr", "87", env=self.state_env())
        self.assertEqual(code, 0, err)
        pending = self.stored_defect("d-0001")
        self.assertEqual(pending["state"], "has-job", "a second job still says it is coming")
        self.assertEqual(pending["jobs"], [second])
        self.assertIsNone(pending["fixed_at"])

        with mock.patch.object(cli, "_merge_pull_request"):
            code, out, err = self.cli(
                "job", "merge", second, "--pr", "88", env=self.state_env()
            )
        self.assertEqual(code, 0, err)
        fixed = self.stored_defect("d-0001")
        self.assertEqual(fixed["state"], "fixed")
        self.assertEqual(fixed["fixed_pr"], "88")
        self.assertTrue(fixed["fixed_at"])
        # Marked once: the first merge left it open, the second closed it.
        self.assertEqual(fixed["count"], 1)

    def test_a_defect_cannot_be_linked_to_a_job_that_is_closed(self) -> None:
        self.cli(
            "defects", "add", "--line", "a bundle carries a machine name", env=self.fake_env()
        )
        job_id, _ = self.open_a_job()
        self.assertEqual(self.cli("job", "close", job_id, env=self.state_env())[0], 0)
        code, _, err = self.cli(
            "defects", "link", "d-0001", "--job", job_id, env=self.state_env()
        )
        self.assertEqual(code, 1)
        self.assertIn("cannot be the fix", err)

    def test_a_defect_that_comes_back_closes_again_with_the_new_job(self) -> None:
        # Blocker 1 from the j-0225 review: the old fixer came back as a link, so the
        # second merge left the defect has-job pointing at a closed job, for ever.
        self.cli(
            "defects", "add", "--line", "a bundle carries a machine name", env=self.fake_env()
        )
        first, _ = self.open_a_job("one", extra=("--defect", "d-0001"))
        self.merge_word(first)
        with mock.patch.object(cli, "_merge_pull_request"):
            code, _, err = self.cli("job", "merge", first, "--pr", "87", env=self.state_env())
        self.assertEqual(code, 0, err)
        self.assertEqual(self.stored_defect("d-0001")["state"], "fixed")

        code, out, err = self.cli(
            "defects", "add", "--line", "a bundle carries a machine name", env=self.fake_env()
        )
        self.assertEqual(code, 0, err)
        self.assertIn("seen again", out)
        again = self.stored_defect("d-0001")
        self.assertEqual(again["state"], "open")
        self.assertEqual(again["jobs"], [], "the old fixer is history, not a link")
        self.assertIsNone(again["job"])
        self.assertIsNone(again["fixed_at"])
        self.assertIsNone(again["fixed_pr"])

        second, _ = self.open_a_job("two", extra=("--defect", "d-0001"))
        self.merge_word(second)
        with mock.patch.object(cli, "_merge_pull_request"):
            code, _, err = self.cli("job", "merge", second, "--pr", "88", env=self.state_env())
        self.assertEqual(code, 0, err)
        fixed = self.stored_defect("d-0001")
        self.assertEqual(fixed["state"], "fixed")
        self.assertEqual(fixed["job"], second)
        self.assertEqual(fixed["fixed_pr"], "88")

    def test_a_reopen_with_an_explicit_status_closes_again_with_the_new_job(self) -> None:
        # The exact reviewer gap, through the command: `--status open` and
        # `--status has-job` must both forget the old fix, so the new merge closes it.
        self.cli(
            "defects", "add", "--line", "a bundle carries a machine name", env=self.fake_env()
        )
        first, _ = self.open_a_job("one", extra=("--defect", "d-0001"))
        self.merge_word(first)
        with mock.patch.object(cli, "_merge_pull_request"):
            code, _, err = self.cli("job", "merge", first, "--pr", "87", env=self.state_env())
        self.assertEqual(code, 0, err)
        self.assertEqual(self.stored_defect("d-0001")["state"], "fixed")

        code, _, err = self.cli(
            "defects",
            "add",
            "--line",
            "a bundle carries a machine name",
            "--status",
            "open",
            env=self.fake_env(),
        )
        self.assertEqual(code, 0, err)
        again = self.stored_defect("d-0001")
        self.assertEqual(again["state"], "open")
        self.assertEqual(again["jobs"], [], "the dead link is gone")
        self.assertIsNone(again["fixed_at"])
        self.assertIsNone(again["fixed_pr"])

        second, _ = self.open_a_job("two", extra=("--defect", "d-0001"))
        self.merge_word(second)
        with mock.patch.object(cli, "_merge_pull_request"):
            code, _, err = self.cli("job", "merge", second, "--pr", "88", env=self.state_env())
        self.assertEqual(code, 0, err)
        fixed = self.stored_defect("d-0001")
        self.assertEqual(fixed["state"], "fixed")
        self.assertEqual(fixed["job"], second)
        self.assertEqual(fixed["fixed_pr"], "88")

    def test_a_reopen_as_has_job_links_the_new_job_and_closes_with_it(self) -> None:
        self.cli(
            "defects", "add", "--line", "a bundle carries a machine name", env=self.fake_env()
        )
        first, _ = self.open_a_job("one", extra=("--defect", "d-0001"))
        self.merge_word(first)
        with mock.patch.object(cli, "_merge_pull_request"):
            code, _, err = self.cli("job", "merge", first, "--pr", "87", env=self.state_env())
        self.assertEqual(code, 0, err)

        second, _ = self.open_a_job("two")
        code, _, err = self.cli(
            "defects",
            "add",
            "--line",
            "a bundle carries a machine name",
            "--status",
            "has-job",
            "--job",
            second,
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        again = self.stored_defect("d-0001")
        self.assertEqual(again["state"], "has-job")
        self.assertEqual(again["jobs"], [second], "the new job only, not the closed one")
        self.assertIsNone(again["fixed_at"])

        self.merge_word(second)
        with mock.patch.object(cli, "_merge_pull_request"):
            code, _, err = self.cli("job", "merge", second, "--pr", "88", env=self.state_env())
        self.assertEqual(code, 0, err)
        fixed = self.stored_defect("d-0001")
        self.assertEqual(fixed["state"], "fixed")
        self.assertEqual(fixed["job"], second)

    def test_defects_add_refuses_a_closed_job_unless_it_records_a_fix(self) -> None:
        job_id, _ = self.open_a_job()
        self.assertEqual(self.cli("job", "close", job_id, env=self.state_env())[0], 0)
        code, _, err = self.cli(
            "defects", "add", "--line", "one line only", "--job", job_id, env=self.state_env()
        )
        self.assertEqual(code, 1)
        self.assertIn("cannot be the job that fixes", err)
        # A seed or a hand-recorded fix may name the job that already merged.
        code, _, err = self.cli(
            "defects",
            "add",
            "--line",
            "one line only",
            "--status",
            "fixed",
            "--job",
            job_id,
            env=self.fake_env(),
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(self.stored_defect("d-0001")["state"], "fixed")

    def test_a_merge_never_rewrites_an_accepted_workaround(self) -> None:
        # Blocker 2: an accepted workaround stored a link, and the merge turned it into
        # a fix, throwing away the owner's decision.
        job_id, _ = self.open_a_job()
        code, _, err = self.cli(
            "defects",
            "add",
            "--line",
            "elapsed time is wall clock, not work",
            "--job",
            job_id,
            "--status",
            "accepted-workaround",
            "--reason",
            "the owner quotes active time instead",
            env=self.state_env(),
        )
        self.assertEqual(code, 0, err)
        self.merge_word(job_id)
        with mock.patch.object(cli, "_merge_pull_request"):
            code, out, err = self.cli(
                "job", "merge", job_id, "--pr", "87", env=self.state_env()
            )
        self.assertEqual(code, 0, err)
        self.assertNotIn("defects marked fixed", out)
        defect = self.stored_defect("d-0001")
        self.assertEqual(defect["state"], "accepted-workaround")
        self.assertEqual(defect["reason"], "the owner quotes active time instead")
        self.assertIsNone(defect["fixed_at"])

    def test_defects_unlink_drops_a_wrong_link_and_records_it(self) -> None:
        self.cli("defects", "add", "--line", "one line only", env=self.fake_env())
        job_id, _ = self.open_a_job()
        self.cli("defects", "link", "d-0001", "--job", job_id, env=self.state_env())
        code, out, err = self.cli(
            "defects", "unlink", "d-0001", "--job", job_id, env=self.state_env()
        )
        self.assertEqual(code, 0, err)
        self.assertIn(f"unlinked from {job_id}", out)
        defect = self.stored_defect("d-0001")
        self.assertEqual(defect["state"], "open")
        self.assertEqual(defect["jobs"], [])
        lines = [
            line
            for line in audit.read_audit(self.state)
            if line.get("action") == "defects-unlinked"
        ]
        self.assertEqual(len(lines), 1, "a dropped link leaves a trail")
        self.assertEqual(lines[0]["defects"], ["d-0001"])
        self.assertEqual(lines[0]["job"], job_id)
        self.assertNotIn("defect", lines[0], "one shape for a dropped link")

    def test_a_non_final_merge_audits_the_link_it_drops(self) -> None:
        self.cli(
            "defects", "add", "--line", "a bundle carries a machine name", env=self.fake_env()
        )
        first, _ = self.open_a_job("one", extra=("--defect", "d-0001"))
        second = "j-0002"
        self.write_job(second, "task/two")
        self.cli("defects", "link", "d-0001", "--job", second, env=self.state_env())
        self.merge_word(first)
        with mock.patch.object(cli, "_merge_pull_request"):
            code, _, err = self.cli("job", "merge", first, "--pr", "87", env=self.state_env())
        self.assertEqual(code, 0, err)
        self.assertEqual(self.stored_defect("d-0001")["state"], "has-job")
        lines = [
            line
            for line in audit.read_audit(self.state)
            if line.get("action") == "defects-fixed"
        ]
        self.assertEqual(len(lines), 1, "dropping a link is a change, so it is audited")
        self.assertEqual(lines[0]["defects"], [])
        self.assertEqual(lines[0]["still_open"], ["d-0001"])

    def test_closing_a_job_drops_its_link_and_audits_it(self) -> None:
        self.cli("defects", "add", "--line", "one line only", env=self.fake_env())
        job_id, _ = self.open_a_job("one", extra=("--defect", "d-0001"))
        code, _, err = self.cli("job", "close", job_id, env=self.state_env())
        self.assertEqual(code, 0, err)
        defect = self.stored_defect("d-0001")
        self.assertEqual(defect["state"], "open")
        self.assertEqual(defect["jobs"], [])
        lines = [
            line
            for line in audit.read_audit(self.state)
            if line.get("action") == "defects-unlinked"
        ]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["job"], job_id)
        self.assertEqual(lines[0]["defects"], ["d-0001"])
        self.assertNotIn("defect", lines[0], "the same shape as defects unlink")

    def test_a_settled_defect_cannot_be_linked(self) -> None:
        self.cli(
            "defects",
            "add",
            "--line",
            "a known cost",
            "--status",
            "accepted-workaround",
            "--reason",
            "the owner accepts it",
            env=self.fake_env(),
        )
        job_id, _ = self.open_a_job()
        code, _, err = self.cli(
            "defects", "link", "d-0001", "--job", job_id, env=self.state_env()
        )
        self.assertEqual(code, 1)
        self.assertIn("accepted-workaround", err)
        self.assertIn("recorded again", err)

    def test_handover_write_names_the_file_and_refuses_a_defect_with_no_id(self) -> None:
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        self.cli("defects", "add", "--line", "a quirk found today", env=self.fake_env())
        code, out, err = self.cli("handover", "write", env=self.fake_env())
        self.assertEqual(code, 0, err)
        today = datetime.now(UTC).date().isoformat()
        path = self.root / f"HANDOVER-{today}.md"
        self.assertIn(str(path), out, "the command names the file it wrote")
        text = path.read_text(encoding="utf-8")
        self.assertIn(today, text)
        self.assertIn("d-0001", text)
        self.assertIn("a quirk found today", text)
        self.assertNotIn("## Tool quirks", text, "the old hand-written shape is gone")

        # A hand-edited entry with no id stops it, and the file already there stays.
        broken = json.loads((self.root / "defects.json").read_text(encoding="utf-8"))
        broken["defects"]["d-0002"] = {
            "line": "an entry with no id",
            "first_seen": "2026-10-25T09:00:00Z",
            "last_seen": "2026-10-25T09:00:00Z",
        }
        (self.root / "defects.json").write_text(json.dumps(broken), encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        code, _, err = self.cli("handover", "write", env=self.fake_env())
        self.assertEqual(code, 1)
        self.assertIn("d-0002", err)
        self.assertIn("no id", err)
        self.assertEqual(path.read_text(encoding="utf-8"), before, "nothing was written")

    def test_handover_write_twice_for_one_session_does_not_duplicate(self) -> None:
        self.cli("defects", "add", "--line", "a quirk found today", env=self.fake_env())
        first = self.cli("handover", "write", env=self.fake_env())
        second = self.cli("handover", "write", env=self.fake_env())
        self.assertEqual(first[0], 0, first[2])
        self.assertEqual(second[0], 0, second[2])
        self.assertIn("replaced the handover already written today", second[1])
        files = sorted(self.root.glob("HANDOVER-*.md"))
        self.assertEqual(len(files), 1)
        text = files[0].read_text(encoding="utf-8")
        self.assertEqual(text.count("d-0001"), 1)
        self.assertEqual(text.count("## What waits on the owner"), 1)

    def test_the_board_shows_a_repeated_defect_for_the_front_door(self) -> None:
        self.cli("defects", "add", "--line", "a quirk with no job", env=self.fake_env())
        self.cli("defects", "add", "--line", "a quirk with no job", env=self.fake_env())
        self.cli("board", env=self.fake_env())
        html = (self.root / "board.html").read_text(encoding="utf-8")
        worker = html.split("Waiting for a worker", 1)[1].split("</section>", 1)[0]
        needs = html.split("For you", 1)[1].split("</section>", 1)[0]
        self.assertIn("seen twice, no job", worker)
        self.assertIn("d-0001", worker)
        self.assertNotIn("seen twice, no job", needs, "not the owner's act")

    # -- the reviewer's fixes on the learning loop ---------------------------

    def test_defects_like_finds_the_same_words_and_misses_a_rephrase(self) -> None:
        # A reworded defect is a new id by design. Word search is the human-scale
        # mitigation: the same words in another order are found, a total rephrase is
        # not - and that miss is pinned so it is never silently lost.
        line = "a step does not close when it reports"
        self.cli("defects", "add", "--line", line, env=self.fake_env())

        code, out, err = self.cli(
            "defects", "list", "--like", "reports close step", env=self.fake_env()
        )
        self.assertEqual(code, 0, err)
        self.assertIn("d-0001", out)
        self.assertIn(line, out)
        self.assertIn("reworded defect shares no word", out)

        code, out, err = self.cli(
            "defects", "list", "--like", "finished work stays in flight", env=self.fake_env()
        )
        self.assertEqual(code, 0, err)
        self.assertIn("no defect holds every word", out)
        self.assertIn("a total rephrase is not found", out)
        self.assertNotIn("d-0001", out)

    def test_start_lists_the_defects_seen_in_the_last_seven_days(self) -> None:
        # The near-duplicate defence is a person reading the recent lines, so start
        # has to show them.
        moment = datetime.now(UTC)
        (self.root / "defects.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "next": 2,
                    "defects": {
                        "d-0001": {
                            "id": "d-0001",
                            "line": "a defect recorded two days ago",
                            "first_seen": to_iso(moment - timedelta(days=2)),
                            "last_seen": to_iso(moment - timedelta(days=2)),
                        },
                        "d-0002": {
                            "id": "d-0002",
                            "line": "a defect recorded nine days ago",
                            "first_seen": to_iso(moment - timedelta(days=9)),
                            "last_seen": to_iso(moment - timedelta(days=9)),
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        code, out, err = self.cli("start", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("defects seen in the last 7 days: 1", out)
        self.assertIn("d-0001 a defect recorded two days ago", out)
        self.assertNotIn("d-0002", out.split("defects seen in the last 7 days")[1])

    def test_a_corrupt_defects_record_fails_open_on_start_and_the_board(self) -> None:
        # One malformed file must not take away the page that says what is happening.
        # `start` and `board` draw from everything else, name the file and the fault,
        # and say with their exit code that the record was unreadable. The command
        # whose job is to read the record is the one that refuses.
        record = self.root / "defects.json"
        record.write_text("{not json", encoding="utf-8")

        code, out, err = self.cli("start", env=self.fake_env())
        self.assertEqual(code, cli.RECORD_UNREADABLE)
        self.assertIn("handover:", out, "the rest of the start output still prints")
        self.assertIn("queue:", out)
        self.assertIn("defects: unreadable", out)
        self.assertIn(str(record), err, "the warning names the file")
        self.assertIn("not valid JSON", err, "and the parse error")

        code, out, err = self.cli("start", "--json", env=self.fake_env())
        self.assertEqual(code, cli.RECORD_UNREADABLE)
        self.assertIn("not valid JSON", json.loads(out)["defects_error"])

        code, out, err = self.cli("board", env=self.fake_env())
        self.assertEqual(code, cli.RECORD_UNREADABLE)
        page = self.root / "board.html"
        self.assertTrue(page.is_file(), "the page is still written")
        html = page.read_text(encoding="utf-8")
        self.assertIn("defects record unreadable", html)
        self.assertIn("defects.json", html)
        self.assertIn(str(record), err)

        code, _, err = self.cli("defects", "list", env=self.fake_env())
        self.assertEqual(code, 1, "the reading command refuses")
        self.assertIn("not valid JSON", err)

    def test_a_corrupt_record_does_not_break_a_command_that_refreshes_the_board(self) -> None:
        # Every state-changing command rewrites the board. A broken record must not
        # make that refresh fail, or the page goes stale and says nothing about why.
        record = self.root / "defects.json"
        record.write_text("{not json", encoding="utf-8")
        code, out, err = self.cli("defects", "add", "--line", "x", env=self.fake_env())
        self.assertEqual(code, 1, "adding still refuses, because it must read the record")
        self.assertIn("not valid JSON", err)

    # -- seeding the record ---------------------------------------------------

    def seed(self, *args: str, env: dict[str, object] | None = None):
        """One seeded entry, the way the twelve were written."""
        return self.cli("defects", "add", *args, env=env or self.fake_env())

    def test_a_seeded_entry_records_its_dates_count_state_and_detail(self) -> None:
        # The seed path: a defect known from an older record, not seen just now. It
        # carries when it was first and last recorded, how often, and where it stands.
        code, out, err = self.seed(
            "--line",
            "a pane reset drops the pane to the default model",
            "--status",
            "open",
            "--first-seen",
            "2026-10-18",
            "--last-seen",
            "2026-10-25",
            "--count",
            "3",
            "--detail",
            "Seen in the handovers of 2026-10-18, 2026-10-22 and 2026-10-25.\n"
            "The reset warns and audits the change; it does not restore the model.",
        )
        self.assertEqual(code, 0, err)
        self.assertIn("d-0001 recorded", out)

        stored = json.loads((self.root / "defects.json").read_text(encoding="utf-8"))
        entry = stored["defects"]["d-0001"]
        self.assertEqual(entry["state"], "open")
        self.assertEqual(entry["count"], 3)
        self.assertEqual(entry["first_seen"], "2026-10-18T00:00:00Z")
        self.assertEqual(entry["last_seen"], "2026-10-25T00:00:00Z")
        self.assertIn("does not restore the model", entry["detail"])

        code, out, err = self.cli("defects", "list", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("d-0001", out)
        self.assertIn("open", out)

        code, out, err = self.cli("defects", "show", "d-0001", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("does not restore the model", out, "the detail is shown")

    def test_a_fixed_entry_is_listed_and_not_repeated(self) -> None:
        # A fixed defect is kept: a return of it is exactly the repetition the record
        # exists to catch. It is not a repetition now, so it is not in --repeated.
        code, _, err = self.seed(
            "--line",
            "a step that reports left its step open",
            "--status",
            "fixed",
            "--first-seen",
            "2026-10-25",
            "--count",
            "2",
            env=self.fake_env(),
        )
        self.assertEqual(code, 0, err)
        code, _, err = self.seed(
            "--line",
            "a job awaiting the merge word held its space",
            "--status",
            "open",
            "--first-seen",
            "2026-10-25",
            "--count",
            "2",
            env=self.fake_env(),
        )
        self.assertEqual(code, 0, err)

        code, out, err = self.cli("defects", "list", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("d-0001", out, "a fixed entry stays in the list")
        self.assertIn("fixed", out)
        self.assertIn("d-0002", out)

        code, out, err = self.cli("defects", "list", "--repeated", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("d-0002", out, "seen twice with no job")
        self.assertNotIn("d-0001", out, "a fixed defect is not a repetition")

    def test_a_seeded_entry_without_a_state_or_a_start_is_refused(self) -> None:
        # The guard that keeps the next seed honest: a seed says both when it was
        # recorded and where it stands today. Dates without a state, or a count with no
        # start, are refused rather than written with a default.
        code, _, err = self.seed(
            "--line", "a defect with a date but no state", "--first-seen", "2026-10-25"
        )
        self.assertEqual(code, 1)
        self.assertIn("needs --status", err)

        code, _, err = self.seed(
            "--line",
            "a defect with a count but no start",
            "--count",
            "2",
            "--status",
            "open",
        )
        self.assertEqual(code, 1)
        self.assertIn("--count needs --first-seen", err)

        code, _, err = self.seed(
            "--line",
            "a defect that ends before it starts",
            "--first-seen",
            "2026-10-25",
            "--last-seen",
            "2026-10-18",
            "--status",
            "open",
        )
        self.assertEqual(code, 1)
        self.assertIn("last seen cannot be before first seen", err)

        self.assertFalse((self.root / "defects.json").exists(), "a refused seed writes nothing")

    def test_start_names_the_seeded_entries_as_seen_in_the_last_seven_days(self) -> None:
        # The source handovers are within the week, so every seeded entry is in the
        # recent list once - the near-duplicate defence seeding depends on.
        for line in ("a seeded defect one", "a seeded defect two"):
            code, _, err = self.seed(
                "--line",
                line,
                "--status",
                "open",
                "--first-seen",
                "2026-10-18",
                "--last-seen",
                "2026-10-25",
                env=self.fake_env(),
            )
            self.assertEqual(code, 0, err)
        code, out, err = self.cli("start", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("defects seen in the last 7 days: 2", out)
        self.assertEqual(out.count("d-0001"), 1, "each entry once")
        self.assertEqual(out.count("d-0002"), 1)

    # -- the page the owner reads -------------------------------------------

    def test_the_board_renders_from_state_without_asking_the_multiplexer(self) -> None:
        # The measured cost: a board render used to read the roster (a multiplexer
        # call plus a git look per agent) on every state change. It is built from the
        # state file now, and says so rather than pretending the roster is live.
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        with mock.patch.object(
            cli.Mux, "list_agents", side_effect=AssertionError("the board asked the mux")
        ):
            code, _, err = self.cli("board", env=self.fake_env())
            self.assertEqual(code, 0, err)
            code, _, err = self.cli(
                "owner", "t-0001", "--item", "choose A or B", env=self.fake_env()
            )
            self.assertEqual(code, 0, err)
        html = (self.root / "board.html").read_text(encoding="utf-8")
        self.assertIn("roster from the state file", html)
        self.assertIn("not read live", html, "the page says the roster is not live")
        self.assertIn("its state was not read live", html, "no agent is called idle from state")

    def test_board_live_asks_the_multiplexer_once(self) -> None:
        with mock.patch.object(cli.Mux, "list_agents", return_value=[]) as listed:
            code, _, err = self.cli("board", "--live", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertEqual(listed.call_count, 1, "one live read, not one per agent")
        html = (self.root / "board.html").read_text(encoding="utf-8")
        self.assertIn("roster read live", html)
        self.assertIn("No agents were found in the live roster.", html)

    def test_the_board_is_rewritten_without_the_multiplexer_on_a_state_change(self) -> None:
        # Twenty-six commands rewrite the page on every state change. None of them may
        # pay for a roster read now.
        self.dispatch(
            "maker", "myrepo", "ship: add the refund page", "--worktree", str(self.repo_path)
        )
        with mock.patch.object(
            cli.Mux, "list_agents", side_effect=AssertionError("a state change read the mux")
        ):
            code, _, err = self.cli(
                "owner", "t-0001", "--item", "choose A or B", env=self.fake_env()
            )
        self.assertEqual(code, 0, err)
        html = (self.root / "board.html").read_text(encoding="utf-8")
        self.assertIn("choose A or B", html)
        self.assertIn("roster from the state file", html)

    # -- what waits on the owner: one list, two views ------------------------

    def add_open_decision(self, task_id: str = "t-0001") -> None:
        store = StateStore(self.state)
        store.add(
            Task(
                id=task_id,
                question="which refund window?",
                brief="ship: x",
                shape="ship",
                agent="maker",
                repo="myrepo",
                repo_path=str(self.repo_path),
                status=REPORTED,
                open_decision="14 days or 30 for the refund window?",
            )
        )
        store.save()

    def test_start_and_the_board_count_the_same_three(self) -> None:
        # The defect: start said zero while a change waited on a walkthrough. One list
        # now feeds both, so a passed-unmerged change, a walkthrough and an open
        # decision are three in both views.
        self.write_job(
            "j-0001",
            branch="task/merge",
            pass_at="2026-10-26T09:00:00Z",
            published_at="2026-10-26T10:00:00Z",
        )
        self.write_held_job("j-0002", "task/held", moved=False)
        self.add_open_decision()

        code, out, err = self.cli("start", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("waiting on you: 3", out)
        for text in ("reply merge", "reply pass, or what to change", "type "):
            self.assertIn(text, out)
        self.assertIn("14 days or 30 for the refund window?", out, "the decision is listed")

        code, _, err = self.cli("board", env=self.fake_env())
        self.assertEqual(code, 0, err)
        html = (self.root / "board.html").read_text(encoding="utf-8")
        section = html.split("For you", 1)[1].split("Running now", 1)[0]
        self.assertIn("count'>3</span>", section)
        self.assertEqual(section.count("<li>"), 3, "three lines, and the count says three")

    def test_start_and_the_board_say_none_when_nothing_waits(self) -> None:
        code, out, err = self.cli("start", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("waiting on you: 0", out)
        self.assertIn("(nothing recorded)", out)

        code, _, err = self.cli("board", env=self.fake_env())
        self.assertEqual(code, 0, err)
        html = (self.root / "board.html").read_text(encoding="utf-8")
        section = html.split("For you", 1)[1].split("Running now", 1)[0]
        self.assertIn("count'>0</span>", section)
        self.assertIn("Nothing is recorded as waiting on you.", section)

    def test_start_counts_a_change_waiting_on_a_walkthrough(self) -> None:
        # The j-0206 regression, alone: a change handed over, its review finished, and
        # nobody has walked it. The old start said zero.
        self.write_held_job("j-0001", "task/held", moved=False)
        code, out, err = self.cli("start", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("waiting on you: 1", out)
        self.assertIn("reply pass, or what to change", out)

    def test_both_views_call_the_one_waiting_list(self) -> None:
        # The shared path, not equal output on one fixture: patch the one function and
        # both views must render what it returns.
        sentinel = owner_mod.Waiting(
            kind=owner_mod.DECISION,
            heading="the one shared question",
            note="your move",
            action="type 1",
            since=now_iso(),
            id="t-9999",
        )
        with mock.patch.object(
            owner_mod, "waiting_on_owner", return_value=([sentinel], [])
        ) as shared:
            code, out, err = self.cli("start", env=self.fake_env())
            self.assertEqual(code, 0, err)
            self.assertIn("the one shared question", out)
            data = cli._board_data(cli.load_config(str(self.config)), StateStore(self.state))
            lines = board_waiting_on_you(data)
            handover = self.cli("handover", "write", env=self.fake_env())
            self.assertEqual(handover[0], 0, handover[2])
        self.assertGreaterEqual(shared.call_count, 3, "all three views asked the one list")
        self.assertIn("the one shared question", " ".join(lines))
        written = next(line for line in handover[1].splitlines() if line.startswith("wrote "))
        self.assertIn(
            "the one shared question",
            Path(written.split(maxsplit=1)[1]).read_text(encoding="utf-8"),
        )

    def test_a_job_awaiting_ci_is_not_counted_for_him(self) -> None:
        # His word is recorded and the pull request waits on CI: the front door's move,
        # shown as "not yours" and never counted.
        self.write_job(
            "j-0001",
            branch="task/ci",
            pass_at="2026-10-26T09:00:00Z",
            published_at="2026-10-26T10:00:00Z",
            merge_word_at="2026-10-26T11:00:00Z",
        )
        code, out, err = self.cli("start", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("waiting on you: 0", out)
        self.assertIn("not yours: 1", out)
        self.assertIn("when CI is green", out)

        code, _, err = self.cli("board", env=self.fake_env())
        self.assertEqual(code, 0, err)
        html = (self.root / "board.html").read_text(encoding="utf-8")
        section = html.split("For you", 1)[1].split("Running now", 1)[0]
        self.assertIn("count'>0</span>", section)
        worker = html.split("Waiting for a worker", 1)[1].split("What landed", 1)[0]
        self.assertIn("not yours", worker)
        self.assertIn("when CI is green", worker)

    # -- an owner item on a closed step, and the queue count ------------------

    def add_owner_item(
        self,
        task_id: str,
        item: str,
        *,
        status: str = CLOSED,
        at: str = "2026-10-20T09:00:00Z",
        created_at: str = "2026-10-10T09:00:00Z",
        job: str | None = None,
        decision: str | None = None,
    ) -> None:
        """A question recorded on a step, whatever its status."""
        store = StateStore(self.state)
        store.add(
            Task(
                id=task_id,
                question="a question for the owner",
                brief="ship: x",
                shape="ship",
                agent="maker",
                repo="myrepo",
                repo_path=str(self.repo_path),
                status=status,
                job=job,
                created_at=created_at,
                owner_item=item,
                owner_item_at=at,
                open_decision=decision,
            )
        )
        store.save()

    def test_a_closed_step_s_owner_item_shows_in_start_and_on_the_board(self) -> None:
        # Seen 2026-10-27: three owner items sat on closed steps and disappeared from
        # both views the moment the step closed. A recorded question waits until it is
        # cleared or answered.
        self.add_owner_item("t-0988", "Approve one capture command in llm-benchmarks")
        code, out, err = self.cli("start", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("waiting on you: 1", out)
        self.assertIn("Approve one capture command", out)
        self.assertIn("type 1", out)

        code, _, err = self.cli("board", env=self.fake_env())
        self.assertEqual(code, 0, err)
        html = (self.root / "board.html").read_text(encoding="utf-8")
        section = html.split("For you", 1)[1].split("Running now", 1)[0]
        self.assertIn("count'>1</span>", section)
        self.assertIn("Approve one capture command", section)
        self.assertIn("type 1", section)

    def test_clearing_a_closed_step_s_item_removes_it_everywhere(self) -> None:
        self.add_owner_item("t-0988", "Approve one capture command in llm-benchmarks")
        code, _, err = self.cli("owner", "t-0988", "--clear", env=self.fake_env())
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("start", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("waiting on you: 0", out)
        self.assertNotIn("Approve one capture command", out)
        code, _, err = self.cli("board", env=self.fake_env())
        self.assertEqual(code, 0, err)
        html = (self.root / "board.html").read_text(encoding="utf-8")
        self.assertNotIn("Approve one capture command", html)

    def test_the_handover_shows_the_same_closed_step_item(self) -> None:
        self.add_owner_item("t-0988", "Approve one capture command in llm-benchmarks")
        code, out, err = self.cli("handover", "write", env=self.fake_env())
        self.assertEqual(code, 0, err)
        written = next(line for line in out.splitlines() if line.startswith("wrote "))
        text = Path(written.split(maxsplit=1)[1]).read_text(encoding="utf-8")
        self.assertIn("Approve one capture command", text)
        self.assertIn("type 1", text)

    def test_the_three_live_items_are_the_same_fixture_and_go_when_cleared(self) -> None:
        # The equivalent fixture, not the live file: three questions on closed steps.
        for task_id, item in (
            ("t-0988", "Approve one capture command in llm-benchmarks"),
            ("t-0991", "Re-run or adopt q8 after the cache setting changed"),
            ("t-0814", "Parked business decisions - RAG at launch"),
        ):
            self.add_owner_item(task_id, item)
        code, out, err = self.cli("start", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("waiting on you: 3", out)
        for item in ("Approve one capture", "Re-run or adopt", "Parked business"):
            self.assertIn(item, out)

        for task_id in ("t-0988", "t-0991", "t-0814"):
            self.assertEqual(self.cli("owner", task_id, "--clear", env=self.fake_env())[0], 0)
        code, out, err = self.cli("start", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("waiting on you: 0", out)

    # -- --clear must not claim more than it did ------------------------------

    def test_clear_clears_an_owner_item_and_says_so(self) -> None:
        self.add_owner_item("t-0001", "choose A or B")
        code, out, err = self.cli("owner", "t-0001", "--clear", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("cleared from what waits on the owner", out)
        self.assertNotIn("still open", out)
        code, out, err = self.cli("start", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("waiting on you: 0", out)

    def test_clear_leaves_an_open_decision_and_says_so(self) -> None:
        # The reviewer's case: the item cleared, the line stayed, because the task also
        # carried an unanswered decision. The command must not say only "cleared".
        self.add_owner_item(
            "t-0001", "choose A or B", decision="14 days or 30 for the refund window?"
        )
        code, out, err = self.cli("owner", "t-0001", "--clear", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("cleared from what waits on the owner", out)
        self.assertIn("the recorded decision is still open", out)
        self.assertIn("report t-0001 --decide", out)

        code, out, err = self.cli("start", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("waiting on you: 1", out)
        self.assertIn("14 days or 30 for the refund window?", out)
        self.assertNotIn("choose A or B", out, "the item itself is gone")

    def test_decide_removes_the_open_decision(self) -> None:
        self.add_owner_item(
            "t-0001", "choose A or B", decision="14 days or 30 for the refund window?"
        )
        self.cli("owner", "t-0001", "--clear", env=self.fake_env())
        code, _, err = self.cli("report", "t-0001", "--decide", "14 days", env=self.fake_env())
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("start", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("waiting on you: 0", out)
        self.assertNotIn("14 days or 30", out)

    def test_clear_with_nothing_recorded_does_not_claim_a_clear(self) -> None:
        self.add_owner_item("t-0001", None, decision="14 days or 30?")
        code, out, err = self.cli("owner", "t-0001", "--clear", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("nothing was recorded to clear", out)
        self.assertNotIn("cleared from what waits on the owner", out)
        self.assertIn("the recorded decision is still open", out)
        code, out, err = self.cli("start", env=self.fake_env())
        self.assertEqual(code, 0, err)
        self.assertIn("waiting on you: 1", out)


if __name__ == "__main__":
    unittest.main()
