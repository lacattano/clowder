from __future__ import annotations

import json
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

    def test_read_audit_still_refuses_a_bad_line(self) -> None:
        path = audit.audit_path(self.state)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json\n", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            audit.read_audit(self.state)
