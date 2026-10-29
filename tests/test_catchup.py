"""What he missed: only what is new, and a view that cannot be stale."""

from __future__ import annotations

import unittest

from clowder import catchup
from clowder.board import BoardData, render_board
from clowder.state import Job, Queued, Task


def job(job_id: str = "j-0001", **overrides: object) -> Job:
    base: dict[str, object] = {
        "id": job_id,
        "label": "refund",
        "repo": "myrepo",
        "repo_path": "C:/code/myrepo",
        "worktree": "C:/code/myrepo/.worktrees/maker",
        "branch": "task/refund",
        "base": "main",
        "agent": "maker",
        "title": "the refund page",
        "effect": "the refund page shows the policy",
        "merged_at": "2026-10-29T09:00:00Z",
    }
    base.update(overrides)
    return Job(**base)  # type: ignore[arg-type]


def task(task_id: str = "t-0001", **overrides: object) -> Task:
    base: dict[str, object] = {
        "id": task_id,
        "question": "add the refund page",
        "brief": "ship: add the refund page",
        "shape": "ship",
        "agent": "maker",
        "repo": "myrepo",
        "repo_path": "C:/code/myrepo",
        "dispatched_at": "2026-10-29T10:00:00Z",
    }
    base.update(overrides)
    return Task(**base)  # type: ignore[arg-type]


class ItemTest(unittest.TestCase):
    def test_what_landed_is_named_by_what_it_changes(self) -> None:
        items = catchup.landed([job()])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].text, "Now live: the refund page shows the policy")
        self.assertEqual(items[0].kind, catchup.LANDED)

    def test_a_job_that_has_not_merged_has_not_landed(self) -> None:
        self.assertEqual(catchup.landed([job(merged_at=None)]), [])

    def test_waiting_lines_carry_the_word_that_answers_them(self) -> None:
        held = job(
            id="j-0002",
            merged_at=None,
            pass_at="2026-10-29T09:30:00Z",
            published_at="2026-10-29T09:40:00Z",
        )
        items = catchup.waiting([], [held])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].answer, "reply merge")
        self.assertEqual(items[0].kind, catchup.WAITING)
        self.assertIn(" - reply merge", items[0].line)
        # A recorded question is answered by its number on the list.
        asked = catchup.waiting([task(owner_item="Which page first?", owner_item_at="x")], [])
        self.assertEqual(asked[0].answer, "type 1")

    def test_running_and_queued_say_what_and_why(self) -> None:
        running = catchup.running([task()])
        self.assertIn("add the refund page", running[0].text)
        self.assertIsNone(running[0].answer)
        queued = catchup.queued(
            [
                Queued(
                    id="q-0001",
                    brief="b",
                    question="read the terms",
                    repo="myrepo",
                    why="the space is busy",
                )
            ]
        )
        self.assertIn("not sent yet: the space is busy", queued[0].text)


class PlainQuestionTest(unittest.TestCase):
    def test_a_handle_and_a_shape_word_are_not_his_nouns(self) -> None:
        self.assertEqual(
            catchup.plain_question("ship (t-1016): make the board show what changed"),
            "make the board show what changed",
        )
        self.assertEqual(
            catchup.plain_question("scout: read the terms page"),
            "read the terms page",
        )
        self.assertEqual(
            catchup.plain_question("add the refund page, t-0001"),
            "add the refund page",
        )

    def test_an_english_sentence_that_starts_with_a_shape_word_is_left_alone(self) -> None:
        # The strip is for the label, "ship:" or "scout:", not for the verb.
        self.assertEqual(
            catchup.plain_question("scout the area for the link"), "scout the area for the link"
        )
        self.assertEqual(catchup.plain_question("ship the refund page"), "ship the refund page")

    def test_a_question_that_is_only_a_handle_says_something(self) -> None:
        self.assertEqual(catchup.plain_question("t-1016"), "a step in flight")


class ReopenTest(unittest.TestCase):
    def test_two_reopens_in_one_second_are_two_news(self) -> None:
        # Timestamps have second resolution, so the count is what separates two reopens
        # that land in the same second - the CI failure this pins.
        import tempfile
        from pathlib import Path

        from clowder.defects import DefectStore

        with tempfile.TemporaryDirectory() as tmp:
            store = DefectStore(Path(tmp) / "defects.json")
            store.sighting("a bundle carries a machine name")
            store.sighting("a bundle carries a machine name")
            tokens = []
            for job_id in ("j-0001", "j-0002"):
                store.link("d-0001", job_id)
                store.unlink_job(job_id)
                defect = store.get("d-0001")
                items = catchup.at_risk([defect], [])
                tokens.append(items[0].token)
            self.assertNotEqual(tokens[0], tokens[1], "same second, different reopen")
            self.assertEqual(store.get("d-0001").reopen_seq, 2)


class SeenTest(unittest.TestCase):
    def test_a_line_is_new_until_it_is_read(self) -> None:
        items = [
            catchup.Item("landed:j-1", catchup.LANDED, "2026-10-29T09:00:00Z", "Now live: x")
        ]
        self.assertEqual(catchup.unseen(items, {}), items)
        seen = catchup.moments(items)
        self.assertEqual(catchup.unseen(items, seen), [], "read once, not shown again")

    def test_a_line_read_in_one_state_is_new_in_another(self) -> None:
        read = catchup.Item(
            "risk:d-1:2026-10-29T09:00:00Z",
            catchup.RISK,
            "2026-10-29T09:00:00Z",
            "a",
            state="open",
        )
        moved = catchup.Item(
            "risk:d-1:2026-10-29T09:00:00Z",
            catchup.RISK,
            "2026-10-29T09:00:00Z",
            "a",
            state="has-job",
        )
        seen = catchup.moments([read])
        self.assertEqual(catchup.unseen([moved], seen), [moved], "same moment, new state")

    def test_the_same_key_with_a_new_moment_is_new_again(self) -> None:
        old = catchup.Item("waiting:merge:j-1", catchup.WAITING, "2026-10-28T09:00:00Z", "a")
        again = catchup.Item("waiting:merge:j-1", catchup.WAITING, "2026-10-29T09:00:00Z", "a")
        seen = catchup.moments([old])
        self.assertEqual(catchup.unseen([again], seen), [again])

    def test_the_standing_picture_is_every_item(self) -> None:
        items = [
            catchup.Item("landed:j-1", catchup.LANDED, "2026-10-29T09:00:00Z", "Now live: x"),
            catchup.Item("queued:q-1", catchup.QUEUED, "2026-10-29T10:00:00Z", "y"),
        ]
        self.assertEqual(catchup.unseen(items, catchup.moments(items)), [])
        self.assertEqual(len(items), 2, "--all is the same list, unread or not")

    def test_the_note_says_new_or_nothing_new(self) -> None:
        self.assertIn("2 new since you last looked", catchup.summary_note(2, 5, None))
        quiet = catchup.summary_note(0, 5, "2026-10-29T10:00:00Z")
        self.assertIn("Nothing new", quiet)
        self.assertIn("--all", quiet)

    def test_the_groups_come_in_his_order(self) -> None:
        items = [
            catchup.Item("queued:q", catchup.QUEUED, "2026-10-29T10:00:00Z", "queued"),
            catchup.Item("landed:j", catchup.LANDED, "2026-10-29T09:00:00Z", "landed"),
            catchup.Item("running:t", catchup.RUNNING, "2026-10-29T11:00:00Z", "running"),
        ]
        headings = [heading for heading, _ in catchup.groups(items)]
        self.assertEqual(
            headings,
            ["What landed", "Running now", "Queued next"],
        )


class BoardTest(unittest.TestCase):
    def test_the_page_opens_with_the_catch_up(self) -> None:
        data = BoardData(
            catchup=["What landed:", "  - Now live: the refund page shows the policy"],
            catchup_note="1 new since you last looked (never).",
            tasks=[task()],
        )
        html = render_board(data)
        first = html.index("Since you last looked")
        self.assertLess(first, html.index("For you"), "the catch-up is the first section")
        self.assertIn("Now live: the refund page shows the policy", html)
        self.assertIn("1 new since you last looked", html)

    def test_a_quiet_page_says_so_and_names_the_one_command(self) -> None:
        html = render_board(BoardData(catchup=[], catchup_note="Nothing new."))
        self.assertIn("Nothing new since your last read", html)
        self.assertIn("clowder catchup --all", html)
        self.assertIn("For you", html, "the record sections are still below it")


if __name__ == "__main__":
    unittest.main()
