"""What waits on the owner: one list, and the views that must agree with it."""

from __future__ import annotations

import unittest

from clowder import owner
from clowder.owner import DECISION, MERGE, WALKTHROUGH, waiting_on_owner
from clowder.state import (
    CHANGES_REQUESTED,
    CLOSED,
    DISPATCHED,
    OPEN,
    PASSED,
    REPORTED,
    Job,
    Task,
)
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
                    review_verdict=PASSED,
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
            [],
            [
                job(
                    reviewer="verifier",
                    review_commit="a" * 40,
                    review_verdict=PASSED,
                    handed_over_at=now_iso(),
                )
            ],
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

    def test_an_owner_item_on_a_closed_step_still_waits(self) -> None:
        # Seen 2026-10-27: three owner items sat on closed steps and vanished from both
        # views. A recorded question waits until it is cleared or answered; the step's
        # own status is irrelevant.
        his, _ = waiting_on_owner(
            [task(status=CLOSED, owner_item="choose A or B", owner_item_at=now_iso())],
            [],
        )
        self.assertEqual(self.kinds(his), [DECISION])
        self.assertIn("choose A or B", his[0].heading)

    def test_an_answered_decision_on_a_closed_step_waits_for_nobody(self) -> None:
        his, _ = waiting_on_owner(
            [
                task(
                    status=CLOSED,
                    open_decision="14 days or 30?",
                    decision_answer="14 days",
                    decision_answered_at=now_iso(),
                )
            ],
            [],
        )
        self.assertEqual(his, [])

    def test_the_age_is_the_item_s_age_not_the_step_s(self) -> None:
        # The step may be a week old; the question was asked two days ago, and the wait
        # he reads is the question's.
        his, _ = waiting_on_owner(
            [
                task(
                    status=CLOSED,
                    created_at="2026-10-17T09:00:00Z",
                    owner_item="choose A or B",
                    owner_item_at="2026-10-25T09:00:00Z",
                )
            ],
            [],
        )
        self.assertEqual(his[0].since, "2026-10-25T09:00:00Z")

    def test_an_owner_item_on_an_open_step_appears_once(self) -> None:
        for status in (DISPATCHED, REPORTED, CLOSED):
            his, _ = waiting_on_owner(
                [task(status=status, owner_item="choose A or B", owner_item_at=now_iso())],
                [],
            )
            self.assertEqual(len(his), 1, f"{status}: one item, one line")
            self.assertEqual(his[0].kind, DECISION)

    def test_clearing_or_deciding_removes_it(self) -> None:
        cleared = task(status=CLOSED, owner_item=None, owner_item_at=None)
        his, _ = waiting_on_owner([cleared], [])
        self.assertEqual(his, [], "cleared: nothing waits")

        decided = task(
            status=CLOSED,
            open_decision="14 days or 30?",
            decision_answer="14 days",
            decision_answered_at=now_iso(),
        )
        his, _ = waiting_on_owner([decided], [])
        self.assertEqual(his, [], "decided: nothing waits")

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
        self.assertEqual(owner.answered_by(his[0], 3), "type 3")
        self.assertTrue(his[0].waited, "the wait is measured from the record")

    def test_the_review_notes_come_from_the_caller(self) -> None:
        # The board passes its re-review notes in; start passes none. The count is the
        # same either way, because the kind does not change.
        base = job(
            reviewer="verifier",
            review_commit="a" * 40,
            review_verdict=PASSED,
            handed_over_at=now_iso(),
        )
        plain, _ = waiting_on_owner([], [base])
        moved, _ = waiting_on_owner([], [base], rereview={base.id: "b" * 40})
        self.assertEqual(self.kinds(plain), self.kinds(moved))
        self.assertIn("waiting for your walkthrough", plain[0].note)
        self.assertIn("moved to", moved[0].note)

    def test_a_passed_change_carries_the_detail_written_for_him(self) -> None:
        detail = (
            "The refund page now says what a customer gets back."
            + chr(10)
            + "Passing means one read-through."
            + chr(10)
            + "Evidence: the review at aaaaaaa and 371 tests."
        )
        his, not_his = waiting_on_owner(
            [],
            [
                job(
                    "j-0001",
                    reviewer="verifier",
                    review_commit="a" * 40,
                    handed_over_at=now_iso(),
                    review_verdict=PASSED,
                    review_detail=detail,
                )
            ],
        )
        self.assertEqual(self.kinds(his), [WALKTHROUGH])
        self.assertEqual(not_his, [])
        self.assertIn("what a customer gets back", his[0].detail or "")
        self.assertIn("Passing means one read-through.", his[0].detail or "")
        self.assertIn("Evidence: the review at aaaaaaa", his[0].detail or "")
        self.assertIn("pass it so it can be published", his[0].detail or "")

    def test_a_passed_change_with_no_detail_says_it_was_not_recorded(self) -> None:
        his, _ = waiting_on_owner(
            [],
            [
                job(
                    "j-0001",
                    reviewer="verifier",
                    review_commit="a" * 40,
                    handed_over_at=now_iso(),
                    review_verdict=PASSED,
                    owner_line="The reviewer's one line.",
                )
            ],
        )
        self.assertEqual(self.kinds(his), [WALKTHROUGH])
        detail = his[0].detail or ""
        self.assertIn("The review-ready detail was not recorded", detail)
        self.assertIn("What the reviewer said: The reviewer's one line.", detail)
        self.assertNotIn("Evidence", detail, "nothing is invented")

    def test_a_changes_requested_change_is_never_offered_as_a_pass(self) -> None:
        reviewed = job(
            "j-0001",
            reviewer="verifier",
            review_commit="a" * 40,
            handed_over_at=now_iso(),
            review_verdict=CHANGES_REQUESTED,
        )
        his, not_his = waiting_on_owner([], [reviewed])
        self.assertEqual(his, [], "not his to pass")
        self.assertEqual(len(not_his), 1)
        self.assertIn("sent it back", not_his[0].note)
        self.assertEqual(reviewed.state_in_words, "changes requested")

    def test_a_review_with_no_recorded_verdict_is_not_offered(self) -> None:
        # d-0145: a review step that reported is not a pass. Without a verdict the
        # change is not put in front of him.
        reviewed = job(
            "j-0001",
            reviewer="verifier",
            review_commit="a" * 40,
            handed_over_at=now_iso(),
        )
        his, not_his = waiting_on_owner([], [reviewed])
        self.assertEqual(his, [])
        self.assertEqual(len(not_his), 1)
        self.assertIn("verdict was not recorded", not_his[0].note)
        self.assertEqual(reviewed.state_in_words, "review verdict not recorded")

    def test_an_owner_item_naming_the_pass_is_the_only_line(self) -> None:
        his, _ = waiting_on_owner(
            [
                task(
                    job="j-0001",
                    owner_item="reply pass on the refund change",
                    owner_item_at=now_iso(),
                )
            ],
            [
                job(
                    "j-0001",
                    reviewer="verifier",
                    review_commit="a" * 40,
                    handed_over_at=now_iso(),
                    review_verdict=PASSED,
                    review_detail="The detail.",
                )
            ],
        )
        self.assertEqual(self.kinds(his), [DECISION], "one decision, one line")


if __name__ == "__main__":
    unittest.main()
