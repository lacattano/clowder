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
    def test_brief_is_unchanged_and_comes_after_the_marker(self) -> None:
        brief = "ship: add the refund page\n\n1. shape\n2. test"
        text = apply_marker(brief, "t-0004", "ship", "clowder", "topcat")
        self.assertEqual(
            text.splitlines()[0], "[clowder job t-0004 | ship | clowder | from topcat]"
        )
        self.assertEqual(text.split("\n", 1)[1], brief)

    def test_a_single_line_brief_still_reports_two_lines(self) -> None:
        text = apply_marker("do the thing", "t-0001", "ship", "r", "topcat")
        self.assertEqual(len(text.splitlines()), 2)

    def test_a_brief_that_looks_like_a_marker_is_not_altered(self) -> None:
        odd = "[clowder job t-9999 | scout | other | from someone else]\nthe real brief"
        text = apply_marker(odd, "t-0001", "ship", "r", "topcat")
        self.assertEqual(len(text.splitlines()), 3)
        self.assertTrue(text.startswith("[clowder job t-0001 |"))


if __name__ == "__main__":
    unittest.main()
