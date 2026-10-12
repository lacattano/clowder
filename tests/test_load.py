"""The heavy-step load probe: standard library only, and None when unreadable."""

from __future__ import annotations

import unittest
from unittest import mock

from clowder import load


class LoadTest(unittest.TestCase):
    def test_the_metric_is_the_platforms(self) -> None:
        self.assertIn(load.metric_name(), (load.METRIC_MEMORY, load.METRIC_LOAD))

    def test_memory_percent_is_a_value_or_none(self) -> None:
        value = load.memory_percent()
        if value is not None:
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 100.0)

    def test_load_per_cpu_is_a_value_or_none(self) -> None:
        value = load.load_per_cpu()
        if value is not None:
            self.assertGreaterEqual(value, 0.0)

    def test_current_reports_the_metric_and_value(self) -> None:
        with (
            mock.patch.object(load, "metric_name", return_value=load.METRIC_LOAD),
            mock.patch.object(load, "load_per_cpu", return_value=0.75),
        ):
            self.assertEqual(load.current(), (load.METRIC_LOAD, 0.75))
        with (
            mock.patch.object(load, "metric_name", return_value=load.METRIC_MEMORY),
            mock.patch.object(load, "memory_percent", return_value=42.0),
        ):
            self.assertEqual(load.current(), (load.METRIC_MEMORY, 42.0))

    def test_a_failed_probe_is_none(self) -> None:
        with (
            mock.patch.object(load, "metric_name", return_value=load.METRIC_MEMORY),
            mock.patch.object(load, "memory_percent", return_value=None),
        ):
            self.assertIsNone(load.current())
        with (
            mock.patch.object(load, "metric_name", return_value=load.METRIC_LOAD),
            mock.patch.object(load, "load_per_cpu", return_value=None),
        ):
            self.assertIsNone(load.current())


if __name__ == "__main__":
    unittest.main()
