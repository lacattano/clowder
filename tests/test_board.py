from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from clowder.board import (
    BoardAgent,
    BoardData,
    open_tasks,
    recent_answers,
    render_board,
    waiting_for_worker,
    waiting_on_you,
    write_board,
)
from clowder.state import CLOSED, REPORTED, Job, Queued, Task
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

    def test_the_page_reloads_itself(self) -> None:
        html = render_board(data())
        self.assertIn('<meta http-equiv="refresh" content="30">', html)
        self.assertIn(
            '<meta http-equiv="refresh" content="30">',
            html.split("<body>")[0],
            "the reload belongs in the head",
        )

    def test_every_section_is_present_even_when_empty(self) -> None:
        html = render_board(data())
        for heading in (
            "Waiting on you",
            "Waiting for a worker",
            "Open steps",
            "Agents and spaces",
            "Answers",
            "Jobs",
        ):
            self.assertIn(heading, html)
        self.assertIn("Nothing is waiting on you.", html)
        self.assertIn("Nothing is waiting for the front door.", html)

    def test_an_open_step_is_shown(self) -> None:
        html = render_board(data(tasks=[task()], agents=[agent()]))
        self.assertIn("t-0001", html)
        self.assertIn("does the site need a refund policy?", html)
        self.assertIn("task/refund", html)
        self.assertNotIn("No open steps", html)

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

    def test_the_job_table_shows_the_owner_s_gates(self) -> None:
        passed = job(pass_at="2026-09-29T10:00:00Z", pass_by="lacattano")
        html = render_board(data(jobs=[passed]))
        self.assertIn("<th>pass</th>", html)
        self.assertIn("<th>word</th>", html)
        self.assertIn("<td>yes</td>", html, "the pass column")
        self.assertIn("<td>no</td>", html, "the merge word column")

    def test_the_worker_section_is_marked_apart_from_the_owner(self) -> None:
        html = render_board(data(queued=[queued()]))
        self.assertIn("Nothing is waiting on you.", html, "the owner has nothing to do")
        self.assertIn("Waiting for a worker", html)
        owner, worker = html.split("Waiting on you", 1)[1].split("Waiting for a worker")
        self.assertNotIn("q-0001", owner, "a queued item is not the owner's work")
        self.assertIn("q-0001", worker)
        self.assertIn("the space holds an open job", worker)

    def test_the_worker_section_names_the_front_door(self) -> None:
        html = render_board(data(front_door_name="topcat"))
        self.assertIn("Nothing is waiting for topcat.", html)

    def test_the_front_door_name_is_used_when_its_list_is_not_empty(self) -> None:
        html = render_board(data(queued=[queued()], front_door_name="topcat"))
        self.assertIn("topcat chases them", html)


class WaitingTest(unittest.TestCase):
    def test_a_decision_waits_on_the_human(self) -> None:
        waiting = waiting_on_you(data(tasks=[task(open_decision="14 days or 30?")]))
        self.assertEqual(len(waiting), 1)
        self.assertIn("decision", waiting[0])
        self.assertIn("14 days or 30?", waiting[0])
        self.assertIn("t-0001", waiting[0])

    def test_a_reviewed_job_waits_on_the_human_to_merge(self) -> None:
        reviewed = job(reviewer="verifier", review_commit="a" * 40)
        waiting = waiting_on_you(data(jobs=[reviewed], tasks=[task(status=REPORTED)]))
        self.assertEqual(len(waiting), 1)
        self.assertIn("ready to merge", waiting[0])
        self.assertIn("verifier", waiting[0])

    def test_a_reviewed_job_with_an_open_step_does_not(self) -> None:
        reviewed = job(reviewer="verifier", review_commit="a" * 40)
        waiting = waiting_on_you(data(jobs=[reviewed], tasks=[task()]))
        self.assertEqual(waiting, [], "the step is still open, so nobody is waiting")

    def test_work_on_no_branch_is_flagged_as_at_risk(self) -> None:
        waiting = waiting_on_you(data(tasks=[task()], stranded={"t-0001"}))
        self.assertEqual(len(waiting), 1)
        self.assertIn("work at risk", waiting[0])
        self.assertIn("on no branch", waiting[0])

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

    def test_a_queued_item_waits(self) -> None:
        waiting = waiting_for_worker(data(queued=[queued()]))
        self.assertEqual(len(waiting), 1)
        self.assertIn("queued", waiting[0])
        self.assertIn("q-0001", waiting[0])
        self.assertIn("maker", waiting[0])
        self.assertIn("open job", waiting[0])

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


class SafetyTest(unittest.TestCase):
    def test_agent_text_is_escaped(self) -> None:
        nasty = task(
            question="<script>alert('x')</script>",
            answer="<img src=x onerror=alert(1)>",
            open_decision="</li><li>injected",
        )
        html = render_board(data(tasks=[nasty], agents=[agent()]))
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img", html)
        self.assertNotIn("<li>injected", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;img", html)

    def test_a_long_answer_is_clipped(self) -> None:
        html = render_board(data(tasks=[task(status=REPORTED, answer="y" * 500)]))
        self.assertNotIn("y" * 300, html)
        self.assertIn("...", html)

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
