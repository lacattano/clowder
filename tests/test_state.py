from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from clowder.errors import StateError
from clowder.state import (
    DISPATCHED,
    REPORTED,
    SCHEMA_VERSION,
    StateStore,
    Task,
)
from clowder.timeutil import now_iso


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

    def test_unknown_task_field_is_refused(self) -> None:
        payload = {
            "schema": SCHEMA_VERSION,
            "seq": 1,
            "tasks": {"t-0001": {"id": "t-0001", "question": "q", "surprise": 1}},
        }
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(StateError) as caught:
            StateStore(self.path).load()
        self.assertIn("unknown fields", str(caught.exception))

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


if __name__ == "__main__":
    unittest.main()
