from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from clowder.board import (
    FILTERABLE_FIELDS,
    BoardAgent,
    BoardData,
    _task_fields,
    model_rollup,
    open_tasks,
    recent_answers,
    render_board,
    waiting_for_worker,
    waiting_on_you,
    write_board,
)
from clowder.defects import Defect
from clowder.state import CLOSED, FAILED, REPORTED, Job, Queued, Task
from clowder.timeutil import now_iso


def task(task_id: str = "t-0001", **overrides: object) -> Task:
    base: dict[str, object] = {
        "id": task_id,
        "question": "does the site need a refund policy?",
        "brief": "scout: read the terms page",
        "shape": "scout",
        "agent": "verifier",
        "repo": "myrepo",
        "repo_path": "C:/code/myrepo",
        "job": "j-0001",
        "branch": "task/refund",
        "commit": "1a2b3c4",
        # Fresh by default, so a test only sees the quiet rule when it asks for it.
        "dispatched_at": now_iso(),
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
        "created_at": "2026-09-27T09:00:00Z",
    }
    base.update(overrides)
    return Job(**base)  # type: ignore[arg-type]


def queued(item_id: str = "q-0001", **overrides: object) -> Queued:
    base: dict[str, object] = {
        "id": item_id,
        "brief": "ship: add the refund page",
        "repo": "myrepo",
        "why": "the space holds an open job",
        "agent": "maker",
    }
    base.update(overrides)
    return Queued(**base)  # type: ignore[arg-type]


def dropped(item_id: str = "q-0009", **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "item": item_id,
        "question": "can the docker images be a tenth of their size?",
        "reason": "already merged under PR #27",
        "by": "topcat",
        "at": "2026-10-01T09:40:00Z",
        "repo": "myrepo",
    }
    base.update(overrides)
    return base


def old_task(task_id: str = "t-0001", **overrides: object) -> Task:
    """A step that has been open long enough to be worth a second look."""
    return task(task_id, dispatched_at="2020-01-01T00:00:00Z", **overrides)


def agent(name: str = "maker", **overrides: object) -> BoardAgent:
    base: dict[str, object] = {
        "name": name,
        "pane_id": "w1:p1",
        "status": "idle",
        "space": "C:/code/myrepo/.worktrees/maker",
        "branch": "task/refund",
        "commit": "1a2b3c4",
    }
    base.update(overrides)
    return BoardAgent(**base)  # type: ignore[arg-type]


def data(**overrides: object) -> BoardData:
    base: dict[str, object] = {
        "tasks": [],
        "jobs": [],
        "agents": [],
        "state_path": "C:/state.json",
        "generated_at": "2026-09-27 10:00",
    }
    base.update(overrides)
    return BoardData(**base)  # type: ignore[arg-type]


class RenderTest(unittest.TestCase):
    def test_the_page_stands_alone(self) -> None:
        html = render_board(data())
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn("<title>clowder board</title>", html)
        self.assertIn("<style>", html, "the page carries its own styles")
        self.assertNotIn("http://", html.split("<body>")[0], "no external assets")
        self.assertNotIn("src=", html)

    def test_the_page_carries_no_control_characters(self) -> None:
        # A broken CSS escape in the Done header ("\25b8 ") was read by Python as
        # an octal control byte, so the browser showed a stray "b8"/"be". The page
        # must carry no control byte other than tab, newline or carriage return.
        html = render_board(data(jobs=[job()], tasks=[task()]))
        bad = sorted(
            {hex(ord(char)) for char in html if ord(char) < 32 and char not in "\t\n\r"}
        )
        self.assertEqual(bad, [], f"control characters in the page: {bad}")

    def test_the_done_header_reads_done_cleanly(self) -> None:
        html = render_board(data())
        self.assertIn("<span class='colhead'>Done</span>", html)
        self.assertNotIn("25b8", html, "no collapsed marker escape")
        self.assertNotIn("25be", html, "no open marker escape")

    def test_the_page_reloads_only_while_work_is_running(self) -> None:
        # Idle: no reload, and the page says so. While something is in flight: the
        # browser re-reads this file every 30s. The box draws nothing between commands,
        # so an idle reload is pure flicker and a render timer would be pure cost.
        idle = render_board(data())
        self.assertNotIn('<meta http-equiv="refresh"', idle)
        self.assertIn("Nothing is running, so this page does not reload itself.", idle)

        busy = render_board(data(tasks=[task(job=None)]))
        self.assertIn('<meta http-equiv="refresh" content="30">', busy)
        self.assertIn(
            '<meta http-equiv="refresh" content="30">',
            busy.split("<body>")[0],
            "the reload belongs in the head",
        )
        self.assertIn("reloads every 30s while work is running", busy)

    def test_the_page_shows_when_it_was_generated_and_when_it_last_changed(self) -> None:
        html = render_board(
            data(
                generated_at="2026-09-27 10:00",
                drawn_at="2026-09-27T10:00:00",
                last_change="2026-09-27 09:15",
            )
        )
        self.assertIn("generated 2026-09-27 10:00", html)
        self.assertIn("last change 2026-09-27 09:15", html)

    def test_a_page_with_nothing_in_flight_shows_no_frozen_age(self) -> None:
        # Drawn at 10:00, nothing recorded since 09:15: the page says both. "Nothing
        # has changed since 09:15" is honest; "generated 10:00" alone would read as
        # fresh when nothing has happened.
        html = render_board(
            data(
                generated_at="2026-09-27 10:00",
                drawn_at="2026-09-27T10:00:00",
                # Naive timestamps here: the page stores UTC and shows local, which a
                # test cannot pin without knowing the machine's zone.
                previous_draw="2026-09-27T09:15:00",
                last_change="2026-09-27 09:15",
            )
        )
        self.assertIn("Nothing has changed since 2026-09-27 09:15.", html)
        self.assertIn("does not reload itself", html)

    def test_every_section_is_present_even_when_empty(self) -> None:
        html = render_board(data())
        for heading in (
            "For you",
            "Running now",
            "Waiting for a worker",
            "Changed since you last opened this page",
            "To Do",
            "In Progress",
            "Done",
            "Agents and spaces",
            "Answers",
            "Dropped from the queue",
        ):
            self.assertIn(heading, html)
        self.assertIn("Nothing is recorded as waiting on you.", html)
        self.assertIn("Nothing is waiting for the front door.", html)
        self.assertIn("No queue item has been dropped.", html)

    def test_an_open_step_is_shown(self) -> None:
        html = render_board(data(tasks=[task(job=None)], agents=[agent()]))
        doing = html[html.index("rows-doing") : html.index("rows-done")]
        self.assertIn("t-0001", doing)
        self.assertIn("does the site need a refund policy?", doing)
        self.assertIn("in progress", doing)

    def test_an_answer_is_shown_with_its_usage(self) -> None:
        answered = task(
            status=REPORTED,
            answer="Yes - the terms page is a placeholder.",
            usage={"total_tokens": 8200, "cost_total": 0.001},
        )
        html = render_board(data(tasks=[answered]))
        self.assertIn("Yes - the terms page is a placeholder.", html)
        self.assertIn("8.2k", html)
        self.assertIn("$0.0010", html)

    def test_a_closed_job_is_marked_as_such(self) -> None:
        html = render_board(data(jobs=[job(status=CLOSED)]))
        self.assertIn("closed", html)
        self.assertIn("refund", html)

    def test_the_job_card_folds_the_owner_s_gates(self) -> None:
        # The pass and the merge word are off the card face now; they are folded
        # into the details line, so the record is never lost.
        passed = job(pass_at="2026-09-29T10:00:00Z", pass_by="lacattano")
        html = render_board(data(jobs=[passed]))
        self.assertIn("details", html)
        self.assertIn("pass yes", html)
        self.assertIn("word no", html)

    def test_a_queued_item_is_to_do_not_the_owner_s_work(self) -> None:
        html = render_board(data(queued=[queued()]))
        self.assertIn(
            "Nothing is recorded as waiting on you.", html, "the owner has nothing to do"
        )
        owner = html.split("For you", 1)[1].split("Running now", 1)[0]
        self.assertNotIn("q-0001", owner, "a queued item is not the owner's work")
        todo = html[html.index("rows-todo") : html.index("rows-doing")]
        self.assertIn("q-0001", todo)
        self.assertIn("the space holds an open job", todo)

    def test_the_worker_section_names_the_front_door(self) -> None:
        html = render_board(data(front_door_name="topcat"))
        self.assertIn("Nothing is waiting for topcat.", html)

    def test_the_front_door_name_is_used_when_its_list_is_not_empty(self) -> None:
        html = render_board(
            data(
                tasks=[old_task()],
                agents=[agent("verifier", status="idle")],
                front_door_name="topcat",
            )
        )
        self.assertIn("topcat chases them", html)

    def test_a_dropped_item_is_history_not_work(self) -> None:
        html = render_board(data(dropped=[dropped()]))
        self.assertIn("Dropped from the queue", html)
        history = html.split("Dropped from the queue", 1)[1]
        self.assertIn("q-0009", history)
        self.assertIn("already merged under PR #27", history)
        self.assertIn("topcat", history)
        worker = html.split("Waiting for a worker", 1)[1].split("To Do", 1)[0]
        self.assertNotIn("q-0009", worker, "a dropped item is not waiting for a worker")

    def test_the_drop_history_is_newest_first(self) -> None:
        html = render_board(
            data(
                dropped=[
                    dropped("q-0009", question="the first drop"),
                    dropped("q-0010", question="the second drop"),
                ]
            )
        )
        history = html.split("Dropped from the queue", 1)[1]
        self.assertLess(history.index("q-0010"), history.index("q-0009"))


class WaitingTest(unittest.TestCase):
    def test_a_decision_waits_on_the_human(self) -> None:
        waiting = waiting_on_you(data(tasks=[task(open_decision="14 days or 30?")]))
        self.assertEqual(len(waiting), 1)
        self.assertIn("decision", waiting[0])
        self.assertIn("14 days or 30?", waiting[0])
        self.assertIn("t-0001", waiting[0])

    def test_an_answered_decision_does_not_wait(self) -> None:
        answered = task(
            open_decision="14 days or 30?",
            decision_answer="14 days",
            decision_answered_at="2026-09-29T10:00:00Z",
        )
        self.assertEqual(waiting_on_you(data(tasks=[answered])), [])

    def test_an_owner_item_waits_on_the_human(self) -> None:
        waiting = waiting_on_you(
            data(
                tasks=[
                    task(
                        owner_item="the team-page design is ready, in this chat",
                        owner_item_at=now_iso(),
                    )
                ]
            )
        )
        self.assertEqual(len(waiting), 1)
        self.assertIn("your move", waiting[0])
        self.assertIn("the team-page design is ready", waiting[0])
        self.assertIn("[t-0001]", waiting[0])
        self.assertLess(
            waiting[0].index("the team-page design"),
            waiting[0].index("[t-0001]"),
            "the name comes before the handle",
        )

    def test_a_long_owner_item_is_shown_in_full(self) -> None:
        item = "read every word of this: " + "x" * 600
        waiting = waiting_on_you(data(tasks=[task(owner_item=item, owner_item_at=now_iso())]))
        self.assertEqual(len(waiting), 1)
        self.assertIn("x" * 600, waiting[0], "the owner item is not clipped")

    def test_an_owner_item_with_a_detail_renders_both_in_full(self) -> None:
        detail = "A: 14 days, costs X.\nB: 30 days, costs Y.\n" + "Recommend A. " * 60
        waiting = waiting_on_you(
            data(
                tasks=[
                    task(
                        owner_item="choose A or B",
                        owner_item_at=now_iso(),
                        owner_detail=detail,
                    )
                ]
            )
        )
        self.assertEqual(len(waiting), 1)
        self.assertIn("choose A or B", waiting[0], "the one line is still first")
        self.assertIn("owner-detail", waiting[0])
        self.assertIn("A: 14 days, costs X.", waiting[0])
        self.assertIn("costs Y.", waiting[0])
        self.assertIn("Recommend A. " * 60, waiting[0], "the full detail, not a clip")
        self.assertIn(
            "costs X." + chr(10) + "B: 30 days",
            waiting[0],
            "the detail keeps its own line breaks instead of being flattened",
        )

    def test_an_owner_item_without_a_detail_has_no_detail_body(self) -> None:
        waiting = waiting_on_you(
            data(tasks=[task(owner_item="choose A or B", owner_item_at=now_iso())])
        )
        self.assertEqual(len(waiting), 1)
        self.assertIn("choose A or B", waiting[0])
        self.assertNotIn("owner-detail", waiting[0])

    def test_a_long_open_decision_is_shown_in_full(self) -> None:
        decision = "choose one: " + "y" * 600
        waiting = waiting_on_you(data(tasks=[task(open_decision=decision)]))
        self.assertEqual(len(waiting), 1)
        self.assertIn("y" * 600, waiting[0], "the decision is not clipped")

    def test_a_long_question_beside_a_decision_is_shown_in_full(self) -> None:
        question = "what should we do about " + "z" * 600
        waiting = waiting_on_you(
            data(tasks=[task(open_decision="choose one", question=question)])
        )
        self.assertEqual(len(waiting), 1)
        self.assertIn(
            "z" * 600, waiting[0], "the question the decision is about is not clipped"
        )

    def test_owner_items_are_numbered_in_order(self) -> None:
        first = task("t-0001", owner_item="choose A or B", owner_item_at="2026-10-01T10:00:00Z")
        second = task(
            "t-0002", owner_item="choose C or D", owner_item_at="2026-10-01T11:00:00Z"
        )
        waiting = waiting_on_you(data(tasks=[second, first]))
        # The item is the heading: numbered, then the line he reads, then the
        # "your move" note under it. The class is what makes it a heading.
        self.assertIn("<b>1.</b> <span class='owner-title'>choose A or B</span>", waiting[0])
        self.assertIn("<b>2.</b> <span class='owner-title'>choose C or D</span>", waiting[1])

    def test_an_uncheckable_commit_is_a_worker_note_not_a_warning(self) -> None:
        notes = data(tasks=[task()], risk_notes={"t-0001": "unknown"})
        self.assertEqual(waiting_on_you(notes), [], "could not check is not work at risk")
        worker = " ".join(waiting_for_worker(notes))
        self.assertIn("could not check", worker)
        self.assertIn("t-0001", worker)

    def test_an_open_decision_is_numbered_too(self) -> None:
        waiting = waiting_on_you(data(tasks=[task(open_decision="14 days or 30?")]))
        self.assertIn("<b>1.</b>", waiting[0])
        self.assertIn("decision", waiting[0])
        self.assertIn("14 days or 30?", waiting[0])

    def test_owner_items_and_decisions_share_one_numbering(self) -> None:
        item = task("t-0001", owner_item="choose A or B", owner_item_at="2026-10-01T10:00:00Z")
        decision = task(
            "t-0002", open_decision="14 days or 30?", created_at="2026-10-01T11:00:00Z"
        )
        waiting = waiting_on_you(data(tasks=[item, decision]))
        self.assertIn("<b>1.</b>", waiting[0])
        self.assertIn("choose A or B", waiting[0])
        self.assertIn("<b>2.</b>", waiting[1])
        self.assertIn("14 days or 30?", waiting[1])

    def test_a_merge_and_an_empty_change_are_named(self) -> None:
        merge = " ".join(
            waiting_for_worker(data(tasks=[task()], risk_notes={"t-0001": "merge"}))
        )
        self.assertIn("merge commit", merge)
        self.assertNotIn("could not check", merge)
        empty = " ".join(
            waiting_for_worker(data(tasks=[task()], risk_notes={"t-0001": "empty"}))
        )
        self.assertIn("empty change", empty)
        self.assertNotIn("could not check", empty)

    def test_an_owner_item_naming_the_merge_is_the_only_line(self) -> None:
        # One decision, one line: an item that names the merge stands in for the
        # derived "your word" line on the same job.
        passed = job(pass_at=now_iso(), published_at=now_iso())
        item = task("t-0001", status=REPORTED, owner_item="merge the refund change")
        waiting = waiting_on_you(data(jobs=[passed], tasks=[item]))
        self.assertEqual(len(waiting), 1)
        self.assertIn("owner-title", waiting[0])
        self.assertNotIn("your word", waiting[0])

    def test_an_item_about_another_thing_does_not_hide_the_merge_line(self) -> None:
        # The merge word is still his to give, so it must still be shown. Only an
        # item that names the merge stands in for it; a false hit would hide a
        # decision, which is worse than showing one twice.
        passed = job(pass_at=now_iso(), published_at=now_iso())
        item = task("t-0001", status=REPORTED, owner_item="choose the colours")
        waiting = waiting_on_you(data(jobs=[passed], tasks=[item]))
        self.assertEqual(len(waiting), 2)
        self.assertIn("choose the colours", " ".join(waiting))
        self.assertIn("your word", " ".join(waiting))

    def test_a_defect_seen_twice_waits_for_the_front_door_not_the_owner(self) -> None:
        # Opening the job is the front door's act, so this list belongs in the
        # worker section. One line and an id: the detail stays in the record.
        defect = Defect(
            id="d-0001",
            line="a step does not close when it reports",
            first_seen="2026-10-24T09:00:00Z",
            last_seen="2026-10-25T09:00:00Z",
            count=2,
            state="open",
        )
        page = data(repeated_defects=[defect])
        self.assertEqual(waiting_on_you(page), [], "none of this is the owner's act")
        worker = " ".join(waiting_for_worker(page))
        self.assertIn("seen twice, no job", worker)
        self.assertIn("d-0001", worker)
        self.assertIn("a step does not close when it reports", worker)
        self.assertIn("not enough", worker, "bumping the count is refused as a fix")

    def test_a_cleared_owner_item_does_not_wait(self) -> None:
        self.assertEqual(waiting_on_you(data(tasks=[task()])), [])

    def test_a_pass_without_a_word_waits_with_no_reviewer(self) -> None:
        passed = job(pass_at=now_iso())
        waiting = waiting_on_you(data(jobs=[passed]))
        self.assertEqual(len(waiting), 1)
        self.assertIn("your word", waiting[0])
        self.assertIn("[j-0001]", waiting[0])

    def test_a_reviewed_job_waits_for_the_owner_s_word(self) -> None:
        reviewed = job(reviewer="verifier", review_commit="a" * 40, pass_at=now_iso())
        waiting = waiting_on_you(data(jobs=[reviewed], tasks=[task(status=REPORTED)]))
        self.assertEqual(len(waiting), 1)
        self.assertIn("your word", waiting[0])
        self.assertIn("verifier", waiting[0])
        self.assertIn("the front door merges it", waiting[0])
        self.assertIn("merge word", waiting[0])

    def test_a_held_change_waits_with_its_age(self) -> None:
        held = job(reviewer="verifier", review_commit="a" * 40, handed_over_at=now_iso())
        waiting = waiting_on_you(data(jobs=[held]))
        self.assertEqual(len(waiting), 1)
        self.assertIn("held for your review", waiting[0])
        self.assertIn("j-0001", waiting[0])
        self.assertIn("verifier", waiting[0])
        self.assertIn("waiting", waiting[0])
        self.assertNotIn("gone quiet", waiting[0], "a fresh hold is not flagged")

    def test_a_held_change_that_has_sat_is_flagged(self) -> None:
        held = job(
            reviewer="verifier",
            review_commit="a" * 40,
            handed_over_at="2020-01-01T00:00:00Z",
        )
        waiting = waiting_on_you(data(jobs=[held]))
        self.assertEqual(len(waiting), 1)
        self.assertIn("held for your review", waiting[0])
        self.assertIn("gone quiet", waiting[0])

    def test_a_held_change_that_moved_after_review_needs_a_rereview(self) -> None:
        # The reviewer checked an old commit; the branch has moved. The owner must
        # not be invited to walk the stale one.
        held = job(reviewer="verifier", review_commit="a" * 40, handed_over_at=now_iso())
        waiting = waiting_on_you(data(jobs=[held], rereview={held.id: "b" * 40}))
        self.assertEqual(len(waiting), 1)
        self.assertIn("needs re-review", waiting[0])
        self.assertNotIn("held for your review", waiting[0])
        self.assertIn("aaaaaaa", waiting[0], "the checked commit is named")
        self.assertIn("bbbbbbb", waiting[0], "the current tip is named")

    def test_the_jobs_table_shows_needs_rereview_when_the_tip_moved(self) -> None:
        held = job(reviewer="verifier", review_commit="a" * 40)
        html = render_board(data(jobs=[held], rereview={held.id: "b" * 40}))
        doing = html[html.index('id="rows-doing"') :]
        self.assertIn("needs re-review", doing)

    def test_the_row_data_state_matches_the_displayed_state_for_a_rereview(self) -> None:
        # The state filter reads data-state; the pill shows the words. They must be
        # the same, or selecting "needs re-review" matches no row.
        held = job(reviewer="verifier", review_commit="a" * 40)
        html = render_board(data(jobs=[held], rereview={held.id: "b" * 40}))
        doing = html[html.index('id="rows-doing"') :]
        row = re.search(r"<tr([^>]*)>", doing).group(1)
        self.assertIn('data-state="needs re-review"', row)
        self.assertIn(">needs re-review<", doing)

    def test_the_row_data_state_matches_the_displayed_state_for_the_equal_tip(self) -> None:
        held = job(reviewer="verifier", review_commit="a" * 40)
        html = render_board(data(jobs=[held]))
        doing = html[html.index('id="rows-doing"') :]
        row = re.search(r"<tr([^>]*)>", doing).group(1)
        self.assertIn('data-state="waiting on a walkthrough"', row)
        self.assertIn(">waiting on a walkthrough<", doing)

    def test_the_row_data_state_matches_review_unconfirmed(self) -> None:
        held = job(reviewer="verifier", review_commit="a" * 40)
        html = render_board(data(jobs=[held], review_unconfirmed={held.id}))
        doing = html[html.index('id="rows-doing"') :]
        row = re.search(r"<tr([^>]*)>", doing).group(1)
        self.assertIn('data-state="review unconfirmed"', row)
        self.assertIn(">review unconfirmed<", doing)

    def test_an_open_review_step_reads_under_review(self) -> None:
        # The owner must not be invited to walk while the reviewer is still going.
        held = job(reviewer="verifier", review_commit="a" * 40, handed_over_at=now_iso())
        html = render_board(data(jobs=[held], tasks=[task()]))
        doing = html[html.index("rows-doing") : html.index("rows-done")]
        self.assertIn("under review", doing)
        self.assertNotIn("waiting on a walkthrough", doing)

    def test_a_reported_review_step_returns_the_owner_word(self) -> None:
        held = job(reviewer="verifier", review_commit="a" * 40, handed_over_at=now_iso())
        html = render_board(data(jobs=[held], tasks=[task(status=REPORTED)]))
        doing = html[html.index("rows-doing") : html.index("rows-done")]
        self.assertIn("waiting on a walkthrough", doing)
        self.assertNotIn("under review", doing)

    def test_an_open_rereview_reads_under_review_not_the_merge_word(self) -> None:
        # j-0095: it showed waiting-on-your-merge-word while its re-review was open.
        passed = job(
            reviewer="verifier",
            review_commit="a" * 40,
            pass_at=now_iso(),
            published_at=now_iso(),
        )
        html = render_board(data(jobs=[passed], tasks=[task()]))
        doing = html[html.index("rows-doing") : html.index("rows-done")]
        self.assertIn("under review", doing)
        self.assertNotIn("waiting on your merge word", doing)

    def test_a_job_the_owner_has_waved_through_does_not_wait(self) -> None:
        reviewed = job(
            reviewer="verifier",
            review_commit="a" * 40,
            pass_at=now_iso(),
            merge_word="merge",
            merge_word_at="2026-09-29T10:00:00Z",
        )
        waiting = waiting_on_you(data(jobs=[reviewed], tasks=[task(status=REPORTED)]))
        self.assertEqual(
            waiting, [], "his word is given, so the merge is the front door's step"
        )

    def test_a_reviewed_job_with_an_open_step_does_not(self) -> None:
        reviewed = job(reviewer="verifier", review_commit="a" * 40)
        waiting = waiting_on_you(data(jobs=[reviewed], tasks=[task()]))
        self.assertEqual(waiting, [], "the step is still open, so nobody is waiting")

    def test_work_on_no_branch_is_flagged_as_at_risk(self) -> None:
        waiting = waiting_on_you(
            data(
                tasks=[task(worktree="C:/code/myrepo/.worktrees/maker")],
                stranded={"t-0001"},
            )
        )
        self.assertEqual(len(waiting), 1)
        self.assertIn("work at risk", waiting[0])
        self.assertIn("genuinely stranded", waiting[0])
        self.assertIn("on no branch", waiting[0])
        self.assertIn("To keep it", waiting[0])
        self.assertIn("branch keep-t-0001 1a2b3c4", waiting[0])

    def test_a_stranded_commit_names_the_change_it_belongs_to(self) -> None:
        waiting = waiting_on_you(
            data(
                tasks=[task(worktree="C:/code/myrepo/.worktrees/maker")],
                jobs=[job()],
                stranded={"t-0001"},
            )
        )
        self.assertEqual(len(waiting), 1)
        self.assertIn("the change refund", waiting[0])
        self.assertIn("[j-0001]", waiting[0])
        self.assertIn("To keep it", waiting[0])

    def test_a_quiet_agent_is_called_out(self) -> None:
        waiting = waiting_for_worker(
            data(tasks=[old_task()], agents=[agent("verifier", status="idle")])
        )
        self.assertEqual(len(waiting), 1)
        self.assertIn("no answer", waiting[0])
        self.assertIn("idle", waiting[0])

    def test_a_quiet_agent_that_is_working_does_not(self) -> None:
        waiting = waiting_for_worker(
            data(tasks=[old_task()], agents=[agent("verifier", status="working")])
        )
        self.assertEqual(waiting, [], "working is not stuck")

    def test_an_agent_that_vanished_is_called_out(self) -> None:
        waiting = waiting_for_worker(data(tasks=[old_task()], agents=[]))
        self.assertEqual(len(waiting), 1)
        self.assertIn("gone quiet", waiting[0])
        self.assertIn("not in the live agent list", waiting[0])

    def test_a_vanished_agent_is_not_blamed_when_the_list_is_unreadable(self) -> None:
        waiting = waiting_for_worker(data(tasks=[old_task()], agents=[], live_ok=False))
        self.assertEqual(waiting, [], "no crying wolf when the list cannot be read")

    def test_a_fresh_step_does_not(self) -> None:
        self.assertEqual(
            waiting_for_worker(data(tasks=[task(dispatched_at=now_iso())], agents=[agent()])),
            [],
        )

    def test_a_queued_item_is_a_to_do_card(self) -> None:
        # It no longer sits in the worker's bullet list; it is a card in To Do.
        html = render_board(data(queued=[queued()]))
        todo = html[html.index("rows-todo") : html.index("rows-doing")]
        self.assertIn("q-0001", todo)
        self.assertIn("maker", todo)
        self.assertIn("the space holds an open job", todo)

    def test_a_queued_item_does_not_wait_on_the_owner(self) -> None:
        self.assertEqual(waiting_on_you(data(queued=[queued()])), [])

    def test_a_quiet_agent_does_not_wait_on_the_owner(self) -> None:
        waiting = waiting_on_you(
            data(tasks=[old_task()], agents=[agent("verifier", status="idle")])
        )
        self.assertEqual(waiting, [], "the front door chases a quiet worker")

    def test_a_closed_job_never_waits(self) -> None:
        closed = job(status=CLOSED, reviewer="verifier", review_commit="a" * 40)
        self.assertEqual(waiting_on_you(data(jobs=[closed])), [])

    def test_the_four_sections_come_first_in_his_order(self) -> None:
        # His order of use: what waits on him, what is running, the crew's queue,
        # then what changed. The records follow, unchanged.
        html = render_board(
            data(
                tasks=[task(job=None)],
                jobs=[job()],
                queued=[queued()],
                agents=[agent()],
            )
        )
        order = [
            "For you",
            "Running now",
            "Waiting for a worker",
            "Changed since you last opened this page",
            "Filters",
            "To Do",
            "Agents and spaces",
            "Answers",
            "Dropped from the queue",
        ]
        positions = [html.index(item) for item in order]
        self.assertEqual(positions, sorted(positions), "the page reads in his order")

    def test_the_changed_section_is_capped_and_says_so(self) -> None:
        # The first draw of an old record can hold months of merges. The newest few are
        # what he can act on; the cut is named so it is never hidden.
        jobs = [
            job(
                f"j-{index:04d}",
                title=f"change {index}",
                merged_at=f"2026-10-0{index % 9 + 1}T10:00:00Z",
            )
            for index in range(20)
        ]
        html = render_board(data(jobs=jobs))
        section = html.split("Changed since you last opened this page", 1)[1].split(
            "</section>", 1
        )[0]
        self.assertIn("Showing the newest 12 of 20", section)
        self.assertEqual(section.count("<li>"), 12, "twelve lines, not twenty")
        self.assertNotIn("j-0001", section, "no job id in his section")

    def test_the_render_is_a_smoke_not_a_benchmark(self) -> None:
        # A generous ceiling, not a measurement: the point is that a page draw stays
        # interactive now that it reads no multiplexer and no git. A real benchmark
        # would flake on a loaded box, which is exactly when the page matters.
        import time

        big = data(
            tasks=[task(f"t-{index:04d}", job=None) for index in range(400)],
            jobs=[job(f"j-{index:04d}") for index in range(40)],
        )
        started = time.perf_counter()
        render_board(big)
        self.assertLess(time.perf_counter() - started, 5.0, "the render is a draw, not a run")


class FilterTest(unittest.TestCase):
    """The declarative field list, and the controls and data both come from it."""

    def rows(self, html: str) -> list[list[str]]:
        """The field names on every filterable row, in order."""
        found = []
        for chunk in re.findall(r"<tr([^>]*)>", html):
            names = re.findall(r"data-([a-z-]+)=", chunk)
            if names:
                found.append(names)
        return found

    def test_the_filterable_field_list_matches_the_rows(self) -> None:
        # A filter may not name a field the rows do not carry, so every card on
        # the page must expose exactly the declared fields.
        names = [field.name for field in FILTERABLE_FIELDS]
        self.assertTrue(names, "the column list is the point")
        html = render_board(
            data(
                tasks=[task(job=None)],
                jobs=[job(), job(job_id="j-0002")],
            )
        )
        rows = self.rows(html)
        self.assertGreaterEqual(len(rows), 3, "a job and a job-less step both carry them")
        for row in rows:
            self.assertEqual(row, names, "a row must expose exactly the declared fields")

    def test_the_controls_are_generated_from_the_same_list(self) -> None:
        names = {field.name for field in FILTERABLE_FIELDS}
        html = render_board(data(tasks=[task()], jobs=[job()]))
        controls = set(re.findall(r"data-filter=\"([a-z-]+)\"", html))
        self.assertTrue(controls)
        self.assertEqual(controls - names, set(), "a filter names a field not in the list")
        for wanted in ("repo", "kind", "status", "waiting-on-you", "blocked", "age"):
            self.assertIn(wanted, controls, f"the {wanted} filter is missing")
        self.assertIn("data-sort", html, "the sort is there")
        for field in FILTERABLE_FIELDS:
            if field.control == "date":
                continue
            self.assertIn(
                f"<span>{field.label}</span>", html, f"the {field.name} control is not labelled"
            )

    def test_a_filter_that_matches_nothing_shows_the_empty_state(self) -> None:
        # A filter that keeps nothing must leave an empty state on the page, not an
        # error and not a half-drawn table.
        html = render_board(data(tasks=[task()], jobs=[job()]))
        empties = re.findall(r"data-empty data-for=\"([a-z-]+)\"", html)
        self.assertEqual(sorted(empties), ["rows-doing", "rows-done", "rows-todo"])
        self.assertIn("Nothing matches those filters.", html)
        self.assertIn("empty.hidden = matched !== 0", html, "the script shows the empty state")

    def test_the_owner_section_is_first_and_is_not_filtered(self) -> None:
        html = render_board(data(tasks=[task()], jobs=[job()]))
        waiting = html.index("For you")
        self.assertLess(waiting, html.index("Filters"), "what waits on him stays first")
        self.assertLess(waiting, html.index("To Do"))
        # No filterable row, and no empty state, sits in his section.
        section = html[waiting : html.index("Filters")]
        self.assertNotIn("data-filter", section)
        self.assertNotIn("data-empty", section)

    def test_the_filters_are_client_side_only(self) -> None:
        html = render_board(data(tasks=[task()], jobs=[job()]))
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertNotIn("<link", html)
        self.assertNotIn("fetch(", html)
        self.assertNotIn("XMLHttpRequest", html)


class KanbanTest(unittest.TestCase):
    """The three columns, labels, the Done collapse, and the filter on the cards."""

    def test_the_three_columns_and_what_falls_in_each(self) -> None:
        html = render_board(
            data(
                queued=[queued("q-0001")],
                jobs=[
                    job("j-0001"),
                    job("j-0002", status=CLOSED, closed_at="2026-09-27T10:00:00Z"),
                ],
                tasks=[
                    task("t-0001", job=None),
                    task("t-0002", job=None, status=REPORTED, answer="done"),
                    task("t-0003", job=None, status="abandoned"),
                ],
            )
        )
        todo = html[html.index("rows-todo") : html.index("rows-doing")]
        doing = html[html.index("rows-doing") : html.index("rows-done")]
        done = html[html.index("rows-done") :]
        self.assertIn("q-0001", todo, "a queued item is To Do")
        self.assertIn("j-0001", doing, "an open job is In Progress")
        self.assertIn("t-0001", doing, "an open step with no job is In Progress")
        self.assertIn("j-0002", done, "a closed job is Done")
        self.assertIn("t-0002", done, "an answered step with no job is Done")
        self.assertIn("t-0003", done, "an abandoned step is kept in Done, never hidden")

    def test_a_step_closed_on_its_report_reads_answered(self) -> None:
        # A step that reports now closes itself, so a closed status alone would
        # read "closed, no answer came" - the opposite of what happened.
        html = render_board(
            data(
                tasks=[
                    task(
                        "t-0001",
                        job=None,
                        status=CLOSED,
                        answer="the refund page is missing",
                        closed_at="2026-09-27T10:00:00Z",
                    )
                ]
            )
        )
        done = html[html.index("rows-done") :]
        self.assertIn("answered", done)
        self.assertNotIn("no answer came", done)

    def test_a_job_and_its_step_are_one_card(self) -> None:
        html = render_board(data(jobs=[job("j-0001")], tasks=[task("t-0001", job="j-0001")]))
        doing = html[html.index("rows-doing") : html.index("rows-done")]
        self.assertIn("j-0001", doing)
        self.assertNotIn("t-0001", doing, "the step is folded into the job's card")

    def test_a_done_card_shows_no_reason(self) -> None:
        # A finished change waits on nothing, so its card carries no reason line.
        closed = job("j-0001", status=CLOSED, closed_at="2026-09-27T10:00:00Z")
        html = render_board(data(jobs=[closed], agents=[agent("maker", status="working")]))
        done = html[html.index("rows-done") :]
        self.assertNotIn("maker is working", done)
        self.assertNotIn("checking it", done)

    def test_a_failed_step_with_no_job_is_on_the_board(self) -> None:
        # A FAILED dispatch is not open, has no answer, and is neither abandoned
        # nor closed. It must still land somewhere, so a not-open job-less step is
        # Done - the same exhaustive bucket the jobs loop uses - and never missing
        # from the page.
        failed = task("t-0001", job=None, status=FAILED)
        html = render_board(data(tasks=[failed]))
        done = html[html.index("rows-done") :]
        self.assertIn("t-0001", done, "a failed job-less step must not disappear")
        self.assertIn(">failed<", done, "a stopped step is not called in progress")

    def test_the_collapsed_done_header_names_the_abandoned_count(self) -> None:
        # Abandoned work must not sit unseen behind the collapse, so the header
        # says how many stopped without an answer.
        gone = task("t-0001", job=None, status="abandoned")
        html = render_board(data(tasks=[gone]))
        done = html[html.index("column done") :]
        self.assertIn("count'>(1, 1 abandoned)</span>", done)

    def test_the_labels_are_recorded_and_coloured(self) -> None:
        html = render_board(
            data(
                queued=[queued(labels=["sev:blocker", "phase:pre-launch"])],
                jobs=[job(labels=["sev:high"])],
            )
        )
        self.assertIn("sev:blocker", html)
        self.assertIn("sev-blocker", html, "the sev: family colours the pill")
        self.assertIn("phase:pre-launch", html)
        self.assertIn("phase-pre-launch", html, "the phase: family colours the pill")
        self.assertIn("sev:high", html)
        self.assertIn("sev-high", html)
        # severity and phase are filter columns too, fed by the label.
        self.assertIn('data-filter="severity"', html)
        self.assertIn('data-filter="phase"', html)
        self.assertIn('data-severity="blocker"', html)
        self.assertIn('data-phase="pre-launch"', html)

    def test_the_done_column_is_collapsed_with_a_count_and_a_cap(self) -> None:
        closed = [
            job(f"j-{n:04d}", status=CLOSED, closed_at="2026-09-27T10:00:00Z")
            for n in range(1, 4)
        ]
        html = render_board(data(jobs=closed))
        done = html[html.index("column done") :]
        self.assertIn("<details>", done, "Done is collapsed by default")
        self.assertIn("colhead'>Done</span>", done)
        self.assertIn("count'>(3)</span>", done, "the count is visible with the collapse")
        self.assertIn('data-cap="20"', done)
        self.assertIn('data-order="newest"', done)
        self.assertIn("the filter reaches the rest", done)
        # Every card is on the page, so the filter can reach the rest of Done.
        for n in range(1, 4):
            self.assertIn(f"j-{n:04d}", done)

    def test_the_filter_reaches_every_card_and_lifts_the_cap(self) -> None:
        html = render_board(data(queued=[queued()], jobs=[job()]))
        rows = [row for row in re.findall(r"<tr([^>]*)>", html) if "data-repo=" in row]
        self.assertGreaterEqual(len(rows), 2, "the queued card and the job card")
        for row in rows:
            self.assertNotIn("data-card", row)
        # The cap is a default only: a filter is never capped, so it reaches the rest.
        self.assertIn("narrowed ? 0 : capOf(body)", html)
        self.assertIn("row.hidden = !show", html)


class ModelRollupTest(unittest.TestCase):
    """Per model: changes passed first time, and changes the verifier sent back."""

    def test_two_models_are_counted_correctly(self) -> None:
        # Two writers under different models. One of them had the change sent back,
        # which the record shows as the verifier running the review twice.
        passed = job(
            "j-0001", agent="maker", reviewer="verifier", pass_at="2026-09-27T10:00:00Z"
        )
        sent_back = job(
            "j-0002",
            agent="maker2",
            reviewer="verifier",
            pass_at="2026-09-27T11:00:00Z",
        )
        steps = [
            task(
                "t-0001",
                agent="maker",
                job="j-0001",
                model="big-model",
                status=REPORTED,
                reported_at="2026-09-27T10:00:00Z",
            ),
            task("t-0002", agent="verifier", job="j-0001", status=REPORTED),
            task(
                "t-0003",
                agent="maker2",
                job="j-0002",
                model="free-model",
                status=REPORTED,
                reported_at="2026-09-27T11:00:00Z",
            ),
            task("t-0004", agent="verifier", job="j-0002", status=REPORTED),
            # The same verifier, a second review on the same change: it went back.
            task("t-0005", agent="verifier", job="j-0002", status=REPORTED),
        ]
        rollup = model_rollup(data(tasks=steps, jobs=[passed, sent_back]))
        self.assertEqual(
            rollup,
            [("big-model", 1, 0), ("free-model", 0, 1)],
            "each model is counted in the column that happened to it",
        )

    def test_a_change_still_waiting_is_in_neither_column(self) -> None:
        waiting = job("j-0001", agent="maker", reviewer="verifier")
        steps = [
            task("t-0001", agent="maker", job="j-0001", model="big-model", status=REPORTED),
            task("t-0002", agent="verifier", job="j-0001", status=REPORTED),
        ]
        self.assertEqual(model_rollup(data(tasks=steps, jobs=[waiting])), [])

    def test_a_change_with_no_recorded_model_is_named_as_such(self) -> None:
        # Measurement, not invention: an unreadable model says so rather than
        # guessing one.
        merged = job("j-0001", agent="maker", pass_at="2026-09-27T10:00:00Z")
        steps = [task("t-0001", agent="maker", job="j-0001", status=REPORTED)]
        self.assertEqual(
            model_rollup(data(tasks=steps, jobs=[merged])),
            [("not recorded", 1, 0)],
        )

    def test_the_board_shows_the_rollup_and_the_model_on_an_answer(self) -> None:
        merged = job("j-0001", agent="maker", pass_at="2026-09-27T10:00:00Z")
        steps = [
            task(
                "t-0001",
                agent="maker",
                job="j-0001",
                model="free-model",
                status=REPORTED,
                answer="the change is ready",
                reported_at="2026-09-27T10:00:00Z",
            )
        ]
        html = render_board(data(tasks=steps, jobs=[merged]))
        self.assertIn("By model", html)
        self.assertIn("free-model", html)
        self.assertIn("passed first time", html)
        self.assertIn("nothing here gates anything", html, "the figure is not a gate")


class PlainNameTest(unittest.TestCase):
    """Work named in the owner's words, so he needs no handle to follow it."""

    def test_a_closed_job_does_not_read_in_progress(self) -> None:
        # The blocker: the derived state ignored status and closed_at, so a change
        # that had stopped read "in progress" on the board and in `job list --all`.
        closed = job(
            status=CLOSED,
            closed_at="2026-09-27T10:00:00Z",
            released_at="2026-09-27T10:00:00Z",
        )
        self.assertEqual(closed.state_in_words, "closed")
        self.assertNotEqual(closed.state_in_words, "in progress")
        # It does not hide either: the words are what the owner reads for history.
        html = render_board(data(jobs=[closed]))
        self.assertIn(">closed<", html.replace("'", '"'))
        self.assertIn(str(closed.id), html)

    def test_a_merged_job_still_reads_merged_not_closed(self) -> None:
        # Merging closes the job too, so "merged" must win over "closed".
        done = job(
            status=CLOSED,
            pass_at="2026-09-27T09:00:00Z",
            published_at="2026-09-27T09:30:00Z",
            merged_at="2026-09-27T10:00:00Z",
            closed_at="2026-09-27T10:00:00Z",
        )
        self.assertEqual(done.state_in_words, "merged")

    def test_every_state_a_row_can_carry_is_a_declared_choice(self) -> None:
        # A value a row produces that the control cannot name is a filter that
        # silently matches nothing. The declared list must cover all of them.
        declared = set(next(f for f in FILTERABLE_FIELDS if f.name == "state").values)
        produced = set()
        for one in (
            job(),
            job(status=CLOSED, closed_at="2026-09-27T10:00:00Z"),
            job(pass_at="2026-09-27T09:00:00Z"),
            job(reviewer="verifier", review_commit="a" * 40),
            job(pass_at="2026-09-27T09:00:00Z", published_at="2026-09-27T09:05:00:00Z"),
            job(merged_at="2026-09-27T10:00:00Z"),
        ):
            produced.add(one.state_in_words)
        produced.add(
            job(reviewer="verifier", review_commit="a" * 40).state_in_words_at("b" * 40)
        )
        produced.add(
            job(reviewer="verifier", review_commit="a" * 40).state_in_words_at(
                None, unreadable=True
            )
        )
        for step in (
            task(),
            task(task_id="t-0002", status=REPORTED),
            task(task_id="t-0003", status="abandoned"),
            task(task_id="t-0004", status="closed"),
            task(task_id="t-0005", status=FAILED),
        ):
            produced.add(_task_fields(step)["state"])
        self.assertEqual(
            produced - declared,
            set(),
            "a row can carry a state the control cannot name",
        )

    def test_the_card_still_filters_on_status(self) -> None:
        # The card face carries the words; the raw status is still on the row, so
        # the status filter still works.
        html = render_board(data(jobs=[job()]))
        doing = html[html.index('id="rows-doing"') :]
        row = re.search(r"<tr([^>]*)>", doing).group(1)
        self.assertIn('data-status="open"', row)
        self.assertIn('data-filter="status"', html)

    def test_state_in_words_is_derived_correctly(self) -> None:
        # Derived from the pass and merge word he actually gave, never stored.
        self.assertEqual(job().state_in_words, "in progress")
        self.assertEqual(
            job(reviewer="verifier", review_commit="a" * 40).state_in_words,
            "waiting on a walkthrough",
        )
        self.assertEqual(job(pass_at="2026-09-27T10:00:00Z").state_in_words, "passed")
        self.assertEqual(
            job(
                pass_at="2026-09-27T10:00:00Z", published_at="2026-09-27T10:05:00Z"
            ).state_in_words,
            "waiting on your merge word",
        )
        self.assertEqual(
            job(
                pass_at="2026-09-27T10:00:00Z",
                published_at="2026-09-27T10:05:00Z",
                merged_at="2026-09-27T10:30:00Z",
            ).state_in_words,
            "merged",
        )

    def test_a_job_with_no_title_falls_back_to_readable_words(self) -> None:
        # Never a bare id: the label is already words, and the effect falls back
        # to the branch rather than nothing.
        plain = job()
        self.assertEqual(plain.name_in_words, plain.label)
        self.assertNotEqual(plain.name_in_words, plain.id)
        self.assertEqual(plain.effect_in_words, plain.branch)

    def test_a_title_and_effect_are_used_when_given(self) -> None:
        named = job(title="the page-context fix", effect="a second page opens in context")
        self.assertEqual(named.name_in_words, "the page-context fix")
        self.assertEqual(named.effect_in_words, "a second page opens in context")

    def test_the_board_shows_the_title_before_the_identifier(self) -> None:
        named = job(
            title="the page-context fix",
            effect="a second page opens in context",
            pass_at="2026-09-27T10:00:00Z",
        )
        html = render_board(data(jobs=[named]))
        self.assertIn("the page-context fix", html)
        self.assertIn("a second page opens in context", html)
        self.assertIn("passed", html)
        # The title opens the card; the id follows on the identification line.
        doing = html[html.index("rows-doing") :]
        self.assertLess(
            doing.index("the page-context fix"),
            doing.index(named.id),
            "the title comes before the id",
        )

    def test_the_title_is_a_declared_filter_column(self) -> None:
        # q-0142's field list, reused: title and state are declared once, and
        # both the controls and the row data come from it.
        names = [field.name for field in FILTERABLE_FIELDS]
        self.assertIn("title", names)
        self.assertIn("state", names)
        named = job(title="the busy-board fix")
        html = render_board(data(jobs=[named]))
        doing = html[html.index('id="rows-doing"') :]
        row = re.search(r"<tr([^>]*)>", doing).group(1)
        self.assertIn('data-title="the busy-board fix"', row)
        self.assertIn('data-state="in progress"', row)
        for name in ("title", "state"):
            self.assertIn(f'data-filter="{name}"', html, f"the {name} control is missing")


class SafetyTest(unittest.TestCase):
    def test_agent_text_is_escaped(self) -> None:
        nasty = task(
            question="<script>alert('x')</script>",
            answer="<img src=x onerror=alert(1)>",
            open_decision="</li><li>injected",
        )
        html = render_board(data(tasks=[nasty], agents=[agent()]))
        # The page carries one script, its own filter loop. Nothing an agent wrote
        # may become a tag.
        self.assertEqual(html.count("<script>"), 1, "only the page's own filter script")
        self.assertNotIn("<script>alert", html)
        self.assertNotIn("<img", html)
        self.assertNotIn("<li>injected", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;img", html)

    def test_a_long_answer_is_expandable_not_lost(self) -> None:
        answer = "y" * 500
        html = render_board(data(tasks=[task(status=REPORTED, answer=answer)]))
        # The preview stays short, but the whole answer is on the page, behind a click.
        self.assertIn("...", html)
        self.assertIn("<details", html)
        self.assertIn(answer, html)

    def test_a_long_queued_reason_is_expandable_not_lost(self) -> None:
        why = "w" * 400
        html = render_board(data(queued=[queued(why=why)]))
        self.assertIn("<details", html)
        self.assertIn(why, html)

    def test_a_missing_agent_list_is_stated_not_hidden(self) -> None:
        html = render_board(
            data(
                live_ok=False,
                note="The agent list could not be read (boom), so this page shows state only.",
            )
        )
        self.assertIn("could not be read", html)
        self.assertIn("state only", html)

    def test_the_state_path_and_time_are_on_the_page(self) -> None:
        html = render_board(data(state_path="C:/somewhere/state.json"))
        self.assertIn("C:/somewhere/state.json", html)
        self.assertIn("2026-09-27 10:00", html)


class HelpersTest(unittest.TestCase):
    def test_open_tasks_only(self) -> None:
        tasks = [task("t-0001"), task("t-0002", status=REPORTED, answer="done")]
        self.assertEqual([t.id for t in open_tasks(tasks)], ["t-0001"])

    def test_recent_answers_are_newest_first(self) -> None:
        tasks = [
            task("t-0001", status=REPORTED, answer="first"),
            task("t-0002", status=REPORTED, answer="second"),
        ]
        self.assertEqual([t.id for t in recent_answers(tasks)], ["t-0002", "t-0001"])

    def test_a_space_says_what_it_holds(self) -> None:
        self.assertEqual(agent(branch=None, detached=True).place_label, "free, on 1a2b3c4")
        self.assertEqual(agent(main_checkout=True).place_label, "the main checkout")
        self.assertEqual(
            agent(branch="task/refund", commit="abc1234").place_label,
            "task/refund @ abc1234",
        )

    def test_write_board_creates_the_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "deep" / "board.html"
            written = write_board(data(), target)
            self.assertTrue(written.is_file())
            self.assertIn("clowder board", written.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
