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
    visible_gib: float = 32.0,
    commit_limit_gib: float = 96.0,
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
        total_bytes=int(visible_gib * box.GIB),
        available_bytes=int(available_gib * box.GIB),
        commit_limit_bytes=int(commit_limit_gib * box.GIB),
        commit_headroom_bytes=int(commit_gib * box.GIB),
        carveout_bytes=None if carveout_gib is None else int(carveout_gib * box.GIB),
        gpu_used_bytes=None if gpu_used_gib is None else int(gpu_used_gib * box.GIB),
        pagefile_bytes=int(pagefile_gib * box.GIB),
        pagefile_percent=pagefile_percent,
        unread=unread,
    )


def measured(**overrides: float) -> box.BoxReading:
    """This machine on 2026-11-16, as the job states it: the measurement case.

    Windows sees 31.8 GiB, the commit limit is 95.8 GiB, 33.0 GiB is committed so
    62.8 GiB of headroom is left, and only 7.2 GiB is free because the rest is warm
    file cache. Each fixture below changes one of those numbers and nothing else.
    """
    stated = {
        "available_gib": 7.2,
        "visible_gib": 31.8,
        "commit_limit_gib": 95.8,
        "commit_gib": 62.8,
        "gpu_used_gib": 2.9,
    }
    stated.update(overrides)
    return reading(**stated)


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

    def test_a_low_free_figure_does_not_refuse_while_commit_headroom_covers_it(self) -> None:
        # 8 GiB free with a suite already running leaves 1 GiB for a model leg, under
        # the free floor. Commit headroom is not thin, so the free test confirms and
        # does not refuse: the box is warm, not full.
        decision = box.decide(
            box.COST_MODEL,
            [box.StepCost("t-0001", box.COST_SUITE)],
            reading(available_gib=8.0),
        )
        self.assertTrue(decision.allowed, decision.reason)
        self.assertEqual(decision.refused_by, ())
        text = "\n".join(decision.lines)
        self.assertIn("8.0 GiB free", text)
        self.assertIn("4.0 GiB this model step needs", text)
        self.assertIn("3.2 GiB must stay free", text)
        self.assertIn("commit headroom left covers it", decision.reason)

    def test_the_same_fit_is_refused_when_commit_headroom_is_thin_too(self) -> None:
        decision = box.decide(
            box.COST_MODEL,
            [box.StepCost("t-0001", box.COST_SUITE)],
            reading(available_gib=8.0, commit_gib=9.0),
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.refused_by, (box.TEST_COMMIT, box.TEST_MEMORY))

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

    def test_a_small_fit_is_refused_by_the_pool_that_is_actually_short(self) -> None:
        # 5 GiB free leaves 2 GiB for a 3 GiB suite, under the free floor of 3.2 GiB
        # (a tenth of the 32 GiB Windows sees). With commit headroom behind it that
        # only confirms; with commit headroom thin too it refuses, and names both.
        roomy = box.decide(box.COST_SUITE, [], reading(available_gib=5.0))
        self.assertTrue(roomy.allowed, roomy.reason)
        decision = box.decide(box.COST_SUITE, [], reading(available_gib=5.0, commit_gib=8.0))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.refused_by, (box.TEST_COMMIT, box.TEST_MEMORY))
        text = "\n".join(decision.lines)
        self.assertIn("3.2 GiB must stay free", text)
        self.assertIn("10% of the 32.0 GiB Windows sees", text)

    def test_the_commit_floor_is_checked_on_its_own(self) -> None:
        decision = box.decide(box.COST_SUITE, [], reading(available_gib=20.0, commit_gib=7.0))
        self.assertFalse(decision.allowed)
        self.assertIn("commit:", "\n".join(decision.lines))
        self.assertIn("6.0 GiB of commit headroom must stay free", "\n".join(decision.lines))

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

    def test_no_gpu_size_skips_the_gpu_check_and_keeps_the_cpu_checks(self) -> None:
        decision = box.decide(
            box.COST_MODEL, [], reading(available_gib=40.0, carveout_gib=None)
        )
        self.assertTrue(decision.allowed, decision.reason)
        self.assertFalse(decision.checked_gpu)
        refused = box.decide(
            box.COST_MODEL, [], reading(available_gib=5.0, commit_gib=7.0, carveout_gib=None)
        )
        self.assertFalse(refused.allowed)
        self.assertIn(box.TEST_COMMIT, refused.refused_by)

    def test_a_floor_of_none_switches_that_check_off(self) -> None:
        open_floors = box.Floors(min_free_share=None, min_commit_gib=None, min_gpu_gib=None)
        decision = box.decide(
            box.COST_SUITE, [], reading(available_gib=0.5, commit_gib=0.5), open_floors
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.refused_by, ())
        self.assertTrue(all(not check.ran for check in decision.checks))

    def test_the_refusal_names_the_running_step_that_holds_the_room(self) -> None:
        decision = box.decide(
            box.COST_MODEL,
            [box.StepCost("t-0042", box.COST_SUITE)],
            reading(available_gib=8.0),
        )
        self.assertIn("t-0042 (suite)", "\n".join(decision.lines))


class FourFixtureTest(unittest.TestCase):
    """The four cases the job names, each with the numbers it names.

    One fixture per behaviour that moved: the measured box must send, a box whose
    commit headroom is gone must refuse and say commit, a small box whose free
    memory is nearly gone must refuse on free, and a big box with room must send.
    Each refusal also pins the readout: it must name the test that refused.
    """

    def test_fixture_one_the_measured_box_sends_a_suite(self) -> None:
        # This machine on 2026-11-16: visible 31.8 GiB, commit limit 95.8 GiB,
        # 33.0 GiB committed (62.8 GiB of headroom), 7.2 GiB free. A 3.0 GiB suite
        # must send, and it is the commit headroom that carries it.
        decided = box.decide(box.COST_SUITE, [], measured())
        self.assertTrue(decided.allowed, decided.reason)
        self.assertEqual(decided.refused_by, ())
        commit_check = next(c for c in decided.checks if c.name == box.TEST_COMMIT)
        memory_check = next(c for c in decided.checks if c.name == box.TEST_MEMORY)
        # 62.8 - 3.0 = 59.8 GiB of headroom left, against a 6 GiB floor.
        self.assertAlmostEqual(commit_check.left_gib, 59.8, places=1)
        # 7.2 - 3.0 = 4.2 GiB free left, against a tenth of 31.8 GiB: 3.18 GiB.
        self.assertAlmostEqual(memory_check.floor_gib, 3.18, places=2)
        self.assertAlmostEqual(memory_check.left_gib, 4.2, places=1)
        self.assertIn("commit headroom", decided.reason)

    def test_fixture_two_the_same_box_without_commit_headroom_refuses_on_commit(self) -> None:
        # The same machine with free 4.0 and 88.0 GiB committed, so 7.8 GiB of
        # headroom: a suite would leave 4.8 GiB, under the 6 GiB floor.
        decided = box.decide(box.COST_SUITE, [], measured(available_gib=4.0, commit_gib=7.8))
        self.assertFalse(decided.allowed)
        self.assertIn(box.TEST_COMMIT, decided.refused_by)
        self.assertIn("commit and memory test refused it", decided.reason)
        self.assertIn("6.0 GiB of commit headroom must stay free", "\n".join(decided.lines))
        refused_readout = "\n".join(box.readout(measured(available_gib=4.0, commit_gib=7.8)))
        self.assertIn("commit and memory test refused it", refused_readout)
        # And the readout says which test refused, in the test's own line as well.
        self.assertIn("test commit: refuses", refused_readout)

    def test_fixture_three_a_16_gib_box_with_4_gib_free_refuses(self) -> None:
        # A small machine: 16 GiB visible with no page file, so 4 GiB of headroom is
        # all the promise left and a 3.0 GiB step leaves 1.0 GiB - thin. The free
        # floor is a tenth with the 1.5 GiB floor under it: 1.6 GiB, and 1.0 GiB is
        # under that too. Both tests refuse, and the refusal names both.
        small = reading(
            available_gib=4.0,
            visible_gib=16.0,
            commit_limit_gib=16.0,
            commit_gib=4.0,
        )
        decided = box.decide(box.COST_SUITE, [], small)
        self.assertFalse(decided.allowed)
        self.assertEqual(decided.refused_by, (box.TEST_COMMIT, box.TEST_MEMORY))
        memory_check = next(c for c in decided.checks if c.name == box.TEST_MEMORY)
        self.assertAlmostEqual(memory_check.floor_gib, 1.6, places=2)
        self.assertIn("commit and memory test refused it", decided.reason)
        text = "\n".join(box.readout(small))
        self.assertIn("test memory: refuses", text)
        self.assertIn("test commit: refuses", text)

    def test_fixture_four_a_128_gib_box_with_40_gib_free_sends(self) -> None:
        # The other end: 12.8 GiB would be a tenth of this machine, which it would
        # never miss, so the ceiling holds the floor at 4.0 GiB and the step sends.
        big = reading(
            available_gib=40.0,
            visible_gib=128.0,
            commit_limit_gib=192.0,
            commit_gib=132.0,
        )
        decided = box.decide(box.COST_SUITE, [], big)
        self.assertTrue(decided.allowed, decided.reason)
        memory_check = next(c for c in decided.checks if c.name == box.TEST_MEMORY)
        self.assertAlmostEqual(memory_check.floor_gib, 4.0, places=2)
        self.assertIn("ceiling", memory_check.floor_note)

    def test_a_crowded_card_is_refused_by_the_gpu_test(self) -> None:
        # Not one of the four: the GPU test still has to refuse on its own, so a
        # second model leg next to the resident 27B is stopped by the card and the
        # readout says so.
        resident = [box.StepCost("t-0042", box.COST_MODEL)]
        roomy = measured(available_gib=24.0)
        decided = box.decide(box.COST_MODEL, resident, roomy)
        self.assertFalse(decided.allowed)
        self.assertEqual(decided.refused_by, (box.TEST_GPU,))
        self.assertIn("GPU test refused it", decided.reason)
        text = "\n".join(box.readout(roomy, resident))
        self.assertIn("test GPU: refuses", text)
        self.assertIn("test memory: passes", text)

    def test_every_test_is_recorded_with_the_numbers_it_compared(self) -> None:
        # A refusal must be checkable by hand: each test says what would be left and
        # the floor it was compared with, whether it passed or refused.
        decided = box.decide(box.COST_MODEL, [], measured(available_gib=2.0))
        self.assertEqual(
            [check.name for check in decided.checks],
            [box.TEST_COMMIT, box.TEST_MEMORY, box.TEST_GPU],
        )
        for check in decided.checks:
            self.assertIsNotNone(check.left_gib)
            self.assertIsNotNone(check.floor_gib)
        self.assertIn(
            "test commit: passes", "\n".join(box.readout(measured(available_gib=2.0)))
        )


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
        tight = reading(available_gib=5.0, commit_gib=8.0)
        refused = box.readout(tight)
        decision = box.decide(box.COST_SUITE, [], tight)
        self.assertFalse(decision.allowed)
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
