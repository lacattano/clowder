"""The generated handover: derived from the record, windowed, capped, idempotent."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path

from clowder import handover
from clowder.defects import DefectStore
from clowder.errors import StateError
from clowder.state import DISPATCHED, OPEN, REPORTED, Job, StateStore, Task
from clowder.timeutil import now, now_iso, to_iso

REPO = Path(__file__).resolve().parent.parent
TEXT_SUFFIXES = {".md", ".py", ".toml", ".json", ".cfg", ".txt", ".mjs", ".yml", ".yaml"}

SECTIONS = [
    "## What waits on the owner",
    "## In Flight",
    "## Landed in this window",
    "## The queue",
    "## Defects seen in this window",
    "## What a command cannot know",
]


def task(task_id: str = "t-0001", **overrides: object) -> Task:
    base: dict[str, object] = {
        "id": task_id,
        "question": "does the site need a refund policy?",
        "brief": "ship: add the refund page",
        "shape": "ship",
        "agent": "maker",
        "repo": "myrepo",
        "repo_path": "C:/code/myrepo",
        "status": DISPATCHED,
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
        "status": OPEN,
    }
    base.update(overrides)
    return Job(**base)  # type: ignore[arg-type]


class HandoverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state.json"
        self.store = StateStore(self.state)
        self.store.load()
        self.defects = DefectStore(self.root / "defects.json")

    def old_handover(self, days: int = 1, body: str = "") -> Path:
        """A hand-written handover from `days` ago, with a marker sentence in it."""
        moment = now() - timedelta(days=days)
        path = self.root / f"HANDOVER-{moment.date().isoformat()}.md"
        path.write_text(
            f"# Handover - {moment.date().isoformat()}\n\n{body}\n", encoding="utf-8"
        )
        # An old file has no `generated:` line, so its time is the file time.
        stamp = moment.timestamp()
        os.utime(path, (stamp, stamp))
        return path

    def test_a_generated_handover_contains_no_hand_written_quirk_prose(self) -> None:
        self.old_handover(
            body="- **A quirk from an old hand-written file.** "
            "The words that must not be copied."
        )
        self.store.add(task())
        self.store.add_job(job(pass_at=now_iso(), published_at=now_iso()))
        self.store.save()
        self.defects.sighting("a step does not close when it reports")

        path, dropped = handover.write(self.store, self.defects)

        text = path.read_text(encoding="utf-8")
        self.assertNotIn("must not be copied", text, "history is never restated")
        self.assertNotIn("A quirk from an old hand-written file", text)
        self.assertEqual(dropped, 0)
        self.assertIn(now().date().isoformat(), path.name, "the file is named for today")
        self.assertIn(f"generated: {to_iso(now())[:10]}", text, "it carries the moment")

    def test_the_sections_are_the_generated_ones_plus_the_notes(self) -> None:
        self.store.save()
        path, _ = handover.write(self.store, self.defects)
        text = path.read_text(encoding="utf-8")
        headings = [line for line in text.splitlines() if line.startswith("## ")]
        self.assertEqual(
            [heading.lower() for heading in headings],
            [section.lower() for section in SECTIONS],
        )
        self.assertEqual(headings[-1], "## What a command cannot know")

    def test_only_the_defects_inside_the_window_are_listed(self) -> None:
        self.old_handover(days=2)
        # One defect seen before the window, one seen inside it. `last_seen` is
        # written by hand here, which is how the test reaches into the past.
        old = now() - timedelta(days=5)
        self.defects.path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "next": 2,
                    "defects": {
                        "d-0001": {
                            "id": "d-0001",
                            "line": "the old defect, seen before the last handover",
                            "first_seen": to_iso(old),
                            "last_seen": to_iso(old),
                            "count": 4,
                        },
                        "d-0002": {
                            "id": "d-0002",
                            "line": "a defect seen in this session",
                            "first_seen": now_iso(),
                            "last_seen": now_iso(),
                            "count": 1,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        text_path, _ = handover.write(self.store, self.defects)
        text = text_path.read_text(encoding="utf-8")
        self.assertIn("d-0002 a defect seen in this session", text)
        self.assertNotIn("d-0001", text, "an older sighting is not re-listed")

    def test_a_defect_is_printed_as_one_line_and_an_id(self) -> None:
        self.store.save()
        self.defects.sighting("the board clips a long answer")
        self.defects.sighting(
            "the board clips a long answer",
            state="accepted-workaround",
            reason="a long reason that describes how it was accepted and what it cost",
        )
        path, _ = handover.write(self.store, self.defects)
        text = path.read_text(encoding="utf-8")
        section = text.split("## Defects seen in this window", 1)[1].split("## ", 1)[0]
        self.assertIn("d-0001", section)
        self.assertIn("the board clips a long answer", section)
        self.assertNotIn("how it was accepted", section, "the record keeps the reason")

    def test_the_notes_section_is_capped_at_ten_lines(self) -> None:
        (self.root / handover.NOTES_NAME).write_text(
            "\n".join(f"note {index}" for index in range(1, 13)) + "\n", encoding="utf-8"
        )
        self.store.save()
        path, dropped = handover.write(self.store, self.defects)
        self.assertEqual(dropped, 2)
        text = path.read_text(encoding="utf-8")
        self.assertIn("note 10", text)
        self.assertNotIn("note 11", text)
        self.assertIn("(capped at 10 lines; 2 line(s) were not written)", text)

    def test_a_defect_with_no_id_stops_the_handover(self) -> None:
        self.store.save()
        self.defects.path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "next": 1,
                    "defects": {
                        "d-0001": {
                            "line": "an entry with no id",
                            "first_seen": now_iso(),
                            "last_seen": now_iso(),
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(StateError) as caught:
            handover.write(self.store, self.defects)
        self.assertIn("d-0001", str(caught.exception))
        self.assertEqual(
            handover.handover_files(self.root), [], "a refused write leaves no file"
        )

    def test_writing_a_handover_twice_does_not_duplicate_entries(self) -> None:
        self.store.add(task())
        self.store.save()
        self.defects.sighting("a defect seen in this session")
        first, _ = handover.write(self.store, self.defects)
        before = first.read_text(encoding="utf-8")

        second, _ = handover.write(self.store, self.defects)
        after = second.read_text(encoding="utf-8")

        self.assertEqual(first, second, "same day, same file")
        self.assertEqual(
            len(handover.handover_files(self.root)), 1, "one file per day, not two"
        )
        self.assertEqual(after.count("d-0001"), 1, "the defect is listed once")
        self.assertEqual(
            after.count("## What waits on the owner"),
            before.count("## What waits on the owner"),
        )

    def test_the_window_starts_before_todays_file_on_a_rewrite(self) -> None:
        # Writing twice must not lose what the first write listed: the window starts
        # at the newest handover older than today's, not at today's own file.
        self.store.save()
        self.defects.sighting("seen this session")
        first, _ = handover.write(self.store, self.defects)
        second, _ = handover.write(self.store, self.defects)
        self.assertIn("seen this session", second.read_text(encoding="utf-8"))
        self.assertEqual(first, second)

    def test_the_handover_names_what_waits_on_the_owner_and_what_is_in_flight(self) -> None:
        self.store.add_job(job(pass_at=now_iso(), published_at=now_iso()))
        self.store.add(
            task(
                "t-0002",
                owner_item="choose A or B",
                owner_item_at=now_iso(),
                status=REPORTED,
            )
        )
        self.store.add(task("t-0003"))
        self.store.save()
        path, _ = handover.write(self.store, self.defects)
        text = path.read_text(encoding="utf-8")
        self.assertIn("your merge word for refund", text)
        self.assertIn("choose A or B", text)
        self.assertIn("does the site need a refund policy?", text)

    def test_a_hand_written_handover_is_never_replaced(self) -> None:
        # The five HANDOVER-*.md files from before this job are history. A file with
        # no generated mark is not written to, even when it carries today's date.
        today = now().date().isoformat()
        old = self.root / f"HANDOVER-{today}.md"
        old.write_text(
            "# Handover - by hand" + chr(10) * 2 + "the old words" + chr(10),
            encoding="utf-8",
        )
        before = old.read_text(encoding="utf-8")
        self.store.save()

        path, _ = handover.write(self.store, self.defects)

        self.assertEqual(old.read_text(encoding="utf-8"), before, "history is untouched")
        self.assertEqual(path.name, f"HANDOVER-{today}.generated.md")
        self.assertTrue(handover.is_generated(path))
        self.assertEqual(
            handover.latest(self.store, self.defects).path,
            path,
            "the generated one is the newest, so it sorts after",
        )

    def test_a_defect_reason_appears_exactly_once_in_the_working_tree(self) -> None:
        # The owner's constraint: no fact in two places. A reason lives in the record
        # and nowhere else - not in the handover, not in DESIGN.md, not in a doc. The
        # repo is scanned, so a future copy of this sentence in any file fails here.
        reason = "accepted because the owner quotes active time instead of wall clock"
        self.defects.sighting(
            "elapsed time is wall clock, not work",
            state="accepted-workaround",
            reason=reason,
        )
        self.store.save()
        handover_path, _ = handover.write(self.store, self.defects)

        self.assertEqual(self.defects.path.read_text(encoding="utf-8").count(reason), 1)
        self.assertNotIn(reason, handover_path.read_text(encoding="utf-8"))

        offenders = []
        for path in REPO.rglob("*"):
            if ".git" in path.parts or ".worktrees" in path.parts:
                continue
            if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if reason in text:
                offenders.append(str(path.relative_to(REPO)))
        self.assertEqual(offenders, [], "a defect reason lives in the record only")

    def test_design_names_a_defect_by_its_id_and_never_by_its_reason(self) -> None:
        # Decision 36 used to quote two defect lines from the handovers. A quoted
        # defect is a second copy: the doc now points at the record instead.
        design = (REPO / "DESIGN.md").read_text(encoding="utf-8")
        self.assertNotIn("A reset drops the pane to the default model", design)
        self.assertNotIn("A publish refused for a foreign identity", design)
        self.assertIn("by its one line and its id", design)
        self.assertIn("clowder defects show <id>", design)

    def test_last_activity_counts_a_defect_sighting(self) -> None:
        self.store.save()
        path, _ = handover.write(self.store, self.defects)
        self.assertFalse(handover.latest(self.store, self.defects).stale)
        # A sighting after the handover is work: the handover is now stale. The
        # record's moments have second resolution, so the clock has to move.
        time.sleep(1.05)
        self.defects.sighting("a defect recorded after the handover")
        latest = handover.latest(self.store, self.defects)
        self.assertTrue(latest.stale)
        self.assertIn("no handover has been written since", latest.why or "")
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
