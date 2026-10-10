from __future__ import annotations

import unittest

from clowder.marker import DEFAULT_SENDER, apply_marker, marker_line


class MarkerLineTest(unittest.TestCase):
    def test_exact_shape(self) -> None:
        self.assertEqual(
            marker_line("t-0004", "ship", "clowder", "topcat"),
            "[clowder job t-0004 | ship | clowder | from topcat]",
        )

    def test_scout_shape_is_recorded_too(self) -> None:
        self.assertIn("| scout |", marker_line("t-0001", "scout", "myrepo", "topcat"))

    def test_default_sender_reads_as_a_sentence(self) -> None:
        self.assertEqual(DEFAULT_SENDER, "the front door")
        self.assertTrue(
            marker_line("t-0001", "ship", "r", DEFAULT_SENDER).endswith("from the front door]")
        )

    def test_marker_is_one_line(self) -> None:
        self.assertEqual(len(marker_line("t-0001", "ship", "r", "topcat").splitlines()), 1)


class ApplyMarkerTest(unittest.TestCase):
    def test_the_brief_is_unchanged_and_comes_last(self) -> None:
        brief = "ship: add the refund page\n\n1. shape\n2. test"
        text = apply_marker(brief, "t-0004", "ship", "clowder", "topcat")
        self.assertEqual(
            text.splitlines()[0], "[clowder job t-0004 | ship | clowder | from topcat]"
        )
        self.assertTrue(text.endswith(brief), "the brief is handed over unchanged")

    def test_the_tool_tells_the_worker_how_to_report(self) -> None:
        text = apply_marker("do the thing", "t-0001", "ship", "r", "topcat")
        # The short marker still carries the essential instruction: send, to whom,
        # resolved in the worker's own peer list, with the id, a headline and where
        # the answer is. Without this, decision 22's failure returns.
        self.assertIn("send its report to topcat over the agent bus", text)
        self.assertIn("peer list", text)
        self.assertIn("job id", text)
        self.assertIn("one-line headline", text)
        self.assertIn("where the full answer is", text)
        self.assertIn("clowder inbox", text)
        # The bus mechanics moved to the crew skill, which the worker has in its
        # system prompt, so the marker points there instead of repeating them.
        self.assertIn("crew skill", text)
        self.assertNotIn("remote-pi", text)
        self.assertNotIn("only a human", text.lower())
        self.assertTrue(text.endswith("do the thing"))

    def test_the_marker_is_short(self) -> None:
        # The whole point of the move: the per-dispatch instruction is small. The
        # old marker was 128 words; this bounds it well below that.
        from clowder.marker import REPORT_INSTRUCTION

        self.assertLessEqual(len(REPORT_INSTRUCTION.split()), 60)

    def test_the_message_names_no_bus_address(self) -> None:
        # The worker resolves the name in its own peer list; addresses move.
        text = apply_marker("do the thing", "t-0001", "ship", "r", "topcat")
        self.assertNotIn("@", text)

    def test_a_brief_that_looks_like_a_marker_is_not_altered(self) -> None:
        odd = "[clowder job t-9999 | scout | other | from someone else]\nthe real brief"
        text = apply_marker(odd, "t-0001", "ship", "r", "topcat")
        self.assertTrue(text.startswith("[clowder job t-0001 |"))
        self.assertTrue(text.endswith(odd), "the odd brief is still handed over intact")


if __name__ == "__main__":
    unittest.main()
