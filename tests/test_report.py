from __future__ import annotations

import unittest

from clowder.report import (
    build_report,
    format_cost,
    format_tokens,
    usage_breakdown,
    usage_line,
    worktree_label,
)
from clowder.sessions import Usage
from clowder.state import Task


def task(**overrides: object) -> Task:
    base: dict[str, object] = {
        "id": "t-0001",
        "question": "does the site need a refund policy before the review?",
        "brief": "scout: read the terms page",
        "shape": "scout",
        "agent": "verifier",
        "repo": "myrepo",
        "repo_path": "C:/code/myrepo",
        "dispatched_at": "2026-09-27T10:00:00Z",
        "reported_at": "2026-09-27T10:00:14Z",
    }
    base.update(overrides)
    return Task(**base)  # type: ignore[arg-type]


def usage(**overrides: object) -> Usage:
    base: dict[str, object] = {
        "turns": 2,
        "input_tokens": 7000,
        "output_tokens": 1200,
        "total_tokens": 8200,
        "cost_total": 0.001,
        "provider": "test-provider",
        "model": "test-model",
        "first_at": 1790512800.0,
        "last_at": 1790512814.0,
    }
    base.update(overrides)
    return Usage(**base)  # type: ignore[arg-type]


class FormatTest(unittest.TestCase):
    def test_tokens(self) -> None:
        self.assertEqual(format_tokens(950), "950")
        self.assertEqual(format_tokens(8200), "8.2k")
        self.assertEqual(format_tokens(1_240_000), "1.2M")

    def test_cost(self) -> None:
        self.assertEqual(format_cost(0), "$0")
        self.assertEqual(format_cost(0.001), "$0.0010")
        self.assertEqual(format_cost(1.5), "$1.50")
        self.assertEqual(format_cost(0.00001), "<$0.0001")

    def test_worktree_label(self) -> None:
        self.assertEqual(worktree_label(task()), "main checkout")
        self.assertEqual(
            worktree_label(task(worktree="C:/code/myrepo/.worktrees/refund-policy")),
            "worktree refund-policy",
        )


class UsageLineTest(unittest.TestCase):
    def test_full_line_matches_the_design(self) -> None:
        line = usage_line(task(), usage())
        self.assertEqual(
            line,
            "verifier | myrepo | main checkout | 14s | 8.2k | $0.0010 | test-model",
        )

    def test_line_without_a_session_says_so(self) -> None:
        self.assertIn("no usage yet", usage_line(task(), None))

    def test_branch_and_commit_name_the_work(self) -> None:
        labelled = task(branch="task/refund", commit="1a2b3c4")
        self.assertEqual(worktree_label(labelled), "task/refund @ 1a2b3c4")

    def test_a_branch_with_no_commit_yet(self) -> None:
        self.assertEqual(worktree_label(task(branch="task/refund")), "task/refund")

    def test_full_line_with_a_job(self) -> None:
        line = usage_line(task(branch="task/refund", commit="1a2b3c4"), usage())
        self.assertEqual(
            line,
            "verifier | myrepo | task/refund @ 1a2b3c4 | 14s | 8.2k | $0.0010 | test-model",
        )

    def test_breakdown_lists_the_parts(self) -> None:
        text = usage_breakdown(usage())
        self.assertIn("turns: 2", text)
        self.assertIn("test-provider", text)
        self.assertIn("test-model", text)
        self.assertTrue(text.startswith("    "))

    def test_breakdown_with_no_turns(self) -> None:
        self.assertEqual(usage_breakdown(Usage()), "no turns recorded")


class BuildReportTest(unittest.TestCase):
    def test_question_comes_first(self) -> None:
        text = build_report(task(), usage(), "Yes - the terms page is a placeholder.")
        first, second, third = text.splitlines()[:3]
        self.assertEqual(first, "Re: does the site need a refund policy before the review?")
        self.assertTrue(second.startswith("    verifier | myrepo |"))
        self.assertEqual(third, "    Yes - the terms page is a placeholder.")

    def test_open_decision_is_last_and_at_most_one(self) -> None:
        text = build_report(
            task(open_decision="accept 14 days, or wait?"), usage(), "the answer"
        )
        self.assertEqual(text.splitlines()[-1], "    Open decision: accept 14 days, or wait?")

    def test_missing_answer_is_stated_not_hidden(self) -> None:
        text = build_report(task(), None, None)
        self.assertIn("no answer yet", text)
        self.assertIn("14s ago", text)

    def test_multiline_answer_is_indented_per_line(self) -> None:
        text = build_report(task(), usage(), "line one\nline two")
        self.assertIn("\n    line one\n    line two\n", text + "\n")

    def test_no_open_decision_line_when_there_is_none(self) -> None:
        text = build_report(task(), usage(), "the answer")
        self.assertNotIn("Open decision", text)

    def test_a_long_answer_is_cut_and_names_the_full_escape_hatch(self) -> None:
        answer = "\n".join(f"line {index}" for index in range(1, 61))
        text = build_report(task(), usage(), answer)
        self.assertIn("    line 40", text)
        self.assertNotIn("line 41", text)
        self.assertIn("20 more line(s) hidden; read the whole answer with --full", text)

    def test_full_prints_the_whole_answer(self) -> None:
        answer = "\n".join(f"line {index}" for index in range(1, 61))
        text = build_report(task(), usage(), answer, full=True)
        self.assertIn("    line 60", text)
        self.assertNotIn("more line(s) hidden", text)

    def test_an_answer_at_the_limit_is_not_cut(self) -> None:
        answer = "\n".join(f"line {index}" for index in range(1, 41))
        text = build_report(task(), usage(), answer)
        self.assertIn("    line 40", text)
        self.assertNotIn("more line(s) hidden", text)


if __name__ == "__main__":
    unittest.main()
