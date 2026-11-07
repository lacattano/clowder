from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from clowder import audit


class ReadActionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state.json"

    def test_read_actions_keeps_one_action_in_order(self) -> None:
        audit.append_audit(self.state, {"action": "queue-drop", "item": "q-0001"})
        audit.append_audit(self.state, {"action": "step-abandon", "step": "t-0001"})
        audit.append_audit(self.state, {"action": "queue-drop", "item": "q-0002"})

        rows = audit.read_actions(self.state, "queue-drop")
        self.assertEqual([row["item"] for row in rows], ["q-0001", "q-0002"])

    def test_read_actions_skips_a_line_that_is_not_json(self) -> None:
        # The real audit file holds one hand-written line. The board must still
        # draw, so the display reader skips it rather than raising.
        audit.append_audit(self.state, {"action": "queue-drop", "item": "q-0001"})
        path = audit.audit_path(self.state)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("2026-10-04 hand-written, not JSON\n")
        audit.append_audit(self.state, {"action": "queue-drop", "item": "q-0002"})

        rows = audit.read_actions(self.state, "queue-drop")
        self.assertEqual([row["item"] for row in rows], ["q-0001", "q-0002"])

    def test_read_actions_on_a_missing_file_is_empty(self) -> None:
        self.assertEqual(audit.read_actions(self.state, "queue-drop"), [])

    def test_read_audit_keeps_a_hand_written_line_as_a_legacy_row(self) -> None:
        # The nine lines the real record held before the format was enforced, copied
        # verbatim: the reader must not refuse the file for any of them.
        fixture = Path(__file__).with_name("fixtures") / "legacy-audit-lines.txt"
        raw = fixture.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(raw), 9)
        path = audit.audit_path(self.state)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(raw) + "\n", encoding="utf-8")

        rows = audit.read_audit(self.state)
        self.assertEqual(len(rows), 9)
        for row, text in zip(rows, raw, strict=True):
            self.assertEqual(row["action"], "legacy")
            self.assertEqual(row["text"], text, "the original text is kept exactly")
            self.assertIn("line", row)
        self.assertEqual(rows[0]["at"], "2026-10-04T21:09:41.722171Z")
        self.assertEqual(rows[0]["line"], 1)

    def test_read_audit_names_a_json_line_that_is_not_an_object(self) -> None:
        path = audit.audit_path(self.state)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("123\n", encoding="utf-8")
        rows = audit.read_audit(self.state)
        self.assertEqual(rows[0]["action"], "legacy")
        self.assertEqual(rows[0]["text"], "123")
        self.assertEqual(rows[0]["why"], "JSON, but not an object")

    def test_a_mixed_file_reads_in_order_with_its_legacy_rows(self) -> None:
        audit.append_audit(self.state, {"action": "queue-drop", "item": "q-0001"})
        with open(audit.audit_path(self.state), "a", encoding="utf-8") as handle:
            handle.write("hand written, not JSON\n")
        audit.append_audit(self.state, {"action": "queue-drop", "item": "q-0002"})

        rows = audit.read_audit(self.state)
        self.assertEqual(
            [row["action"] for row in rows], ["queue-drop", "legacy", "queue-drop"]
        )
        self.assertEqual(rows[1]["line"], 2)
        self.assertEqual(rows[1]["text"], "hand written, not JSON")

    def test_the_real_audit_file_reads_if_this_machine_has_one(self) -> None:
        live = Path.home() / ".clowder" / "state.json"
        if not live.is_file():
            self.skipTest("no live state file on this machine")
        rows = audit.read_audit(live)
        self.assertTrue(rows)
