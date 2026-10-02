"""`clowder agent rename` moves one name everywhere, or changes nothing."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clowder import cli, gitcmd
from clowder.errors import GitError
from clowder.mux import Mux
from clowder.state import (
    CLOSED,
    DISPATCHED,
    OPEN,
    REPORTED,
    Job,
    Queued,
    StateStore,
    Task,
)
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
        # The real repos ignore `.pi/` (the pane's remote-pi config) and
        # `.worktrees/` (the spaces). Without this, either would read as unsaved work.
        (path / ".gitignore").write_text(".pi/\n.worktrees/\n", encoding="utf-8")
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
                status=REPORTED,
                worktree=str(self.space),
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
        store.add_queued(Queued(id="q-0001", brief="b", repo="myrepo", why="w", agent="maker"))
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

    def seed_remote_pi(self, name: str = "maker", cwd: Path | None = None) -> None:
        path = (cwd or self.space) / ".pi" / "remote-pi" / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"agent_name": name, "auto_start_relay": True}, indent=2),
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

    def rename(self, old: str = "maker", new: str = "tancat-maker-1"):
        return self.cli("agent", "rename", old, "--to", new, env=self.mux_env())

    def agent_names(self) -> list[str]:
        state = json.loads(self.mux_state.read_text(encoding="utf-8"))
        return [agent["name"] for agent in state["agents"]]

    def remote_pi_name(self, cwd: Path) -> str:
        path = cwd / ".pi" / "remote-pi" / "config.json"
        return json.loads(path.read_text(encoding="utf-8"))["agent_name"]

    # -- the passing rename ------------------------------------------------

    def test_a_rename_moves_all_six_places(self) -> None:
        code, out, err = self.rename()
        self.assertEqual(code, 0, err)
        self.assertIn("tancat-maker-1", out)

        # 1. the mux agent
        self.assertIn("tancat-maker-1", self.agent_names())
        self.assertNotIn("maker", self.agent_names())

        # 2. the space folder
        new_space = self.repo / ".worktrees" / "tancat-maker-1"
        self.assertTrue(new_space.is_dir())
        self.assertFalse(self.space.exists())

        # 3. the state records, including the stored worktree paths
        store = StateStore(self.state)
        self.assertEqual(store.get("t-0001").agent, "tancat-maker-1")
        self.assertEqual(store.get_job("j-0001").agent, "tancat-maker-1")
        self.assertEqual(store.get("t-0001").worktree, str(new_space))
        self.assertEqual(store.get_job("j-0001").worktree, str(new_space))

        # 4. the remote-pi config, which moved with the folder
        self.assertEqual(self.remote_pi_name(new_space), "tancat-maker-1")

        # 5 and 6. the two shared documents
        roster = (self.workspace / "roster.md").read_text(encoding="utf-8")
        agents = (self.workspace / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("| tancat-maker-1 | builds |", roster)
        self.assertIn("- tancat-maker-1 builds code", agents)
        # A longer name that contains the old one is not touched.
        self.assertIn("| tancat-maker | another |", roster)

    def test_the_queued_item_is_renamed_too(self) -> None:
        code, _, err = self.rename()
        self.assertEqual(code, 0, err)
        self.assertEqual(StateStore(self.state).get_queued("q-0001").agent, "tancat-maker-1")

    def test_the_reviewer_name_is_renamed_too(self) -> None:
        store = StateStore(self.state)
        store.get_job("j-0001").reviewer = "maker"
        store.save()
        code, _, err = self.rename()
        self.assertEqual(code, 0, err)
        self.assertEqual(StateStore(self.state).get_job("j-0001").reviewer, "tancat-maker-1")

    def test_a_stale_worktree_path_is_repointed(self) -> None:
        # The task records the space the step ran in. After the folder moves, the
        # record must name the folder that exists, not the one that left.
        code, _, err = self.rename()
        self.assertEqual(code, 0, err)
        store = StateStore(self.state)
        for path in (store.get("t-0001").worktree, store.get_job("j-0001").worktree):
            self.assertIsNotNone(path)
            self.assertTrue(Path(str(path)).is_dir())

    def test_the_remote_pi_parent_and_suffix_are_renamed(self) -> None:
        self.seed_remote_pi("tancat-ai/maker#2")
        code, _, err = self.rename()
        self.assertEqual(code, 0, err)
        new_space = self.repo / ".worktrees" / "tancat-maker-1"
        self.assertEqual(self.remote_pi_name(new_space), "tancat-maker-1")

    def test_a_remote_pi_name_that_only_contains_the_old_name_is_left(self) -> None:
        # `tancat-maker` contains `maker` but is not `maker`, so it is not this
        # agent's name and must not change.
        self.seed_remote_pi("tancat-maker")
        code, _, err = self.rename()
        self.assertEqual(code, 0, err)
        new_space = self.repo / ".worktrees" / "tancat-maker-1"
        self.assertEqual(self.remote_pi_name(new_space), "tancat-maker")

    def test_an_agent_in_the_main_checkout_moves_no_folder(self) -> None:
        state = json.loads(self.mux_state.read_text(encoding="utf-8"))
        state["agents"][0]["cwd"] = str(self.repo)
        self.mux_state.write_text(json.dumps(state, indent=2), encoding="utf-8")
        self.seed_remote_pi("maker", cwd=self.repo)

        code, out, err = self.rename()
        self.assertEqual(code, 0, err)
        # No linked space, so no folder moved and the old space is untouched.
        self.assertTrue(self.space.is_dir())
        self.assertFalse((self.repo / ".worktrees" / "tancat-maker-1").exists())
        self.assertIn("not in a linked space", out)
        self.assertEqual(self.remote_pi_name(self.repo), "tancat-maker-1")
        self.assertEqual(StateStore(self.state).get("t-0001").agent, "tancat-maker-1")

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

        code, _, err = self.rename()
        self.assertEqual(code, 2)
        self.assertIn("already a live agent", err)

    def test_an_open_job_on_the_space_is_refused(self) -> None:
        store = StateStore(self.state)
        store.get_job("j-0001").status = OPEN
        store.save()

        code, _, err = self.rename()
        self.assertEqual(code, 1)
        self.assertIn("open job", err)
        self.assertIn("j-0001", err)

    def test_an_open_step_is_refused(self) -> None:
        store = StateStore(self.state)
        store.get("t-0001").status = DISPATCHED
        store.save()

        code, _, err = self.rename()
        self.assertEqual(code, 1)
        self.assertIn("open step", err)
        self.assertIn("t-0001", err)

    def test_a_dirty_space_is_refused(self) -> None:
        (self.space / "half-written.txt").write_text("x\n", encoding="utf-8")

        code, _, err = self.rename()
        self.assertEqual(code, 1)
        self.assertIn("unsaved work", err)
        # Nothing moved.
        self.assertTrue(self.space.is_dir())
        self.assertEqual(self.agent_names(), ["maker"])

    def test_a_target_folder_that_already_exists_is_refused(self) -> None:
        (self.repo / ".worktrees" / "tancat-maker-1").mkdir(parents=True)

        code, _, err = self.rename()
        self.assertEqual(code, 1)
        self.assertIn("folder already exists", err)
        self.assertTrue(self.space.is_dir())

    def test_a_target_branch_that_already_exists_is_refused(self) -> None:
        gitcmd.run_git(self.repo, "branch", "tancat-maker-1")

        code, _, err = self.rename()
        self.assertEqual(code, 1)
        self.assertIn("branch named", err)
        self.assertTrue(self.space.is_dir())

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

    # -- the rollback ------------------------------------------------------

    def test_a_mid_rename_failure_restores_every_place(self) -> None:
        real = Mux.list_agents
        calls = {"n": 0}

        def stale(self: Mux, timeout_s: float = 15.0):
            calls["n"] += 1
            live = real(self, timeout_s)
            if calls["n"] == 1:
                return live
            # The rename did not take: the new name is not in the live list.
            return [item for item in live if item.name != "tancat-maker-1"]

        with mock.patch.object(Mux, "list_agents", stale):
            code, _, err = self.rename()

        self.assertEqual(code, 1)
        self.assertIn("not in the live agent list", err)
        # 1. the mux agent is back
        self.assertEqual(self.agent_names(), ["maker"])
        # 2. the folder is back
        self.assertTrue(self.space.is_dir())
        self.assertFalse((self.repo / ".worktrees" / "tancat-maker-1").exists())
        # 3 and 4. the config and the documents are back
        self.assertEqual(self.remote_pi_name(self.space), "maker")
        self.assertIn("| maker | builds |", (self.workspace / "roster.md").read_text())
        self.assertIn("- maker builds code", (self.workspace / "AGENTS.md").read_text())
        # The state was never written.
        self.assertEqual(StateStore(self.state).get("t-0001").agent, "maker")

    def test_a_failed_undo_is_surfaced_not_swallowed(self) -> None:
        real_move = gitcmd.move_worktree
        moves = {"n": 0}

        def flaky(path, target):
            moves["n"] += 1
            if moves["n"] == 1:
                return real_move(path, target)
            raise GitError("the folder could not be put back")

        real = Mux.list_agents
        calls = {"n": 0}

        def stale(self: Mux, timeout_s: float = 15.0):
            calls["n"] += 1
            live = real(self, timeout_s)
            if calls["n"] == 1:
                return live
            return [item for item in live if item.name != "tancat-maker-1"]

        with (
            mock.patch.object(gitcmd, "move_worktree", flaky),
            mock.patch.object(Mux, "list_agents", stale),
        ):
            code, _, err = self.rename()

        self.assertEqual(code, 1)
        self.assertIn("could not be fully undone", err)
        self.assertIn("the folder could not be put back", err)


class ReplaceNameTest(unittest.TestCase):
    def test_a_shorter_name_does_not_touch_a_longer_one(self) -> None:
        text = "maker and tancat-maker and maker-1 and remaker"
        self.assertEqual(
            cli._replace_name(text, "maker", "tancat-maker-1"),
            "tancat-maker-1 and tancat-maker and maker-1 and remaker",
        )


if __name__ == "__main__":
    unittest.main()
