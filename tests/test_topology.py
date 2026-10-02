from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clowder import gitcmd
from clowder.errors import UsageError
from clowder.gitcmd import Identity
from clowder.mux import AgentInfo, MuxResult
from clowder.topology import (
    agents_in_repo,
    derive_name,
    ensure_agent,
    inside,
    match_agent,
    sanitise,
    workspace_for_repo,
    write_remote_pi_config,
)


def agent(
    name: str,
    cwd: str,
    pane: str = "w1:p1",
    workspace: str | None = None,
) -> AgentInfo:
    return AgentInfo(name=name, pane_id=pane, cwd=cwd, workspace_id=workspace, status="idle")


class StubMux:
    """Records what would have been created, without a multiplexer."""

    def __init__(
        self,
        agents: list[AgentInfo] | None = None,
        start_ok: bool = True,
        move_ok: bool = True,
    ):
        self.existing = list(agents or [])
        self.start_ok = start_ok
        self.move_ok = move_ok
        self.made: list[AgentInfo] = []
        self.actions: list[str] = []
        self.moves: list[tuple[str, str]] = []
        self.last_cwd = ""
        self.last_env: dict[str, str] | None = None
        self.pane = 100

    def list_agents(self, timeout_s: float = 15.0) -> list[AgentInfo]:
        return self.existing + self.made

    def split_pane(self, cwd, direction="right", focus=False, env=None, timeout_s=20.0) -> str:
        self.actions.append(f"split {cwd}")
        self.last_cwd = str(cwd)
        self.last_env = dict(env) if env else None
        self.pane += 1
        return f"w9:p{self.pane}"

    def move_pane(self, pane_id, workspace_id, focus=False, timeout_s=20.0) -> MuxResult:
        self.actions.append(f"move {pane_id} --workspace {workspace_id} --new-tab")
        if not self.move_ok:
            return MuxResult(
                argv=("herdr", "pane", "move"),
                returncode=4,
                stdout='{"error": {"code": "no_such_workspace", "message": "gone"}}',
                stderr="",
                duration_ms=5,
            )
        self.moves.append((pane_id, workspace_id))
        # A move gives the pane a new id in the target workspace, exactly as the
        # real multiplexer does. Holding the split's id afterwards would be stale.
        self.pane += 1
        moved = f"{workspace_id}:p{self.pane}"
        return MuxResult(
            argv=("herdr", "pane", "move"),
            returncode=0,
            stdout=json.dumps({"result": {"pane": {"pane_id": moved}}}),
            stderr="",
            duration_ms=5,
        )

    def start_agent(self, name, pane_id, kind="pi", timeout_s=None) -> MuxResult:
        self.actions.append(f"start {name} {kind} {pane_id}")
        if not self.start_ok:
            return MuxResult(
                argv=("herdr", "agent", "start"),
                returncode=4,
                stdout='{"error": {"code": "agent_not_ready", "message": "blocked"}}',
                stderr="",
                duration_ms=5,
            )
        self.made.append(
            AgentInfo(name=name, pane_id=pane_id, cwd=self.last_cwd, status="idle")
        )
        return MuxResult(
            argv=("herdr", "agent", "start"),
            returncode=0,
            stdout="{}",
            stderr="",
            duration_ms=5,
        )


class SanitiseTest(unittest.TestCase):
    def test_a_repo_with_a_dot_and_a_dash(self) -> None:
        self.assertEqual(sanitise("llama.cpp-strixhalo-fork"), "llama-cpp-strixhalo-fork")

    def test_uppercase_and_spaces(self) -> None:
        self.assertEqual(sanitise("Cat Tan Trading"), "cat-tan-trading")

    def test_a_name_that_starts_with_a_digit(self) -> None:
        # The multiplexer requires a name to start with a letter.
        self.assertEqual(sanitise("3d-printer"), "a3d-printer")

    def test_empty_input_still_yields_a_name(self) -> None:
        self.assertEqual(sanitise("..."), "repo")

    def test_length_is_capped(self) -> None:
        self.assertLessEqual(len(sanitise("x" * 200)), 32)


class DeriveNameTest(unittest.TestCase):
    def test_repo_and_role(self) -> None:
        self.assertEqual(derive_name("cat-tan-trading", "maker"), "cat-tan-trading-maker")

    def test_the_role_survives_a_long_repo_name(self) -> None:
        name = derive_name("x" * 60, "verifier")
        self.assertLessEqual(len(name), 32)
        self.assertTrue(name.endswith("-verifier"))

    def test_the_name_is_acceptable_to_the_multiplexer(self) -> None:
        for repo in ("llama.cpp-strixhalo-fork", "3d printer", "Cat Tan Trading"):
            name = derive_name(repo, "researcher")
            self.assertLessEqual(len(name), 32)
            self.assertRegex(name, r"^[a-z][a-z0-9_-]*$")


class InsideTest(unittest.TestCase):
    def test_a_subdirectory_is_inside(self) -> None:
        self.assertTrue(inside("C:/code/myrepo/.worktrees/refund", "C:/code/myrepo"))

    def test_the_same_directory_is_inside(self) -> None:
        self.assertTrue(inside("C:/code/myrepo", "C:/code/myrepo"))

    def test_a_sibling_is_not_inside(self) -> None:
        self.assertFalse(inside("C:/code/other", "C:/code/myrepo"))

    def test_a_prefix_that_is_not_a_directory_boundary(self) -> None:
        # "myrepo-old" must not count as being in "myrepo".
        self.assertFalse(inside("C:/code/myrepo-old", "C:/code/myrepo"))

    def test_an_empty_agent_directory_is_not_inside(self) -> None:
        self.assertFalse(inside("", "C:/code/myrepo"))


class MatchAgentTest(unittest.TestCase):
    def test_the_derived_name_wins(self) -> None:
        agents = [
            agent("maker", "C:/code/myrepo"),
            agent("myrepo-maker", "C:/code/myrepo"),
        ]
        chosen = match_agent(agents, "C:/code/myrepo", "myrepo", "maker")
        assert chosen is not None
        self.assertEqual(chosen.name, "myrepo-maker")

    def test_a_bare_role_name_is_reused(self) -> None:
        # How a crew is usually named by hand: maker, verifier, teacher.
        agents = [agent("verifier", "C:/code/myrepo")]
        chosen = match_agent(agents, "C:/code/myrepo", "myrepo", "verifier")
        assert chosen is not None
        self.assertEqual(chosen.name, "verifier")

    def test_a_role_suffix_is_reused(self) -> None:
        agents = [agent("tancat-verifier", "C:/code/myrepo")]
        chosen = match_agent(agents, "C:/code/myrepo", "myrepo", "verifier")
        assert chosen is not None
        self.assertEqual(chosen.name, "tancat-verifier")

    def test_the_only_agent_in_the_repo_is_used(self) -> None:
        agents = [agent("odd-name", "C:/code/myrepo")]
        chosen = match_agent(agents, "C:/code/myrepo", "myrepo", "teacher")
        assert chosen is not None
        self.assertEqual(chosen.name, "odd-name")

    def test_two_agents_and_no_role_match_is_left_to_the_human(self) -> None:
        agents = [
            agent("maker", "C:/code/myrepo"),
            agent("verifier", "C:/code/myrepo"),
        ]
        self.assertIsNone(match_agent(agents, "C:/code/myrepo", "myrepo", "teacher"))

    def test_an_agent_in_another_repo_is_never_matched(self) -> None:
        agents = [agent("maker", "C:/code/myrepo")]
        self.assertIsNone(match_agent(agents, "C:/code/other", "other", "maker"))

    def test_agents_in_repo_includes_a_worktree(self) -> None:
        agents = [
            agent("main-maker", "C:/code/myrepo"),
            agent("wt-maker", "C:/code/myrepo/.worktrees/refund"),
            agent("elsewhere", "C:/code/other"),
        ]
        found = [a.name for a in agents_in_repo(agents, "C:/code/myrepo")]
        self.assertEqual(found, ["main-maker", "wt-maker"])


class EnsureAgentTest(unittest.TestCase):
    def test_an_existing_agent_is_reused_and_nothing_is_created(self) -> None:
        mux = StubMux([agent("maker", "C:/code/myrepo")])
        result = ensure_agent(mux, "myrepo", "C:/code/myrepo", role="maker")
        self.assertTrue(result.ok)
        self.assertFalse(result.created)
        assert result.agent is not None
        self.assertEqual(result.agent.name, "maker")
        self.assertEqual(mux.actions, [])

    def test_a_missing_agent_is_created_in_the_repo(self) -> None:
        mux = StubMux([])
        result = ensure_agent(mux, "myrepo", "C:/code/myrepo", role="maker")
        self.assertTrue(result.ok)
        self.assertTrue(result.created)
        assert result.agent is not None
        self.assertEqual(result.agent.name, "myrepo-maker")
        self.assertEqual(Path(result.agent.cwd or ""), Path("C:/code/myrepo"))
        self.assertEqual(
            mux.actions,
            [f"split {Path('C:/code/myrepo')}", "start myrepo-maker pi w9:p101"],
        )

    def test_a_new_pane_gets_a_remote_pi_config_that_turns_the_relay_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "myrepo"
            repo.mkdir()
            gitcmd.run_git(repo, "init", "-b", "main")
            gitcmd.run_git(repo, "config", "user.email", "test@example.com")
            gitcmd.run_git(repo, "config", "user.name", "Test")
            (repo / "readme.md").write_text("x\n", encoding="utf-8")
            gitcmd.run_git(repo, "add", "readme.md")
            gitcmd.run_git(repo, "commit", "-m", "first")

            mux = StubMux([])
            result = ensure_agent(mux, "myrepo", repo, role="maker")
            self.assertTrue(result.ok)

            space = repo / ".worktrees" / "myrepo-maker"
            config = space / ".pi" / "remote-pi" / "config.json"
            self.assertTrue(config.is_file(), "the new pane can reach the relay")
            data = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(data["agent_name"], "myrepo-maker")
            self.assertIs(data["auto_start_relay"], True)

    def test_a_write_failure_leaves_no_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "myrepo"
            repo.mkdir()
            mux = StubMux([])
            with (
                mock.patch(
                    "clowder.topology.write_remote_pi_config",
                    side_effect=OSError("disk full"),
                ),
                self.assertRaises(OSError),
            ):
                ensure_agent(mux, "myrepo", repo, role="maker")
            self.assertEqual(mux.actions, [], "no pane was made")

    def test_the_crew_identity_reaches_the_new_panes_environment(self) -> None:
        identity = Identity("clowder-bot", "bot@example.com")
        mux = StubMux([])
        ensure_agent(mux, "myrepo", "C:/code/myrepo", role="maker", identity=identity)
        self.assertEqual(mux.last_env, identity.env())
        self.assertEqual(mux.last_env["GIT_AUTHOR_EMAIL"], "bot@example.com")
        self.assertEqual(mux.last_env["GIT_COMMITTER_NAME"], "clowder-bot")

    def test_no_identity_leaves_the_pane_environment_alone(self) -> None:
        mux = StubMux([])
        ensure_agent(mux, "myrepo", "C:/code/myrepo", role="maker")
        self.assertIsNone(mux.last_env)

    def test_a_repo_that_is_not_git_gets_no_worktree_and_says_so(self) -> None:
        # Nothing to work in: it uses the checkout itself, and reports the reason.
        mux = StubMux([])
        result = ensure_agent(mux, "myrepo", "C:/code/myrepo", role="maker")
        self.assertTrue(result.ok)
        self.assertIn("not a git repository", result.note or "")
        self.assertEqual(len(mux.actions), 2)

    def test_an_explicit_name_is_made_even_when_the_role_is_already_taken(self) -> None:
        # The bug a real run found: "make me tancat-maker" handed back the maker.
        mux = StubMux([agent("maker", "C:/code/myrepo")])
        result = ensure_agent(mux, "myrepo", "C:/code/myrepo", role="maker", name="myrepo-wt")
        self.assertTrue(result.ok)
        self.assertTrue(result.created)
        assert result.agent is not None
        self.assertEqual(result.agent.name, "myrepo-wt")
        self.assertTrue(mux.actions, "it made a space of its own")

    def test_an_explicit_name_that_already_exists_is_reused(self) -> None:
        mux = StubMux([agent("myrepo-wt", "C:/code/myrepo")])
        result = ensure_agent(mux, "myrepo", "C:/code/myrepo", role="maker", name="myrepo-wt")
        self.assertTrue(result.ok)
        self.assertFalse(result.created)
        self.assertEqual(mux.actions, [])

    def test_an_explicit_name_in_another_repo_is_not_reused(self) -> None:
        mux = StubMux([agent("myrepo-wt", "C:/code/elsewhere")])
        result = ensure_agent(mux, "myrepo", "C:/code/myrepo", role="maker", name="myrepo-wt")
        self.assertFalse(result.ok)
        self.assertIn("already exists", result.reason or "")

    def test_a_bad_explicit_name_is_refused(self) -> None:
        mux = StubMux([])
        with self.assertRaises(UsageError) as caught:
            ensure_agent(mux, "myrepo", "C:/code/myrepo", role="maker", name="Bad Name")
        self.assertIn("not a usable agent name", str(caught.exception))
        self.assertEqual(mux.actions, [])

    def test_creating_is_refused_when_not_allowed(self) -> None:
        mux = StubMux([])
        result = ensure_agent(mux, "myrepo", "C:/code/myrepo", role="maker", create=False)
        self.assertFalse(result.ok)
        self.assertIn("not allowed", result.reason or "")
        self.assertEqual(mux.actions, [])

    def test_a_live_name_from_another_repo_is_not_reused(self) -> None:
        # The name is taken by a pane that serves a different repo. Making a
        # second agent with one name would make the transport ambiguous.
        mux = StubMux([agent("myrepo-maker", "C:/code/other")])
        result = ensure_agent(mux, "myrepo", "C:/code/myrepo", role="maker")
        self.assertFalse(result.ok)
        self.assertIn("already exists", result.reason or "")
        self.assertEqual(mux.actions, [])

    def test_several_agents_and_no_match_asks_the_human(self) -> None:
        mux = StubMux([agent("maker", "C:/code/myrepo"), agent("verifier", "C:/code/myrepo")])
        result = ensure_agent(mux, "myrepo", "C:/code/myrepo", role="teacher")
        self.assertFalse(result.ok)
        self.assertEqual(result.candidates, ["maker", "verifier"])
        self.assertEqual(mux.actions, [])

    def test_a_failed_start_is_reported_and_nothing_is_claimed(self) -> None:
        mux = StubMux([], start_ok=False)
        result = ensure_agent(mux, "myrepo", "C:/code/myrepo", role="maker")
        self.assertFalse(result.ok)
        self.assertIn("agent_not_ready", result.reason or "")
        self.assertFalse(result.created)

    def test_created_but_absent_from_the_list_is_not_claimed(self) -> None:
        class ForgetfulMux(StubMux):
            def list_agents(self, timeout_s: float = 15.0) -> list[AgentInfo]:
                return self.existing

        mux = ForgetfulMux([])
        result = ensure_agent(mux, "myrepo", "C:/code/myrepo", role="maker")
        self.assertFalse(result.ok)
        self.assertTrue(result.created)
        self.assertIn("not in the live agent list", result.reason or "")

    def test_the_result_serialises(self) -> None:
        import json

        mux = StubMux([])
        result = ensure_agent(mux, "myrepo", "C:/code/myrepo", role="maker")
        json.dumps(result.as_dict())


class WriteRemotePiConfigTest(unittest.TestCase):
    def test_it_writes_the_name_and_turns_the_relay_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_remote_pi_config(tmp, "myrepo-maker")
            assert path is not None
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["agent_name"], "myrepo-maker")
            self.assertIs(data["auto_start_relay"], True)

    def test_it_keeps_settings_that_are_already_there(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".pi" / "remote-pi" / "config.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps({"relay": "http://relay", "agent_name": "old"}),
                encoding="utf-8",
            )
            write_remote_pi_config(tmp, "new")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["relay"], "http://relay")
            self.assertEqual(data["agent_name"], "new")
            self.assertIs(data["auto_start_relay"], True)

    def test_a_missing_folder_is_not_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope"
            self.assertIsNone(write_remote_pi_config(missing, "x"))
            self.assertFalse(missing.exists())

    def test_an_existing_false_is_kept(self) -> None:
        # A pane deliberately taken off the relay stays off.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".pi" / "remote-pi" / "config.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"auto_start_relay": False}), encoding="utf-8")
            write_remote_pi_config(tmp, "myrepo-maker")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIs(data["auto_start_relay"], False)
            self.assertEqual(data["agent_name"], "myrepo-maker")

    def test_a_new_file_turns_the_relay_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_remote_pi_config(tmp, "myrepo-maker")
            assert path is not None
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIs(data["auto_start_relay"], True)


class PanePlacementTest(unittest.TestCase):
    """Where a new pane lands: with the repo's peers, or beside the caller."""

    def test_a_new_agent_joins_a_peer_workspace_as_a_new_tab(self) -> None:
        mux = StubMux([agent("verifier", "C:/code/myrepo", workspace="w1")])
        result = ensure_agent(mux, "myrepo", "C:/code/myrepo", role="maker")
        self.assertTrue(result.ok)
        self.assertTrue(result.created)
        self.assertEqual(mux.moves, [("w9:p101", "w1")])
        self.assertEqual(
            mux.actions,
            [
                f"split {Path('C:/code/myrepo')}",
                "move w9:p101 --workspace w1 --new-tab",
                # The move renumbers the pane, so the agent starts in the new id,
                # not the id the split returned.
                "start myrepo-maker pi w1:p102",
            ],
        )
        assert result.agent is not None
        self.assertEqual(result.agent.pane_id, "w1:p102")

    def test_no_peer_in_the_repo_keeps_the_caller_split(self) -> None:
        mux = StubMux([])
        result = ensure_agent(mux, "myrepo", "C:/code/myrepo", role="maker")
        self.assertTrue(result.ok)
        self.assertEqual(mux.moves, [])
        self.assertNotIn("move", " ".join(mux.actions))

    def test_a_peer_in_another_repo_does_not_draw_the_pane_over(self) -> None:
        mux = StubMux([agent("other-maker", "C:/code/other", workspace="w7")])
        result = ensure_agent(mux, "myrepo", "C:/code/myrepo", role="maker")
        self.assertTrue(result.ok)
        self.assertEqual(mux.moves, [])

    def test_a_peer_with_no_workspace_reported_keeps_the_caller_split(self) -> None:
        # An older multiplexer may report no workspace. Split rather than guess.
        mux = StubMux([agent("verifier", "C:/code/myrepo")])
        result = ensure_agent(mux, "myrepo", "C:/code/myrepo", role="maker")
        self.assertTrue(result.ok)
        self.assertEqual(mux.moves, [])

    def test_a_failed_move_is_reported_and_the_agent_is_not_started(self) -> None:
        mux = StubMux([agent("verifier", "C:/code/myrepo", workspace="w1")], move_ok=False)
        result = ensure_agent(mux, "myrepo", "C:/code/myrepo", role="maker")
        self.assertFalse(result.ok)
        self.assertIn("could not move", result.reason or "")
        self.assertIn("no_such_workspace", result.reason or "")
        self.assertNotIn("start", " ".join(mux.actions))

    def test_the_workspace_choice_is_stable_when_two_peers_report_one(self) -> None:
        agents = [
            agent("zebra", "C:/code/myrepo", workspace="w2"),
            agent("alpha", "C:/code/myrepo", workspace="w1"),
        ]
        self.assertEqual(workspace_for_repo(agents, "C:/code/myrepo"), "w1")

    def test_a_peer_with_no_workspace_is_passed_over_for_one_that_has_it(self) -> None:
        agents = [
            agent("alpha", "C:/code/myrepo"),
            agent("beta", "C:/code/myrepo", workspace="w3"),
        ]
        self.assertEqual(workspace_for_repo(agents, "C:/code/myrepo"), "w3")


if __name__ == "__main__":
    unittest.main()
