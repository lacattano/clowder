"""What waits on the owner: one list, and the views that must agree with it."""

from __future__ import annotations

import unittest

from clowder import owner
from clowder.owner import DECISION, MERGE, WALKTHROUGH, waiting_on_owner
from clowder.state import CLOSED, DISPATCHED, OPEN, REPORTED, Job, Task
from clowder.timeutil import now_iso


def task(task_id: str = "t-0001", **overrides: object) -> Task:
    base: dict[str, object] = {
        "id": task_id,
        "question": "does the site need a refund policy?",
        "brief": "ship: add the refund page",
        "shape": "ship",
        "agent": "maker",
        "repo": "myrepo",
        "repo_path": "C:/code/myrepo",
        "status": REPORTED,
        "dispatched_at": now_iso(),
        "reported_at": now_iso(),
    }
    base.update(overrides)
    return Task(**base)  # type: ignore[arg-type]


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
        "status": OPEN,
        "created_at": now_iso(),
    }
    base.update(overrides)
    return Job(**base)  # type: ignore[arg-type]


class WaitingOnOwnerTest(unittest.TestCase):
    def kinds(self, his: list[owner.Waiting]) -> list[str]:
        return [item.kind for item in his]

    def test_the_three_kinds(self) -> None:
        his, not_his = waiting_on_owner(
            [task(open_decision="14 days or 30?")],
            [
                job("j-0001", pass_at=now_iso(), published_at=now_iso()),
                job(
                    "j-0002",
                    reviewer="verifier",
                    review_commit="a" * 40,
                    handed_over_at=now_iso(),
                ),
            ],
        )
        self.assertEqual(self.kinds(his), [DECISION, MERGE, WALKTHROUGH])
        self.assertEqual(not_his, [])

    def test_a_walkthrough_alone_is_counted(self) -> None:
        # The j-0206 case: the old start counted decisions only and said zero while a
        # change waited for a walkthrough for a day.
        his, _ = waiting_on_owner(
            [], [job(reviewer="verifier", review_commit="a" * 40, handed_over_at=now_iso())]
        )
        self.assertEqual(self.kinds(his), [WALKTHROUGH])
        self.assertEqual(his[0].action, owner.ACTIONS[WALKTHROUGH])

    def test_nothing_waits_when_nothing_is_there(self) -> None:
        his, not_his = waiting_on_owner([], [])
        self.assertEqual((his, not_his), ([], []))

    def test_a_passed_but_unpublished_change_is_not_his(self) -> None:
        his, not_his = waiting_on_owner([], [job(pass_at=now_iso())])
        self.assertEqual(his, [])
        self.assertEqual(len(not_his), 1)
        self.assertIn("publishes it and CI runs there", not_his[0].note)

    def test_a_job_awaiting_ci_after_the_word_is_not_his(self) -> None:
        his, not_his = waiting_on_owner(
            [],
            [job(pass_at=now_iso(), published_at=now_iso(), merge_word_at=now_iso())],
        )
        self.assertEqual(his, [])
        self.assertEqual(len(not_his), 1)
        self.assertIn("when CI is green", not_his[0].note)

    def test_a_job_with_an_open_step_is_not_his_yet(self) -> None:
        his, not_his = waiting_on_owner(
            [task(status=DISPATCHED, job="j-0001")],
            [job("j-0001", pass_at=now_iso(), published_at=now_iso())],
        )
        self.assertEqual(his, [], "the step is still running")

    def test_an_owner_item_naming_the_merge_is_the_only_line(self) -> None:
        his, _ = waiting_on_owner(
            [
                task(
                    job="j-0001",
                    owner_item="merge the refund change",
                    owner_item_at=now_iso(),
                )
            ],
            [job(pass_at=now_iso(), published_at=now_iso())],
        )
        self.assertEqual(self.kinds(his), [DECISION], "one decision, one line")

    def test_an_item_about_another_thing_does_not_hide_the_merge(self) -> None:
        his, _ = waiting_on_owner(
            [task(job="j-0001", owner_item="choose the colours", owner_item_at=now_iso())],
            [job(pass_at=now_iso(), published_at=now_iso())],
        )
        self.assertEqual(self.kinds(his), [DECISION, MERGE])

    def test_a_closed_task_waits_for_nobody(self) -> None:
        his, _ = waiting_on_owner(
            [task(status=CLOSED, owner_item="choose A or B", open_decision="14 days or 30?")],
            [],
        )
        self.assertEqual(his, [])

    def test_the_oldest_waits_first(self) -> None:
        his, _ = waiting_on_owner(
            [
                task("t-0001", owner_item="later", owner_item_at="2026-10-02T10:00:00Z"),
                task("t-0002", owner_item="earlier", owner_item_at="2026-10-01T10:00:00Z"),
            ],
            [],
        )
        self.assertEqual([item.heading for item in his], ["earlier", "later"])

    def test_every_line_carries_what_he_types_and_how_long(self) -> None:
        his, _ = waiting_on_owner([task(open_decision="14 days or 30?")], [])
        self.assertEqual(his[0].action, owner.ACTIONS[DECISION])
        self.assertTrue(his[0].waited, "the wait is measured from the record")

    def test_the_review_notes_come_from_the_caller(self) -> None:
        # The board passes its re-review notes in; start passes none. The count is the
        # same either way, because the kind does not change.
        base = job(reviewer="verifier", review_commit="a" * 40, handed_over_at=now_iso())
        plain, _ = waiting_on_owner([], [base])
        moved, _ = waiting_on_owner([], [base], rereview={base.id: "b" * 40})
        self.assertEqual(self.kinds(plain), self.kinds(moved))
        self.assertIn("waiting for your walkthrough", plain[0].note)
        self.assertIn("moved to", moved[0].note)


if __name__ == "__main__":
    unittest.main()
