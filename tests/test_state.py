from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from clowder.errors import StateError
from clowder.state import (
    DISPATCHED,
    REPORTED,
    SCHEMA_VERSION,
    Job,
    Queued,
    StateStore,
    Task,
)
from clowder.timeutil import now_iso

REPO = Path(__file__).resolve().parent.parent

# Two writers, in two processes, as two crew commands would be.
WRITER = (
    "import sys\n"
    "from clowder.state import StateStore, Task\n"
    "path, tid = sys.argv[1], sys.argv[2]\n"
    "store = StateStore(path)\n"
    "store.add(Task(id=tid, question='q', brief='b', shape='ship',\n"
    "               agent='maker', repo='repo', repo_path='C:/repo'))\n"
    "store.save()\n"
)


def make_task(task_id: str = "t-0001", **overrides: object) -> Task:
    base: dict[str, object] = {
        "id": task_id,
        "question": "does the site need a refund policy?",
        "brief": "scout: check the terms page",
        "shape": "scout",
        "agent": "verifier",
        "repo": "myrepo",
        "repo_path": "C:/code/myrepo",
    }
    base.update(overrides)
    return Task(**base)  # type: ignore[arg-type]


def make_queued(item_id: str = "q-0001", **overrides: object) -> Queued:
    base: dict[str, object] = {
        "id": item_id,
        "brief": "ship: add the refund page",
        "repo": "myrepo",
        "why": "the maker's space holds an open job",
        "agent": "maker",
    }
    base.update(overrides)
    return Queued(**base)  # type: ignore[arg-type]


def make_job(job_id: str = "j-0001", **overrides: object) -> Job:
    base: dict[str, object] = {
        "id": job_id,
        "label": "refund",
        "repo": "myrepo",
        "repo_path": "C:/code/myrepo",
        "worktree": "C:/code/myrepo/.worktrees/maker",
        "branch": "task/refund",
        "base": "main",
        "agent": "maker",
    }
    base.update(overrides)
    return Job(**base)  # type: ignore[arg-type]


class StateStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"

    def test_missing_file_is_an_empty_store(self) -> None:
        store = StateStore(self.path)
        self.assertEqual(len(store), 0)
        self.assertEqual(store.all(), [])

    def test_add_then_reload_round_trips_every_field(self) -> None:
        store = StateStore(self.path)
        task = make_task(
            worktree="C:/wt/refund",
            status=REPORTED,
            dispatched_at=now_iso(),
            reported_at=now_iso(),
            answer="yes, it is missing",
            answer_source="session",
            open_decision="14 days or 30?",
            usage={"turns": 3, "total_tokens": 8200},
            mux_argv=["herdr", "agent", "prompt", "verifier", "brief"],
        )
        store.add(task)
        store.save()

        reloaded = StateStore(self.path).get("t-0001")
        self.assertEqual(reloaded.question, task.question)
        self.assertEqual(reloaded.worktree, "C:/wt/refund")
        self.assertEqual(reloaded.status, REPORTED)
        self.assertEqual(reloaded.answer, "yes, it is missing")
        self.assertEqual(reloaded.open_decision, "14 days or 30?")
        self.assertEqual(reloaded.usage, {"turns": 3, "total_tokens": 8200})
        self.assertEqual(reloaded.mux_argv[-1], "brief")

    def test_a_decision_answer_survives_a_reload(self) -> None:
        store = StateStore(self.path)
        store.add(
            make_task(
                open_decision="14 days or 30?",
                decision_answer="14 days",
                decision_answered_at="2026-09-29T10:00:00Z",
            )
        )
        store.save()

        reloaded = StateStore(self.path).get("t-0001")
        self.assertEqual(reloaded.open_decision, "14 days or 30?")
        self.assertEqual(reloaded.decision_answer, "14 days")
        self.assertEqual(reloaded.decision_answered_at, "2026-09-29T10:00:00Z")
        self.assertFalse(reloaded.decision_is_open, "his answer closes it")

    def test_next_id_increments_and_survives_reload(self) -> None:
        store = StateStore(self.path)
        first = store.next_id()
        store.add(make_task(first))
        store.save()

        second_store = StateStore(self.path)
        second = second_store.next_id()
        self.assertEqual(first, "t-0001")
        self.assertEqual(second, "t-0002")

    def test_duplicate_id_is_refused(self) -> None:
        store = StateStore(self.path)
        store.add(make_task("t-0001"))
        with self.assertRaises(StateError):
            store.add(make_task("t-0001"))

    def test_unknown_id_says_which_file(self) -> None:
        store = StateStore(self.path)
        with self.assertRaises(StateError) as caught:
            store.get("t-9999")
        self.assertIn(str(self.path), str(caught.exception))

    def test_corrupt_json_is_refused(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(StateError) as caught:
            StateStore(self.path).load()
        self.assertIn("not valid JSON", str(caught.exception))

    def test_unknown_schema_is_refused(self) -> None:
        self.path.write_text(
            json.dumps({"schema": 99, "seq": 0, "tasks": {}}), encoding="utf-8"
        )
        with self.assertRaises(StateError) as caught:
            StateStore(self.path).load()
        self.assertIn("schema", str(caught.exception))

    def test_an_unknown_task_field_is_ignored_and_kept(self) -> None:
        record = make_task("t-0001").to_dict()
        record["surprise"] = 7
        payload = {"schema": SCHEMA_VERSION, "seq": 1, "tasks": {"t-0001": record}}
        self.path.write_text(json.dumps(payload), encoding="utf-8")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            store = StateStore(self.path)
            task = store.get("t-0001")
        self.assertEqual(task.extra, {"surprise": 7})
        self.assertIn("surprise", err.getvalue(), "the reader names the field it ignored")

        store.save()  # a newer copy may still need it, so it is not dropped
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["tasks"]["t-0001"]["surprise"], 7)

    def test_save_leaves_no_temp_file(self) -> None:
        store = StateStore(self.path)
        store.add(make_task())
        store.save()
        leftovers = [p.name for p in self.path.parent.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_select_filters(self) -> None:
        store = StateStore(self.path)
        store.add(make_task("t-0001", agent="maker", status=DISPATCHED))
        store.add(make_task("t-0002", agent="verifier", status=REPORTED, answer="done"))
        store.add(make_task("t-0003", agent="verifier", repo="other", status=DISPATCHED))
        store.save()

        self.assertEqual(len(store.all()), 3)
        self.assertEqual([t.id for t in store.select(agent="verifier")], ["t-0002", "t-0003"])
        self.assertEqual([t.id for t in store.select(open_only=True)], ["t-0001", "t-0003"])
        self.assertEqual([t.id for t in store.select(repo="other")], ["t-0003"])
        self.assertEqual([t.id for t in store.select(status=REPORTED)], ["t-0002"])

    def test_open_means_no_answer_yet(self) -> None:
        self.assertTrue(make_task(status=DISPATCHED).is_open)
        self.assertFalse(make_task(status=REPORTED).is_open)

    def test_age_is_measured_from_dispatch(self) -> None:
        task = make_task(
            dispatched_at="2026-09-27T10:00:00Z", reported_at="2026-09-27T10:00:14Z"
        )
        self.assertAlmostEqual(task.age_seconds, 14.0, places=3)


class QueuedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"

    def test_a_queued_item_survives_a_reload(self) -> None:
        store = StateStore(self.path)
        item = make_queued(shape="scout", job="j-0001", question="the refund page?")
        store.add_queued(item)
        store.save()

        reloaded = StateStore(self.path).get_queued("q-0001")
        self.assertEqual(reloaded.brief, item.brief)
        self.assertEqual(reloaded.repo, "myrepo")
        self.assertEqual(reloaded.agent, "maker")
        self.assertEqual(reloaded.role, None)
        self.assertEqual(reloaded.why, item.why)
        self.assertEqual(reloaded.shape, "scout")
        self.assertEqual(reloaded.job, "j-0001")
        self.assertEqual(reloaded.question, "the refund page?")
        self.assertEqual(reloaded.target, "maker")

    def test_a_role_only_item_targets_the_role(self) -> None:
        store = StateStore(self.path)
        store.add_queued(make_queued(agent=None, role="verifier"))
        store.save()
        reloaded = StateStore(self.path).get_queued("q-0001")
        self.assertEqual(reloaded.target, "verifier")

    def test_next_queue_id_increments_and_survives_reload(self) -> None:
        store = StateStore(self.path)
        first = store.next_queue_id()
        store.add_queued(make_queued(first))
        store.save()

        second_store = StateStore(self.path)
        second = second_store.next_queue_id()
        self.assertEqual(first, "q-0001")
        self.assertEqual(second, "q-0002")

    def test_removing_a_queued_item_survives_reload(self) -> None:
        store = StateStore(self.path)
        store.add_queued(make_queued())
        store.save()

        again = StateStore(self.path)
        again.remove_queued("q-0001")
        again.save()
        self.assertEqual(StateStore(self.path).all_queued(), [])

    def test_an_unknown_queued_field_is_ignored_and_kept(self) -> None:
        record = make_queued("q-0001").to_dict()
        record["surprise"] = 3
        payload = {"schema": SCHEMA_VERSION, "seq": 0, "queued": {"q-0001": record}}
        self.path.write_text(json.dumps(payload), encoding="utf-8")

        store = StateStore(self.path)
        self.assertEqual(store.get_queued("q-0001").extra, {"surprise": 3})
        store.save()
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["queued"]["q-0001"]["surprise"], 3)


class JobGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"

    def test_the_owner_s_two_words_survive_a_reload(self) -> None:
        store = StateStore(self.path)
        store.add_job(
            make_job(
                pass_shown="the diff of task/refund",
                pass_answer="yes, ship it",
                pass_at="2026-09-29T10:00:00Z",
                pass_by="lacattano",
                merge_word="merge it",
                merge_word_at="2026-09-29T11:00:00Z",
                merge_word_by="lacattano",
            )
        )
        store.save()

        reloaded = StateStore(self.path).get_job("j-0001")
        self.assertTrue(reloaded.has_pass)
        self.assertTrue(reloaded.has_merge_word)
        self.assertEqual(reloaded.pass_shown, "the diff of task/refund")
        self.assertEqual(reloaded.pass_answer, "yes, ship it")
        self.assertEqual(reloaded.pass_by, "lacattano")
        self.assertEqual(reloaded.merge_word, "merge it")
        self.assertEqual(reloaded.merge_word_by, "lacattano")

    def test_a_job_with_no_words_has_no_gates(self) -> None:
        job = make_job()
        self.assertFalse(job.has_pass)
        self.assertFalse(job.has_merge_word)


class ConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"

    def test_two_writers_at_once_keep_every_record(self) -> None:
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", WRITER, str(self.path), tid],
                cwd=str(REPO),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for tid in ("t-0001", "t-0002")
        ]
        for proc in procs:
            _, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err.decode("utf-8", "replace"))

        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(set(payload["tasks"]), {"t-0001", "t-0002"})

    def test_a_second_writer_folds_in_a_new_record(self) -> None:
        base = StateStore(self.path)
        base.add(make_task("t-0001"))
        base.save()

        first = StateStore(self.path)
        second = StateStore(self.path)
        second.add(make_task("t-0002"))
        second.save()
        first.add(make_task("t-0003"))
        first.save()  # must keep t-0002, not lose it

        ids = {task.id for task in StateStore(self.path).all()}
        self.assertEqual(ids, {"t-0001", "t-0002", "t-0003"})

    def test_a_stale_writer_is_refused(self) -> None:
        base = StateStore(self.path)
        base.add(make_task("t-0001"))
        base.save()

        writer = StateStore(self.path)
        other = StateStore(self.path)
        writer.get("t-0001").answer = "mine"
        other.get("t-0001").answer = "theirs"
        other.save()

        with self.assertRaises(StateError) as caught:
            writer.save()
        self.assertIn("stale save", str(caught.exception))
        self.assertEqual(StateStore(self.path).get("t-0001").answer, "theirs")


if __name__ == "__main__":
    unittest.main()
