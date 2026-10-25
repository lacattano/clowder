"""The defects record: one file, one home, a count that means something."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from clowder.defects import (
    ACCEPTED,
    FIXED,
    HAS_JOB,
    OPEN,
    DefectStore,
    defects_path,
    describe,
    matches_line,
    normalise_line,
    read_defects,
    recently_seen,
    words,
)
from clowder.errors import StateError
from clowder.timeutil import to_epoch


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = defects_path(self.root / "state.json")
        self.store = DefectStore(self.path)

    def test_the_record_sits_beside_the_state_file(self) -> None:
        self.assertEqual(self.path, self.root / "defects.json")

    def test_a_defect_added_twice_has_count_two_and_appears_repeated(self) -> None:
        first, created = self.store.sighting("a step does not close when it reports")
        self.assertTrue(created)
        self.assertEqual(first.count, 1)
        first_seen = first.first_seen

        time.sleep(1.05)  # the record's moments have second resolution
        second, created_again = self.store.sighting("A step does not close when it reports.")
        self.assertFalse(created_again, "the same line is the same defect")
        self.assertEqual(second.id, first.id)
        self.assertEqual(second.count, 2)
        self.assertEqual(second.first_seen, first_seen, "first seen does not move")
        self.assertGreater(
            to_epoch(second.last_seen) or 0,
            to_epoch(first_seen) or 0,
            "last seen moves",
        )

        repeated = DefectStore(self.path).repeated([])
        self.assertEqual([defect.id for defect in repeated], [second.id])

    def test_a_defect_with_an_open_job_is_not_repeated(self) -> None:
        self.store.sighting("a step does not close when it reports")
        self.store.sighting("a step does not close when it reports", job="j-0007")
        defect = self.store.all()[0]
        self.assertEqual(defect.job, "j-0007")
        self.assertEqual(defect.state, HAS_JOB)
        self.assertEqual(self.store.repeated(["j-0007"]), [])
        # The same job, but closed: the fix is not in flight, so it counts again.
        self.assertEqual([item.id for item in self.store.repeated([])], [defect.id])

    def test_an_accepted_workaround_is_not_repeated_and_needs_a_reason(self) -> None:
        self.store.sighting("elapsed time is wall clock, not work")
        defect, _ = self.store.sighting(
            "elapsed time is wall clock, not work",
            state=ACCEPTED,
            reason="the owner quotes active time instead",
        )
        self.assertEqual(defect.state, ACCEPTED)
        self.assertEqual(defect.reason, "the owner quotes active time instead")
        self.assertEqual(self.store.repeated([]), [], "an accepted cost is not a job")

    def test_a_fixed_defect_seen_again_is_open_again(self) -> None:
        self.store.sighting("the board clips a long answer")
        self.store.sighting("the board clips a long answer", state=FIXED)
        self.assertEqual(self.store.all()[0].state, FIXED)
        defect, _ = self.store.sighting("the board clips a long answer")
        self.assertEqual(defect.state, OPEN, "it is not fixed if it happened again")
        self.assertEqual(defect.count, 3)

    def test_different_words_are_different_defects(self) -> None:
        self.store.sighting("a step does not close when it reports")
        self.store.sighting("a queued item is recorded as a ship")
        self.assertEqual([d.id for d in self.store.all()], ["d-0001", "d-0002"])

    def test_ids_are_never_reused(self) -> None:
        first, _ = self.store.sighting("one")
        second, _ = self.store.sighting("two")
        self.assertEqual((first.id, second.id), ("d-0001", "d-0002"))
        reloaded = DefectStore(self.path)
        third, _ = reloaded.sighting("three")
        self.assertEqual(third.id, "d-0003")

    def test_a_multi_line_defect_is_kept_as_one_line(self) -> None:
        defect, _ = self.store.sighting("a step does not close\nwhen it reports")
        self.assertEqual(defect.line, "a step does not close when it reports")

    def test_an_unknown_state_is_refused(self) -> None:
        with self.assertRaises(StateError):
            self.store.sighting("something", state="maybe")

    def test_an_entry_with_no_id_is_reported_as_broken(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "next": 2,
                    "defects": {
                        "d-0001": {
                            "id": "d-0001",
                            "line": "a real one",
                            "first_seen": "2026-10-25T10:00:00Z",
                            "last_seen": "2026-10-25T10:00:00Z",
                        },
                        "d-0002": {
                            "line": "an entry with no id",
                            "first_seen": "2026-10-25T10:00:00Z",
                            "last_seen": "2026-10-25T10:00:00Z",
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        broken = DefectStore(self.path).broken()
        self.assertEqual(len(broken), 1)
        self.assertEqual(broken[0][0], "d-0002")

    def test_a_second_writer_folds_instead_of_losing_the_first(self) -> None:
        one = DefectStore(self.path)
        two = DefectStore(self.path)
        one.sighting("the first")
        two.sighting("the second")  # reloads under the lock, so the first survives
        self.assertEqual(len(DefectStore(self.path)), 2)

    def test_the_line_key_ignores_case_whitespace_and_edge_punctuation(self) -> None:
        self.assertEqual(
            normalise_line("  A step  does not close. "),
            normalise_line("a step does not close"),
        )
        self.assertNotEqual(
            normalise_line("a step does not close"),
            normalise_line("a step does not report"),
        )


class WordSearchTest(unittest.TestCase):
    def test_the_same_words_in_another_order_are_found(self) -> None:
        # The reviewer's case: the same defect written in different words. Word
        # search finds it when the words are the same, whatever the phrasing.
        line = "a step does not close when it reports"
        self.assertTrue(matches_line(line, "reports close step"))
        self.assertTrue(matches_line(line, "step reports"))

    def test_a_total_rephrase_is_missed_and_that_is_pinned(self) -> None:
        # Pinned on purpose: the limit is real, and a later change must not pretend
        # it is solved. Fuzzy matching would merge two real defects, which is worse.
        line = "a step does not close when it reports"
        self.assertFalse(matches_line(line, "finished work stays in flight"))

    def test_one_missing_word_is_a_miss(self) -> None:
        self.assertFalse(matches_line("the board clips a long answer", "board answer text"))

    def test_words_ignore_case_and_punctuation(self) -> None:
        self.assertEqual(words("A step does NOT close."), ["a", "step", "does", "not", "close"])

    def test_an_empty_query_matches_nothing(self) -> None:
        self.assertFalse(matches_line("a step does not close", "   "))


class SafeReadTest(unittest.TestCase):
    def test_a_corrupt_record_is_reported_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "defects.json"
            path.write_text("{not json", encoding="utf-8")
            entries, error = read_defects(DefectStore(path))
            self.assertEqual(entries, [])
            self.assertIsNotNone(error)
            self.assertIn(str(path), error or "", "the error names the file")
            self.assertIn("JSON", error or "", "and the fault")

    def test_a_good_record_reads_with_no_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = DefectStore(Path(tmp) / "defects.json")
            store.sighting("one line")
            entries, error = read_defects(store)
            self.assertEqual(len(entries), 1)
            self.assertIsNone(error)

    def test_recently_seen_keeps_the_last_seven_days(self) -> None:
        from datetime import timedelta

        from clowder.timeutil import now, to_iso

        with tempfile.TemporaryDirectory() as tmp:
            store = DefectStore(Path(tmp) / "defects.json")
            store.sighting("today")
            old, _ = store.sighting("long ago")
            # Last seen, not first seen: a defect stated a month ago and still biting
            # is exactly the one a reworded duplicate would run from.
            long_ago = to_iso(now() - timedelta(days=8))
            old.first_seen = long_ago
            old.last_seen = long_ago
            entries, error = read_defects(store)
            self.assertIsNone(error)
            recent = recently_seen(entries, 7)
            self.assertEqual([defect.line for defect in recent], ["today"])


class DescribeTest(unittest.TestCase):
    def test_describe_names_the_id_and_the_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = DefectStore(Path(tmp) / "defects.json")
            defect, _ = store.sighting("one line")
            text = describe(defect)
            self.assertIn("d-0001", text)
            self.assertIn("one line", text)
            self.assertIn("seen 1 time", text)


if __name__ == "__main__":
    unittest.main()
