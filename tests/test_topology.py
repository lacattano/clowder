from __future__ import annotations

import unittest
from pathlib import Path

from clowder.errors import UsageError
from clowder.mux import AgentInfo, MuxResult
from clowder.topology import (
    agents_in_repo,
    derive_name,
    ensure_agent,
    inside,
    match_agent,
    sanitise,
)


def agent(name: str, cwd: str, pane: str = "w1:p1") -> AgentInfo:
    return AgentInfo(name=name, pane_id=pane, cwd=cwd, status="idle")


class StubMux:
    """Records what would have been created, without a multiplexer."""

    def __init__(self, agents: list[AgentInfo] | None = None, start_ok: bool = True):
        self.existing = list(agents or [])
        self.start_ok = start_ok
        self.made: list[AgentInfo] = []
        self.actions: list[str] = []
        self.last_cwd = ""
        self.pane = 100

    def list_agents(self, timeout_s: float = 15.0) -> list[AgentInfo]:
        return self.existing + self.made

    def split_pane(self, cwd, direction="right", focus=False, timeout_s=20.0) -> str:
        self.actions.append(f"split {cwd}")
        self.last_cwd = str(cwd)
        self.pane += 1
        return f"w9:p{self.pane}"

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


if __name__ == "__main__":
    unittest.main()
