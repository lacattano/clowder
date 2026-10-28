"""The cost classes, the headroom gate, and the box readout.

Every test states its own machine reading. The gate compares GiB, so a test that
asserts a refusal must be able to name the same numbers the message does, and it
must not depend on how busy the box running the suite happens to be.
"""

from __future__ import annotations

import unittest
from unittest import mock

from clowder import box, load


def reading(
    *,
    available_gib: float = 20.0,
    commit_gib: float = 40.0,
    carveout_gib: float | None = 32.0,
    gpu_used_gib: float | None = None,
    pagefile_gib: float = 64.0,
    pagefile_percent: float | None = None,
    unread: tuple[str, ...] = (),
) -> box.BoxReading:
    """One stated read of the box, in GiB where the gate compares GiB."""
    return box.BoxReading(
        read_at="2026-10-27 12:00:00",
        installed_bytes=int(64 * box.GIB),
        total_bytes=int(32 * box.GIB),
        available_bytes=int(available_gib * box.GIB),
        commit_limit_bytes=int(96 * box.GIB),
        commit_headroom_bytes=int(commit_gib * box.GIB),
        carveout_bytes=None if carveout_gib is None else int(carveout_gib * box.GIB),
        gpu_used_bytes=None if gpu_used_gib is None else int(gpu_used_gib * box.GIB),
        pagefile_bytes=int(pagefile_gib * box.GIB),
        pagefile_percent=pagefile_percent,
        unread=unread,
    )


class CostClassTest(unittest.TestCase):
    def test_the_set_is_closed_and_small(self) -> None:
        self.assertEqual(box.COSTS, ("none", "read", "suite", "install", "browser", "model"))
        for name in box.COSTS:
            self.assertIn(name, box.COST_CLASSES)

    def test_an_unknown_class_is_refused_with_the_set_named(self) -> None:
        with self.assertRaises(ValueError) as caught:
            box.cost_class("heavyy")
        self.assertIn("none, read, suite, install, browser, model", str(caught.exception))

    def test_every_class_states_what_it_costs(self) -> None:
        # A class with no footprint would be a claim with no number behind it, which
        # is the thing this change exists to end.
        for name, klass in box.COST_CLASSES.items():
            self.assertEqual(name, klass.name)
            self.assertGreaterEqual(klass.memory_gib, 0.0)
            self.assertGreaterEqual(klass.gpu_gib, 0.0)
            self.assertTrue(klass.what, f"{name} says nothing about itself")

    def test_heavy_means_a_suite_and_nothing_else(self) -> None:
        self.assertTrue(box.heavy_flag(box.COST_SUITE))
        for name in box.COSTS:
            if name != box.COST_SUITE:
                self.assertFalse(box.heavy_flag(name))

    def test_a_record_written_before_classes_reads_as_its_old_flag(self) -> None:
        class Old:
            heavy = True

        class Older:
            heavy = False

        class New:
            cost = "browser"
            heavy = False

        self.assertEqual(box.cost_of(Old()), box.COST_SUITE)
        self.assertEqual(box.cost_of(Older()), box.COST_NONE)
        self.assertEqual(box.cost_of(New()), box.COST_BROWSER)

    def test_a_class_a_newer_copy_wrote_is_read_as_no_cost(self) -> None:
        # The gate must never block a step because of a word it has not learned.
        self.assertEqual(box.klass_of("hologram").name, box.COST_NONE)


class GateTest(unittest.TestCase):
    def test_two_suites_fit_when_the_box_has_room_for_both(self) -> None:
        decision = box.decide(
            box.COST_SUITE,
            [box.StepCost("t-0001", box.COST_SUITE)],
            reading(available_gib=20.0),
        )
        self.assertTrue(decision.allowed, decision.reason)
        self.assertTrue(decision.checked_memory)

    def test_a_suite_and_a_model_leg_do_not_fit_in_8_gib(self) -> None:
        decision = box.decide(
            box.COST_MODEL,
            [box.StepCost("t-0001", box.COST_SUITE)],
            reading(available_gib=8.0),
        )
        self.assertFalse(decision.allowed)
        text = "\n".join(decision.lines)
        self.assertIn("8.0 GiB free", text)
        self.assertIn("3.0 GiB held", text)
        self.assertIn("4.0 GiB this model step needs", text)
        self.assertIn("4.0 GiB must stay free", text)

    def test_an_unmarked_step_never_blocks_anything(self) -> None:
        for available in (0.0, 0.5):
            decision = box.decide(box.COST_NONE, [], reading(available_gib=available))
            self.assertTrue(decision.allowed)
            self.assertFalse(decision.checked_memory)
        # And a box that cannot be read does not block one either.
        self.assertTrue(box.decide(box.COST_NONE, [], box.BoxReading(read_at="today")).allowed)

    def test_a_box_that_cannot_be_read_fails_open(self) -> None:
        decision = box.decide(box.COST_SUITE, [], box.BoxReading(read_at="today"))
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.unmeasured)
        self.assertIn("could not be read", decision.reason)

    def test_the_floor_is_what_refuses_a_small_fit(self) -> None:
        # 6 GiB free covers a 3 GiB suite, and the 4 GiB floor is what the box still
        # needs, so a 5 GiB box refuses it and says which number came up short.
        decision = box.decide(box.COST_SUITE, [], reading(available_gib=5.0))
        self.assertFalse(decision.allowed)
        self.assertIn("4.0 GiB must stay free", "\n".join(decision.lines))

    def test_the_commit_floor_is_checked_on_its_own(self) -> None:
        decision = box.decide(box.COST_SUITE, [], reading(available_gib=20.0, commit_gib=7.0))
        self.assertFalse(decision.allowed)
        self.assertIn("commit:", "\n".join(decision.lines))
        self.assertIn("6.0 GiB must stay free", "\n".join(decision.lines))

    def test_the_gpu_floor_refuses_a_leg_that_would_crowd_the_card(self) -> None:
        # The 27B is resident, so a browser leg would leave less than the 2 GiB the
        # card must keep. This is the check the CPU number cannot make.
        decision = box.decide(
            box.COST_BROWSER,
            [box.StepCost("t-0001", box.COST_BROWSER), box.StepCost("t-0002", box.COST_MODEL)],
            reading(available_gib=40.0, carveout_gib=32.0, gpu_used_gib=0.0),
        )
        self.assertFalse(decision.allowed)
        self.assertIn("GPU: 32.0 GiB is set aside", "\n".join(decision.lines))

    def test_no_gpu_size_skips_the_gpu_check_and_keeps_the_memory_check(self) -> None:
        decision = box.decide(
            box.COST_MODEL, [], reading(available_gib=40.0, carveout_gib=None)
        )
        self.assertTrue(decision.allowed, decision.reason)
        self.assertFalse(decision.checked_gpu)
        refused = box.decide(box.COST_MODEL, [], reading(available_gib=5.0, carveout_gib=None))
        self.assertFalse(refused.allowed)

    def test_a_floor_of_none_switches_that_check_off(self) -> None:
        open_floors = box.Floors(min_free_gib=None, min_commit_gib=None, min_gpu_gib=None)
        decision = box.decide(
            box.COST_SUITE, [], reading(available_gib=0.5, commit_gib=0.5), open_floors
        )
        self.assertTrue(decision.allowed)

    def test_the_refusal_names_the_running_step_that_holds_the_room(self) -> None:
        decision = box.decide(
            box.COST_MODEL,
            [box.StepCost("t-0042", box.COST_SUITE)],
            reading(available_gib=8.0),
        )
        self.assertIn("t-0042 (suite)", "\n".join(decision.lines))


class SummaryTest(unittest.TestCase):
    def test_the_summary_names_the_free_memory_and_the_held_figure(self) -> None:
        text = box.summary(
            reading(available_gib=20.0),
            [box.StepCost("t-0001", box.COST_SUITE)],
        )
        self.assertIn("box: 20.0 GiB free", text)
        self.assertIn("3.0 GiB held by 1 running step(s)", text)
        self.assertIn("a suite step would send now", text)

    def test_the_summary_says_what_a_missing_reading_means(self) -> None:
        text = box.summary(None)
        self.assertIn("not readable", text)
        self.assertIn("fails open", text)


class ReadoutTest(unittest.TestCase):
    def test_it_prints_three_pools_with_units_and_their_source(self) -> None:
        lines = box.readout(
            reading(
                available_gib=20.0,
                carveout_gib=32.0,
                gpu_used_gib=2.0,
                pagefile_percent=0.4,
            )
        )
        text = "\n".join(lines)
        self.assertIn("memory: 64.0 GiB installed", text)
        self.assertIn("32.0 GiB is set aside for the GPU", text)
        self.assertIn("so Windows sees 32.0 GiB", text)
        self.assertIn("GetPhysicallyInstalledSystemMemory", text)
        self.assertIn("HardwareInformation.qwMemorySize", text)
        self.assertIn("GPU side: 2.0 GiB in use of the 32.0 GiB", text)
        self.assertIn("30.0 GiB free", text)
        self.assertIn("CPU side: 32.0 GiB visible, 20.0 GiB free", text)
        self.assertIn("ullAvailPhys", text)
        self.assertIn("page file 64.0 GiB", text)
        self.assertIn("0.40% in use", text)
        self.assertIn("Paging File(_Total)", text)
        self.assertIn("gate now, a suite step", text)
        self.assertNotIn("GiB GiB", text)

    def test_it_names_the_process_holding_the_card(self) -> None:
        held = reading(gpu_used_gib=25.4)
        held = box.BoxReading(
            **{
                **held.__dict__,
                "gpu_holder": "llama-server.exe (pid 42132), 25.4 GiB",
            }
        )
        self.assertIn("held by llama-server.exe", "\n".join(box.readout(held)))

    def test_a_box_with_no_gpu_counter_still_prints_the_other_pools(self) -> None:
        lines = box.readout(
            reading(
                carveout_gib=None,
                unread=("the GPU's own memory (no registry value under the display class)",),
            )
        )
        text = "\n".join(lines)
        self.assertIn("GPU side: not readable on this box", text)
        self.assertIn("CPU side: 32.0 GiB visible", text)
        self.assertIn("could not be read:", text)
        self.assertIn("no registry value", text)

    def test_a_gpu_size_without_a_counter_says_what_it_could_not_read(self) -> None:
        text = "\n".join(box.readout(reading()))
        self.assertIn("GPU side: 32.0 GiB set aside, use not read", text)

    def test_the_readout_carries_the_numbers_a_refusal_uses(self) -> None:
        refused = box.readout(reading(available_gib=5.0))
        decision = box.decide(box.COST_SUITE, [], reading(available_gib=5.0))
        text = "\n".join(refused)
        for line in decision.lines:
            self.assertIn(line.strip(), text)

    def test_an_unreadable_machine_says_so_instead_of_printing_zero(self) -> None:
        text = "\n".join(box.readout(box.BoxReading(read_at="today")))
        self.assertIn("CPU side: not readable on this box", text)
        self.assertIn("the gate fails open", text)
        self.assertNotIn(" 0.0 GiB free", text)


class ProbeTest(unittest.TestCase):
    """The readings themselves, on whatever machine runs the suite."""

    def test_a_reading_is_bytes_or_nothing(self) -> None:
        read = load.memory_reading()
        if read is None:
            return
        self.assertGreater(read.total_bytes, 0)
        self.assertGreaterEqual(read.available_bytes, 0)
        self.assertLessEqual(read.available_bytes, read.total_bytes)
        self.assertGreaterEqual(read.commit_headroom_bytes, 0)
        self.assertLessEqual(read.commit_headroom_bytes, read.commit_limit_bytes)

    def test_installed_memory_is_the_boxes_own_figure(self) -> None:
        installed = load.installed_bytes()
        if installed is None:
            return
        self.assertGreater(installed, 0)
        self.assertEqual(installed % (1024 * 1024), 0, "reported in KiB, so a round MiB")

    def test_the_gpu_size_is_read_once_and_kept(self) -> None:
        first = load.gpu_carveout_bytes()
        second = load.gpu_carveout_bytes()
        self.assertEqual(first, second)
        if first is not None:
            self.assertGreater(first, 0)

    def test_the_probes_are_none_off_windows_and_never_raise(self) -> None:
        with mock.patch.object(load.sys, "platform", "linux"):
            self.assertIsNone(load.memory_reading())
            self.assertIsNone(load.installed_bytes())

    def test_the_slow_counters_are_not_read_by_the_gate(self) -> None:
        # A gate must stay under a second, so the readout's two counter reads are
        # opt-in and `read()` must not shell out.
        with mock.patch.object(box, "_typeperf") as probe:
            box.read()
        probe.assert_not_called()

    def test_a_counter_that_cannot_be_read_is_none(self) -> None:
        with mock.patch.object(box, "_typeperf", return_value=None):
            self.assertIsNone(box.gpu_used())
            self.assertIsNone(box.gpu_holder())
            self.assertIsNone(box.pagefile_percent())


if __name__ == "__main__":
    unittest.main()
