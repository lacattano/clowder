"""`clowder agent rename` moves one name everywhere, or changes nothing."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from clowder import cli, gitcmd
from clowder.state import CLOSED, OPEN, Job, StateStore, Task
from tests.support import clean_env, make_mux_launcher, write_config, write_fake_state


class AgentRenameTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

        self.workspace = self.root / "ws"
        self.repo = self.workspace / "myrepo"
        self.repo.mkdir(parents=True)
        self.make_repo(self.repo)

        # The agent's space: a linked worktree named after it, as `clowder ensure`
        # makes one.
        self.space = self.repo / ".worktrees" / "maker"
        gitcmd.add_worktree_free(self.repo, self.space, "main")

        self.mux_state = self.root / "mux-state.json"
        write_fake_state(
            self.mux_state,
            agents=[
                {
                    "name": "maker",
                    "pane_id": "w9:p1",
                    "cwd": str(self.space),
                    "status": "idle",
                }
            ],
        )

        self.launcher = make_mux_launcher(self.root / "bin")
        self.state = self.root / "state.json"
        self.config = write_config(
            self.root / "clowder.config.toml",
            workspace={"root": str(self.workspace)},
            mux={"bin": str(self.launcher)},
            state={"path": str(self.state)},
        )
        self.seed_state()
        self.seed_docs()
        self.seed_remote_pi()

    # -- setup helpers -----------------------------------------------------

    @staticmethod
    def make_repo(path: Path) -> None:
        gitcmd.run_git(path, "init", "-b", "main")
        gitcmd.run_git(path, "config", "user.email", "test@example.com")
        gitcmd.run_git(path, "config", "user.name", "Test")
        (path / "readme.md").write_text("hello\n", encoding="utf-8")
        # The real repos ignore `.pi/`, which is where the pane's remote-pi config
        # lives. Without this, that local file would read as unsaved work.
        (path / ".gitignore").write_text(".pi/\n", encoding="utf-8")
        gitcmd.run_git(path, "add", "readme.md", ".gitignore")
        gitcmd.run_git(path, "commit", "-m", "first")

    def seed_state(self) -> None:
        store = StateStore(self.state)
        store.add(
            Task(
                id="t-0001",
                question="does the rename move the record?",
                brief="ship: x",
                shape="ship",
                agent="maker",
                repo="myrepo",
                repo_path=str(self.repo),
            )
        )
        store.add_job(
            Job(
                id="j-0001",
                label="a finished line of work",
                repo="myrepo",
                repo_path=str(self.repo),
                worktree=str(self.space),
                branch="task/x",
                base="main",
                agent="maker",
                status=CLOSED,
            )
        )
        store.save()

    def seed_docs(self) -> None:
        (self.workspace / "roster.md").write_text(
            "# roster\n\n| maker | builds |\n| tancat-maker | another |\n",
            encoding="utf-8",
        )
        (self.workspace / "AGENTS.md").write_text(
            "# AGENTS\n\n- maker builds code\n- the front door dispatches\n",
            encoding="utf-8",
        )

    def seed_remote_pi(self) -> None:
        path = self.space / ".pi" / "remote-pi" / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"agent_name": "maker", "auto_start_relay": True}, indent=2),
            encoding="utf-8",
        )

    # -- driving -----------------------------------------------------------

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

    def mux_env(self, **extra: object) -> dict[str, object]:
        return {
            "CLOWDER_FAKE_STATE": str(self.mux_state),
            "CLOWDER_FAKE_SESSION_DIR": str(self.root / "sessions"),
            **extra,
        }

    def agent_names(self) -> list[str]:
        state = json.loads(self.mux_state.read_text(encoding="utf-8"))
        return [agent["name"] for agent in state["agents"]]

    # -- the passing rename ------------------------------------------------

    def test_a_rename_moves_all_six_places(self) -> None:
        code, out, err = self.cli(
            "agent", "rename", "maker", "--to", "tancat-maker-1", env=self.mux_env()
        )
        self.assertEqual(code, 0, err)
        self.assertIn("tancat-maker-1", out)

        # 1. the mux agent
        self.assertIn("tancat-maker-1", self.agent_names())
        self.assertNotIn("maker", self.agent_names())

        # 2. the space folder
        new_space = self.repo / ".worktrees" / "tancat-maker-1"
        self.assertTrue(new_space.is_dir())
        self.assertFalse(self.space.exists())

        # 3. the state records
        store = StateStore(self.state)
        self.assertEqual(store.get("t-0001").agent, "tancat-maker-1")
        self.assertEqual(store.get_job("j-0001").agent, "tancat-maker-1")

        # 4. the remote-pi config, which moved with the folder
        config_path = new_space / ".pi" / "remote-pi" / "config.json"
        self.assertEqual(
            json.loads(config_path.read_text(encoding="utf-8"))["agent_name"],
            "tancat-maker-1",
        )

        # 5 and 6. the two shared documents
        roster = (self.workspace / "roster.md").read_text(encoding="utf-8")
        agents = (self.workspace / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("| tancat-maker-1 | builds |", roster)
        self.assertIn("- tancat-maker-1 builds code", agents)
        # A longer name that contains the old one is not touched.
        self.assertIn("| tancat-maker | another |", roster)

    def test_the_reviewer_name_is_renamed_too(self) -> None:
        store = StateStore(self.state)
        job = store.get_job("j-0001")
        job.reviewer = "maker"
        store.save()
        code, _, err = self.cli(
            "agent", "rename", "maker", "--to", "tancat-maker-1", env=self.mux_env()
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(StateStore(self.state).get_job("j-0001").reviewer, "tancat-maker-1")

    # -- the guards --------------------------------------------------------

    def test_an_unknown_name_is_refused(self) -> None:
        code, _, err = self.cli(
            "agent", "rename", "nobody", "--to", "tancat-nobody", env=self.mux_env()
        )
        self.assertEqual(code, 2)
        self.assertIn("no live agent named", err)
        self.assertIn("maker", err)

    def test_a_name_that_is_already_live_is_refused(self) -> None:
        state = json.loads(self.mux_state.read_text(encoding="utf-8"))
        state["agents"].append(
            {
                "name": "tancat-maker-1",
                "pane_id": "w9:p2",
                "cwd": str(self.repo),
                "status": "idle",
            }
        )
        self.mux_state.write_text(json.dumps(state, indent=2), encoding="utf-8")

        code, _, err = self.cli(
            "agent", "rename", "maker", "--to", "tancat-maker-1", env=self.mux_env()
        )
        self.assertEqual(code, 2)
        self.assertIn("already a live agent", err)

    def test_an_open_job_on_the_space_is_refused(self) -> None:
        store = StateStore(self.state)
        store.get_job("j-0001").status = OPEN
        store.save()

        code, _, err = self.cli(
            "agent", "rename", "maker", "--to", "tancat-maker-1", env=self.mux_env()
        )
        self.assertEqual(code, 1)
        self.assertIn("open job", err)
        self.assertIn("j-0001", err)

    def test_a_dirty_space_is_refused(self) -> None:
        (self.space / "half-written.txt").write_text("x\n", encoding="utf-8")

        code, _, err = self.cli(
            "agent", "rename", "maker", "--to", "tancat-maker-1", env=self.mux_env()
        )
        self.assertEqual(code, 1)
        self.assertIn("unsaved work", err)
        # Nothing moved.
        self.assertTrue(self.space.is_dir())
        self.assertEqual(self.agent_names(), ["maker"])

    def test_an_unusable_new_name_is_refused(self) -> None:
        code, _, err = self.cli(
            "agent", "rename", "maker", "--to", "Not A Name", env=self.mux_env()
        )
        self.assertEqual(code, 1)
        self.assertIn("not a usable agent name", err)

    def test_the_same_name_is_refused(self) -> None:
        code, _, err = self.cli("agent", "rename", "maker", "--to", "maker", env=self.mux_env())
        self.assertEqual(code, 1)
        self.assertIn("already its own name", err)


class ReplaceNameTest(unittest.TestCase):
    def test_a_shorter_name_does_not_touch_a_longer_one(self) -> None:
        text = "maker and tancat-maker and maker-1 and remaker"
        self.assertEqual(
            cli._replace_name(text, "maker", "tancat-maker-1"),
            "tancat-maker-1 and tancat-maker and maker-1 and remaker",
        )


if __name__ == "__main__":
    unittest.main()
