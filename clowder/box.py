"""What a step costs, and what the box has left.

`--heavy` was one boolean set by feel. Two failures came from that. A worker that
only edited files held the crew's only heavy slot for twenty minutes, because the
front door had said "heavy" as a precaution; and a step that reads a large
repository cost nothing and was never marked at all. Nothing measured the machine
either: the two [gates] thresholds were unset, so the only gate was a state flag.

This module replaces the flag with a class, a stated footprint, and one
comparison against what the box has free right now. Everything a refusal says is a
number a person can check with `clowder box`.

THE COST CLASSES, and what each costs on this box.

Measured, or built on a measurement, on 2026-10-27 (t-0986):

    class    memory   GPU      CPU-minutes   note
    none     0.0 GiB  0.0      0             an answer, an edit, a report
    read     1.0 GiB  0.0      a few         a large repository or its history
    suite    3.0 GiB  0.0      7             the clowder suite: 7 minutes, small
    install  2.0 GiB  0.0      5             an install: minutes, disk
    browser  6.0 GiB  3.0      60            a 113-story browser leg: about an hour
    model    4.0 GiB  26.0     60            the 27B: 25.35 GiB resident in the GPU's
                                             32 GiB, about an hour; the host side is
                                             small because the weights sit on the card

The memory figure is what the step must have free on the CPU side (free plus
standby, `GlobalMemoryStatusEx` `ullAvailPhys`). The GPU figure is what it takes
from the 32 GiB the GPU reserves for itself - the memory Windows cannot see, which
is why a resident 27B model used to look like an idle machine. A model's host share
is small for that reason: charging its 25 GiB to the CPU pool as well would refuse
every model leg on a box that is running one perfectly well.

WHICH POOL DECIDES. Commit headroom, not free physical. On this box measured
2026-11-16 the two figures were 62.8 GiB of commit headroom and 7.2 GiB free, and a
3.0 GiB suite was refused on the free figure while the box could promise another
seven times that. Free physical is the reclaimable file cache as well, so it is a
snapshot of what is warm, not of what the box can meet.

THE RULE, in one sentence: a step sends when the commit headroom it leaves after
the step and the steps already running is at least `min_commit_gib`; the free
memory it leaves is compared with a tenth of the RAM Windows can see, held between
1.5 and 4.0 GiB, and that test confirms a refusal only when the commit headroom is
under its floor too - except that free below `HARD_MIN_FREE_GIB` refuses on its own,
whatever the commit headroom says. Every refusal names the test that refused.

WHY THE FREE FLOOR IS PROPORTIONAL AND CLAMPED. A fixed floor was written for one
size of machine. 4 GiB is 12.6 percent of this box's visible 31.8 GiB and 25
percent of a 16 GiB box, which is a quarter of the small machine's memory held as
reserve - the same number refuses a step on the box with the room while passing it
on the box without. A tenth scales with the machine; the clamp keeps it useful at
both ends, 1.5 GiB so a small box still keeps a reserve worth having, and 4.0 GiB
so a 128 GiB box does not reserve 12.8 GiB it will never miss.

The free test is kept as a second gate and not as a copy of the first: it says
whether the physical pool would be gone after the step, which is a fact a reader
needs and the commit figure cannot show. It does not refuse on its own, because
free physical is mostly reclaimable file cache - on this box a run of 7.2 GiB free
with 62.8 GiB of commit headroom behind it is a box with work in it, not a box
without room. Both numbers are printed either way, so a reader can see the pool
that came close, and the test line says plainly when free was under its floor and
the commit headroom covered it.

THE HARD FLOOR. One narrow case still has to refuse on free alone: a step that
would leave the box below `HARD_MIN_FREE_GIB`, where Windows pages itself into a
crawl no matter what the commit limit promises. That is a guard, not a reserve,
and it is set below every proportional floor (the smallest is the 1.5 GiB clamp),
so it can never fire in ordinary operation - not on this box at 7-10 GiB free, and
not on a small box where the proportional floor is already doing the work.

`model` is the one class that takes the box alone. That is the old one-heavy-step
state gate, kept where it is still true: the GPU counter is too slow to put in a
gate, so the crew cannot measure two model legs against each other - the record
says one at a time. A refused overlap there costs nothing but a wait; a second
model leg would cost the box.
"""

from __future__ import annotations

import csv
import os
import shutil
import subprocess
from collections.abc import Sequence
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime

from . import load

COST_NONE = "none"
COST_READ = "read"
COST_SUITE = "suite"
COST_INSTALL = "install"
COST_BROWSER = "browser"
COST_MODEL = "model"

# The closed set, in the order a person reads it: cheapest first.
COST_ORDER = (COST_NONE, COST_READ, COST_SUITE, COST_INSTALL, COST_BROWSER, COST_MODEL)
COSTS = COST_ORDER

GIB = load.GIB


@dataclass(frozen=True)
class CostClass:
    """One class of step, and what it costs."""

    name: str
    # GiB of the CPU-side pool it must have free while it runs.
    memory_gib: float
    # GiB it takes from the memory the GPU set aside for itself.
    gpu_gib: float
    # Rough wall-clock minutes on this box; advice, never a gate.
    cpu_minutes: float
    # The class takes the box alone: the record keeps one at a time.
    alone: bool = False
    what: str = ""


COST_CLASSES: dict[str, CostClass] = {
    COST_NONE: CostClass(
        COST_NONE, 0.0, 0.0, 0, what="an answer, an edit or a report - no run"
    ),
    COST_READ: CostClass(
        COST_READ, 1.0, 0.0, 3, what="reads a large repository or its history"
    ),
    COST_SUITE: CostClass(
        COST_SUITE, 3.0, 0.0, 7, what="runs a test suite; the clowder suite is 7 minutes"
    ),
    COST_INSTALL: CostClass(
        COST_INSTALL, 2.0, 0.0, 5, what="installs packages or builds; minutes, and disk"
    ),
    COST_BROWSER: CostClass(
        COST_BROWSER,
        6.0,
        3.0,
        60,
        what="drives a browser; a 113-story leg is about an hour",
    ),
    COST_MODEL: CostClass(
        COST_MODEL,
        4.0,
        26.0,
        60,
        alone=True,
        what="runs the 27B model; 25.35 GiB of the GPU's 32 GiB, and its host share",
    ),
}


def cost_class(name: str) -> CostClass:
    """The class for a name, or a ValueError naming the whole set.

    An unknown class is a refusal, not a fallback: quietly treating a typo as
    `none` would send the one step that needed measuring.
    """
    try:
        return COST_CLASSES[name]
    except KeyError:
        raise ValueError(
            f"unknown cost class {name!r}. One of: {', '.join(COST_ORDER)}"
        ) from None


def cost_of(record: object) -> str:
    """The class of a task or queued item, including one written before classes.

    A record carrying only the old `heavy` flag is a `suite`: that is what the flag
    meant, so every brief and every state file written before this change reads
    the same way it used to.
    """
    stated = str(getattr(record, "cost", "") or "").strip().lower()
    if stated in COST_CLASSES:
        return stated
    return COST_SUITE if getattr(record, "heavy", False) else COST_NONE


def heavy_flag(cost: str) -> bool:
    """Whether the old `heavy` marker is set, so older readers still understand it.

    It stays true for a suite only. The marker line and the board do not need it -
    they read the class - but a state file is read by more than one version.
    """
    return cost == COST_SUITE


# The free floor is a share of the visible RAM, held between these two, so a small
# box keeps a reserve worth having and a huge one does not reserve memory it will
# never miss. See the module docstring for why each number is what it is.
FREE_FLOOR_MIN_GIB = 1.5
FREE_FLOOR_MAX_GIB = 4.0

# The last-resort guard: free memory the box must have left, whatever the commit
# headroom says. It refuses on its own, and it is the only way the free side can.
#
# Why 1.0 and not 1.5: every proportional floor is at least 1.5 GiB (that is the
# clamp), so a 1.5 GiB guard would sit exactly on top of the smallest proportional
# floor and fire in ordinary operation on a 16 GiB box, quietly undoing the rule
# that free confirms rather than refuses. At 1.0 GiB the guard is strictly below
# every proportional floor, so it can only fire when the box is genuinely out -
# about the point where Windows' own allocations start to fail and every running
# step slows to a crawl. This machine reads 7-10 GiB free, so it never comes near.
HARD_MIN_FREE_GIB = 1.0


@dataclass(frozen=True)
class Floors:
    """What must still be free after the step starts.

    A floor is not a cost: it is the reserve that keeps the box usable. `None`
    switches that floor off.

    The free floor is a share of the memory Windows can see (a tenth by default),
    held between 1.5 and 4.0 GiB, not a fixed number: a fixed 4 GiB is 12.6 percent
    of a 31.8 GiB machine and 25 percent of a 16 GiB one, so it refused work on the
    box with the room. The commit and GPU floors stay GiB, because those pools are
    limits rather than memory the box has: 6 GiB of commit headroom is a promise the
    box can still keep, and 2 GiB of the card keeps it usable after the step. The
    hard free floor is the one absolute reserve: below it the box pages itself into
    a crawl, so it refuses on its own rather than confirming a commit refusal.
    """

    min_free_share: float | None = 0.10
    min_commit_gib: float | None = 6.0
    min_gpu_gib: float | None = 2.0
    # The last-resort guard, below every proportional floor: free memory that must be
    # left whatever the commit headroom says. See the constant for why it is 1.0 GiB.
    min_hard_free_gib: float | None = HARD_MIN_FREE_GIB

    def free_floor_gib(self, visible_gib: float | None) -> float | None:
        """The free memory that must stay, in GiB, or None when it cannot be told.

        A share of an unknown visible amount is unknown. The gate skips that test
        rather than invent a floor, and the readout says the test did not run.
        """
        if self.min_free_share is None or visible_gib is None:
            return None
        share = self.min_free_share * visible_gib
        return min(max(share, FREE_FLOOR_MIN_GIB), FREE_FLOOR_MAX_GIB)

    def free_floor_note(self, visible_gib: float | None) -> str:
        """Where the free floor came from, for a line a person can check by hand."""
        if self.min_free_share is None:
            return "that test is switched off"
        if visible_gib is None:
            return "the visible amount could not be read"
        share = self.min_free_share * visible_gib
        where = f"{self.min_free_share:.0%} of the {visible_gib:.1f} GiB Windows sees"
        if share < FREE_FLOOR_MIN_GIB:
            return f"{where}, held up to the {FREE_FLOOR_MIN_GIB:.1f} GiB floor"
        if share > FREE_FLOOR_MAX_GIB:
            return f"{where}, held down to the {FREE_FLOOR_MAX_GIB:.1f} GiB ceiling"
        return where


FLOORS = Floors()


@dataclass(frozen=True)
class StepCost:
    """One step in flight: its id, and the class it costs."""

    id: str
    cost: str


def steps_in_flight(tasks: Sequence[object]) -> list[StepCost]:
    """The cost of each open step, for a board line, a start order or a gate."""
    return [StepCost(id=str(getattr(task, "id", "?")), cost=cost_of(task)) for task in tasks]


# The variable the multiplexer sets inside a pane. It is how `clowder box` knows which
# open step is asking, so that step's cost is not subtracted twice.
ASKING_PANE_ENV = "HERDR_PANE_ID"


def asking_pane() -> str | None:
    """The pane this command is running in, or None when it is not in one."""
    return os.environ.get(ASKING_PANE_ENV) or None


def asking_step(tasks: Sequence[object], pane_id: str | None) -> str | None:
    """The id of the open step running in this pane, or None when there is none.

    `clowder box` is usually run from inside a worker's pane, so the step asking is
    often also an open step in the record. Naming it lets the readout leave it out of
    the gate lines, where its cost would otherwise be subtracted twice: once among the
    running steps and once as the step being considered. The newest match wins, because
    a pane can hold a stale open step next to the live one.
    """
    if not pane_id:
        return None
    matches = [task for task in tasks if str(getattr(task, "pane_id", "") or "") == pane_id]
    if not matches:
        return None
    newest = max(matches, key=lambda task: str(getattr(task, "created_at", "") or ""))
    return str(getattr(newest, "id", "") or "") or None


def steps_for_gate(steps: Sequence[StepCost], asking: str | None = None) -> list[StepCost]:
    """The running steps a gate compares: every one of them except the step asking."""
    return [step for step in steps if step.id != asking]


@dataclass(frozen=True)
class BoxReading:
    """One read of the box. Every figure is bytes unless the name says otherwise.

    `unread` names what could not be read and why, one short phrase each, so the
    readout can say what is missing instead of printing zero.
    """

    read_at: str
    installed_bytes: int | None = None
    total_bytes: int | None = None
    available_bytes: int | None = None
    commit_limit_bytes: int | None = None
    commit_headroom_bytes: int | None = None
    percent: float | None = None
    carveout_bytes: int | None = None
    gpu_used_bytes: int | None = None
    gpu_adapter: str | None = None
    gpu_holder: str | None = None
    pagefile_bytes: int | None = None
    pagefile_percent: float | None = None
    unread: tuple[str, ...] = ()

    @property
    def gpu_free_bytes(self) -> int | None:
        """The GPU's own memory still free, or None when either side is unknown."""
        if self.carveout_bytes is None or self.gpu_used_bytes is None:
            return None
        return max(0, self.carveout_bytes - self.gpu_used_bytes)


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _typeperf(counter: str, timeout_s: float = 20.0) -> list[str] | None:
    """One sample of a performance counter, as the header and the value row.

    Shelling out is fine for the readout and far too slow for a gate, which is why
    `read(with_gpu=False)` - the one the gate uses - never calls this. `None` means
    the counter could not be read at all: no typeperf, no such counter set, or a
    sample that timed out.
    """
    exe = shutil.which("typeperf")
    if exe is None:
        return None
    try:
        done = subprocess.run(
            [exe, counter, "-sc", "1"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    rows = [line for line in done.stdout.splitlines() if line.strip().startswith('"')]
    return rows[:2] if len(rows) >= 2 else None


def _counter_columns(counter: str) -> list[tuple[str, float]] | None:
    """Each instance of one counter and its value, or None when unreadable."""
    rows = _typeperf(counter)
    if rows is None:
        return None
    try:
        names = next(csv.reader([rows[0]]))
        values = next(csv.reader([rows[1]]))
    except csv.Error, StopIteration:
        return None
    if len(values) != len(names):
        return None
    out: list[tuple[str, float]] = []
    for name, value in zip(names[1:], values[1:], strict=False):
        try:
            out.append((name, float(value)))
        except ValueError:
            continue
    return out


def gpu_used() -> tuple[int, str | None] | None:
    """The largest GPU's dedicated memory in use, in bytes, and which adapter.

    `GPU Adapter Memory(*)\\Dedicated Usage` is the only counter that sees a model
    resident in the GPU's own memory. The largest adapter is taken because the
    carve-out read from the registry is the largest card: subtracting a smaller
    card's use from a bigger card's size would invent free memory.
    """
    columns = _counter_columns(r"\GPU Adapter Memory(*)\Dedicated Usage")
    if columns is None:
        return None
    if not columns:
        return 0, None
    name, value = max(columns, key=lambda pair: pair[1])
    return int(value), _adapter_label(name)


def _adapter_label(instance: str) -> str:
    """`...GPU Adapter Memory(luid_..._phys_0)\\Dedicated Usage` as a short label.

    The instance name arrives wrapped in its counter path, so the adapter is the
    thing inside the parentheses that mentions a LUID, not the first bracket of the
    line - and not the counter name that follows it.
    """
    start = instance.find("(luid_")
    if start < 0:
        start = instance.find("(")
    end = instance.find(")", start) if start >= 0 else -1
    if start < 0 or end < 0:
        return instance
    inner = instance[start + 1 : end]
    luid = inner.split("luid_", 1)[-1].split("_phys_")[0] if "luid_" in inner else ""
    phys = inner.rsplit("_", 1)[-1]
    parts = [
        part
        for part in (f"luid {luid}" if luid else "", f"phys {phys}" if phys.isdigit() else "")
        if part
    ]
    return ", ".join(parts) or inner


def gpu_holder() -> str | None:
    """The process holding the most GPU memory, or None when it cannot be told.

    Best effort, for the readout only. A machine with no GPU process counter, or a
    pid that has already gone, prints nothing rather than a guess.
    """
    columns = _counter_columns(r"\GPU Process Memory(*)\Dedicated Usage")
    if not columns:
        return None
    instance, value = max(columns, key=lambda pair: pair[1])
    if value < 256 * 1024 * 1024:  # below a quarter GiB, nothing is holding a model
        return None
    pid = _pid_of(instance)
    if pid is None:
        return f"pid unknown, {_gib(value)} GiB"
    name = _process_name(pid)
    where = f"{name} (pid {pid})" if name else f"pid {pid}"
    return f"{where}, {_gib(value)} GiB"


def _pid_of(instance: str) -> int | None:
    """The pid inside a `GPU Process Memory(pid_1234_luid_...)` instance name."""
    marker = "pid_"
    start = instance.find(marker)
    if start < 0:
        return None
    digits = ""
    for char in instance[start + len(marker) :]:
        if not char.isdigit():
            break
        digits += char
    return int(digits) if digits else None


def _process_name(pid: int) -> str | None:
    """The image name of a running pid, on Windows, or None.

    Only the readout uses this, and only to answer "which process holds it". A pid
    that has gone, or a handle that cannot be opened, prints nothing rather than a
    guess. The prototype is stated in full because a 64-bit handle read as an int is
    truncated on this box, and a truncated handle opens nothing.
    """
    try:
        import ctypes

        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return None
        kernel32 = windll.kernel32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # query limited information
        if not handle:
            return None
        try:
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(1024)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return None
            return os.path.basename(buffer.value) or None
        finally:
            kernel32.CloseHandle(handle)
    except OSError, AttributeError, ValueError:
        return None


def pagefile_percent() -> float | None:
    """How much of the page file is in use, percent, or None.

    This is the direct swap symptom. The commit ratio is not: on this box it read 62
    percent while the page file was 0.39 percent used, so it says how much is
    promised, not how much is spilled.
    """
    columns = _counter_columns(r"\Paging File(_Total)\% Usage")
    if not columns:
        return None
    return columns[0][1]


def read(*, with_gpu: bool = False) -> BoxReading:
    """One read of the box.

    `with_gpu` adds the two counter reads the readout shows and the gate must not
    pay for: each shells out to typeperf and takes seconds. With it off, this is the
    ctypes read the gate uses, and it stays well under a second.
    """
    memory = load.memory_reading()
    installed = load.installed_bytes()
    carveout = load.gpu_carveout_bytes()
    unread: list[str] = []
    if memory is None:
        unread.append("memory (GlobalMemoryStatusEx did not answer)")
    if installed is None:
        unread.append("installed RAM (the SMBIOS table could not be read)")
    if carveout is None:
        unread.append("the GPU's own memory (no registry value under the display class)")

    available = None if memory is None else memory.available_bytes
    commit_headroom = None if memory is None else memory.commit_headroom_bytes
    commit_limit = None if memory is None else memory.commit_limit_bytes
    total = None if memory is None else memory.total_bytes
    percent = None if memory is None else memory.percent

    pagefile_bytes = None
    if commit_limit is not None and total is not None:
        pagefile_bytes = max(0, commit_limit - total)

    gpu_used_bytes = None
    adapter = None
    holder = None
    pagefile_used_percent = None
    if with_gpu:
        used = gpu_used()
        if used is None:
            unread.append("GPU use (no GPU Adapter Memory counter on this machine)")
        else:
            gpu_used_bytes = used[0]
            adapter = used[1]
            holder = gpu_holder()
        pagefile_used_percent = pagefile_percent()
        if pagefile_used_percent is None:
            unread.append("page file use (no Paging File counter on this machine)")

    return BoxReading(
        read_at=_stamp(),
        installed_bytes=installed,
        total_bytes=total,
        available_bytes=available,
        commit_limit_bytes=commit_limit,
        commit_headroom_bytes=commit_headroom,
        percent=percent,
        carveout_bytes=carveout,
        gpu_used_bytes=gpu_used_bytes,
        gpu_adapter=adapter,
        gpu_holder=holder,
        pagefile_bytes=pagefile_bytes,
        pagefile_percent=pagefile_used_percent,
        unread=tuple(unread),
    )


def _gib(value: int | float | None) -> str:
    """Bytes as GiB, one decimal, or a dash. Never a bare zero for unknown."""
    return "-" if value is None else f"{value / GIB:.1f}"


TEST_MEMORY = "memory"
TEST_COMMIT = "commit"
TEST_HARD_FREE = "hard free"
TEST_GPU = "GPU"


@dataclass(frozen=True)
class TestResult:
    """One test the gate ran: what would be left, the floor, and where it came from.

    A floor of `None` means the test did not run - there was no share to compute, or
    the floor is switched off - while a `None` left figure means the reading could
    not tell. The two are different facts, and a readout that printed zero for
    either would be inventing one.
    """

    name: str
    left_gib: float | None
    floor_gib: float | None
    floor_note: str = ""
    # When set, being under this test's own floor only refuses if the test named
    # here is also under its floor. The free test confirms a refusal; it does not
    # make one, because free physical is mostly file cache and refusing on it alone
    # is the mistake this gate was changed to end.
    confirms: str | None = None

    @property
    def ran(self) -> bool:
        return self.floor_gib is not None and self.left_gib is not None

    @property
    def missed(self) -> bool:
        """True when this test's own numbers are under its own floor."""
        return self.ran and self.left_gib < self.floor_gib  # type: ignore[operator]

    @property
    def margin(self) -> float:
        """How far clear of its floor this test is; negative when it is under it."""
        return 0.0 if not self.ran else self.left_gib - self.floor_gib  # type: ignore[operator]

    def line(self, counts: bool) -> str:
        """One line for the readout: the test, its numbers, and its verdict.

        `counts` says whether this test is one the refusal turned on, so a test that
        is under its floor without refusing says exactly that instead of a bare
        verdict a reader would have to reconcile with the gate's own answer.
        """
        if not self.ran:
            return f"test {self.name}: did not run ({self.floor_note or 'no floor'})"
        note = f", {self.floor_note}" if self.floor_note else ""
        if not self.missed:
            verdict = "passes"
        elif counts:
            verdict = "refuses"
        else:
            verdict = (
                f"under its floor, but the {self.confirms} test had room, so it did not refuse"
            )
        return (
            f"test {self.name}: {verdict} - {self.left_gib:.1f} GiB would be left "
            f"against a {self.floor_gib:.1f} GiB floor{note}"
        )


@dataclass(frozen=True)
class Decision:
    """What the gate says about one step, and why, in the numbers it compared."""

    allowed: bool
    cost: str
    reason: str
    # The measured lines a refusal prints: one per pool that fell short.
    lines: list[str] = field(default_factory=list)
    # Every test that ran, in the order the gate checks them, so a readout can say
    # which one refused instead of only which pool fell short.
    checks: tuple[TestResult, ...] = ()
    # Cheap flags for a caller: which checks were actually made.
    checked_memory: bool = False
    checked_gpu: bool = False
    # True when the reading was missing, so the gate stood aside.
    unmeasured: bool = False

    @property
    def refused_by(self) -> tuple[str, ...]:
        """The tests the refusal turned on, in the order they are checked.

        A test that confirms another one - the free test confirms commit headroom -
        counts only when the test it names is under its floor too. That is the rule
        that stopped this box refusing work it could do: free physical is mostly
        file cache, so a low free figure with 50 GiB of commit headroom behind it is
        not a reason to refuse.
        """
        return _refusing_names(self.checks)

    @property
    def refused_by_label(self) -> str:
        """Those names as a phrase: "commit", "memory and GPU"."""
        return _tests_label(self.refused_by)

    @property
    def tightest(self) -> TestResult | None:
        """The test with the least room left, whether or not it refused."""
        ran = [check for check in self.checks if check.ran]
        return min(ran, key=lambda check: check.margin) if ran else None

    def _tightest_refusing(self) -> TestResult | None:
        names = self.refused_by
        refusing = [check for check in self.checks if check.name in names]
        return min(refusing, key=lambda check: check.margin) if refusing else None

    @property
    def left_gib(self) -> float | None:
        """What the tightest refusing test would leave, or the tightest of all."""
        tightest = self._tightest_refusing() or self.tightest
        return None if tightest is None else tightest.left_gib

    @property
    def floor_gib(self) -> float | None:
        """The floor that went with `left_gib`."""
        tightest = self._tightest_refusing() or self.tightest
        return None if tightest is None else tightest.floor_gib


def _refusing_names(checks: Sequence[TestResult]) -> tuple[str, ...]:
    """The tests the refusal turned on, in the order they are checked.

    A test that confirms another one - the free test confirms commit headroom -
    counts only when the test it names is under its floor too. That is the rule that
    stopped this box refusing work it could do: free physical is mostly file cache,
    so a low free figure with 50 GiB of commit headroom behind it is not a reason to
    refuse.
    """
    by_name = {check.name: check for check in checks}
    refusing: list[str] = []
    for check in checks:
        if not check.missed:
            continue
        if check.confirms is not None and not by_name[check.confirms].missed:
            continue
        refusing.append(check.name)
    return tuple(refusing)


def _tests_label(names: Sequence[str]) -> str:
    """Test names as a phrase: "commit", "memory and GPU", "memory, commit and GPU"."""
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def decide(
    cost: str,
    steps: Sequence[StepCost] = (),
    reading: BoxReading | None = None,
    floors: Floors = FLOORS,
) -> Decision:
    """Would this step send now? Compare the pools the box can meet.

    The comparison is a subtraction, not a percentage, and the pool that decides is
    commit headroom: it says how much more the box can promise, where free physical
    counts the file cache and so reads low on a box with nothing wrong with it. The
    free test compares what is left with a share of the memory Windows can see
    (`min_free_share`, clamped), and confirms a refusal rather than making one; the
    GPU test compares `min_gpu_gib` of the memory the GPU reserved and refuses on its
    own. Running steps are subtracted first: that is what makes two suites fit where
    a suite and a model leg do not.

    Every test that runs is recorded in `checks` with the number it compared, so a
    refusal names the test that refused as well as the pool it measured.

    A class with no cost (`none`) is never refused, and a box that cannot be read
    fails open. A gate that cannot measure must not invent a refusal.
    """
    klass = cost_class(cost)
    if klass.name == COST_NONE:
        return Decision(
            True,
            cost,
            "a step with no stated cost is never refused",
            checked_memory=False,
            checked_gpu=False,
        )
    if reading is None or reading.available_bytes is None:
        return Decision(
            True,
            cost,
            "the box's memory could not be read, so the gate could not measure it",
            unmeasured=True,
        )

    held = [step for step in steps if klass_of(step.cost).name != COST_NONE]
    memory_held = sum(klass_of(step.cost).memory_gib for step in held)
    gpu_held = sum(klass_of(step.cost).gpu_gib for step in held)
    listed = ", ".join(f"{step.id} ({step.cost})" for step in held) or "no running step"

    lines: list[str] = []
    checks: list[TestResult] = []
    free = reading.available_bytes / GIB
    commit = (reading.commit_headroom_bytes or 0) / GIB
    visible = None if reading.total_bytes is None else reading.total_bytes / GIB
    need = klass.memory_gib

    # Commit headroom first: it is the pool that decides, so it is the first test a
    # reader meets and the first the refusal text names.
    commit_left = commit - memory_held - need
    checks.append(
        TestResult(TEST_COMMIT, commit_left, floors.min_commit_gib, "a fixed reserve")
    )
    if floors.min_commit_gib is not None and commit_left < floors.min_commit_gib:
        lines.append(
            f"commit: {commit:.1f} GiB of headroom, minus {memory_held:.1f} GiB held by "
            f"{len(held)} running step(s) ({listed}), minus {need:.1f} GiB this "
            f"{klass.name} step needs, leaves {commit_left:.1f} GiB; "
            f"{floors.min_commit_gib:.1f} GiB of commit headroom must stay free"
        )

    # The free test confirms the commit one. Free physical is mostly reclaimable file
    # cache, so it is under its floor on a healthy box whenever something is running;
    # refusing on it alone is the mistake this change ends.
    memory_left = free - memory_held - need
    free_floor = floors.free_floor_gib(visible)
    checks.append(
        TestResult(
            TEST_MEMORY,
            memory_left,
            free_floor,
            floors.free_floor_note(visible),
            confirms=TEST_COMMIT,
        )
    )
    if free_floor is not None and memory_left < free_floor:
        lines.append(
            f"memory: {free:.1f} GiB free, minus {memory_held:.1f} GiB held by "
            f"{len(held)} running step(s) ({listed}), minus {need:.1f} GiB this "
            f"{klass.name} step needs, leaves {memory_left:.1f} GiB; "
            f"{free_floor:.1f} GiB must stay free "
            f"({floors.free_floor_note(visible)})"
        )

    # The last resort: below this much free the box pages itself into a crawl, so
    # this test refuses on its own, whatever the commit limit promises. It sits below
    # every proportional floor, so ordinary operation never reaches it.
    checks.append(
        TestResult(TEST_HARD_FREE, memory_left, floors.min_hard_free_gib, "a fixed reserve")
    )
    if floors.min_hard_free_gib is not None and memory_left < floors.min_hard_free_gib:
        lines.append(
            f"free floor: {free:.1f} GiB free, minus {memory_held:.1f} GiB held by "
            f"{len(held)} running step(s) ({listed}), minus {need:.1f} GiB this "
            f"{klass.name} step needs, leaves {memory_left:.1f} GiB; the hard floor of "
            f"{floors.min_hard_free_gib:.1f} GiB must stay free whatever the commit "
            f"headroom says"
        )

    checked_gpu = False
    if klass.gpu_gib > 0 and reading.carveout_bytes is not None:
        checked_gpu = True
        gpu_size = reading.carveout_bytes / GIB
        gpu_left = gpu_size - gpu_held - klass.gpu_gib
        checks.append(
            TestResult(
                TEST_GPU,
                gpu_left,
                floors.min_gpu_gib,
                f"of the {gpu_size:.1f} GiB the GPU reserved",
            )
        )
        if floors.min_gpu_gib is not None and gpu_left < floors.min_gpu_gib:
            lines.append(
                f"GPU: {gpu_size:.1f} GiB is set aside for the GPU, minus "
                f"{gpu_held:.1f} GiB held by the crew, minus {klass.gpu_gib:.1f} "
                f"GiB this {klass.name} step needs, leaves {gpu_left:.1f} GiB; "
                f"{floors.min_gpu_gib:.1f} GiB must stay free"
            )

    refused_by = _refusing_names(checks)
    if refused_by:
        reason = (
            f"a {klass.name} step needs {need:.1f} GiB of memory"
            + (f" and {klass.gpu_gib:.1f} GiB of the GPU's" if klass.gpu_gib else "")
            + f", and the box does not have it free; the "
            f"{_tests_label(refused_by)} test refused it"
        )
        return Decision(
            False,
            cost,
            reason,
            lines=lines,
            checks=tuple(checks),
            checked_memory=free_floor is not None,
            checked_gpu=checked_gpu,
        )

    memory_check = next(check for check in checks if check.name == TEST_MEMORY)
    if memory_check.ran and memory_check.missed:
        tail = (
            f"; free memory would be {memory_check.left_gib:.1f} GiB, under its "
            f"{memory_check.floor_gib:.1f} GiB floor, and the "
            f"{commit_left:.1f} GiB of commit headroom left covers it"
        )
    else:
        ran = [check for check in checks if check.ran]
        tightest = min(ran, key=lambda check: check.margin) if ran else None
        tail = (
            ""
            if tightest is None
            else f", the tightest test leaving {tightest.margin:.1f} GiB above its floor"
        )
    reason = (
        f"a {klass.name} step fits: {commit:.1f} GiB of commit headroom and "
        f"{free:.1f} GiB free, minus {memory_held:.1f} GiB held and {need:.1f} GiB "
        f"needed{tail}"
    )
    return Decision(
        True,
        cost,
        reason,
        lines=lines,
        checks=tuple(checks),
        checked_memory=free_floor is not None,
        checked_gpu=checked_gpu,
    )


def klass_of(name: str) -> CostClass:
    """A class by name, where an unknown name stands for `none`.

    A state file written by a newer copy may name a class this one does not know. It
    is read as no-cost rather than refused: the gate must never block a step because
    of a word it has not learned yet.
    """
    return cost_class(name) if name in COST_CLASSES else COST_CLASSES[COST_NONE]


def summary(
    reading: BoxReading | None,
    steps: Sequence[StepCost] = (),
    floors: Floors = FLOORS,
) -> str:
    """What the box has left, in one line, for `start` and the board.

    It answers the two questions a waiting step raises: how much is left, and would
    the step I am about to send go. A missing reading says so; it never says zero.
    """
    if reading is None or reading.available_bytes is None:
        return "box: not readable, so no step is refused for memory (the gate fails open)"
    held = [step for step in steps if klass_of(step.cost).name != COST_NONE]
    memory_held = sum(klass_of(step.cost).memory_gib for step in held)
    gpu_held = sum(klass_of(step.cost).gpu_gib for step in held)
    parts = [
        f"box: {_gib(reading.available_bytes)} GiB free",
        f"{_gib(reading.commit_headroom_bytes)} GiB of commit headroom",
    ]
    if reading.carveout_bytes is not None:
        parts.append(
            f"GPU {gpu_held:.1f} of {_gib(reading.carveout_bytes)} GiB held by the crew"
        )
    else:
        parts.append("GPU size not readable")
    if held:
        parts.append(f"{memory_held:.1f} GiB held by {len(held)} running step(s)")
    suite = decide(COST_SUITE, steps, reading, floors)
    model = decide(COST_MODEL, steps, reading, floors)
    verdicts = [_verdict("suite", suite), _verdict("model", model)]
    return "; ".join(parts) + ". " + "; ".join(verdicts) + "."


def _verdict(name: str, decision: Decision) -> str:
    """One clause saying whether that class would send now, and which test refused."""
    if decision.unmeasured:
        return f"a {name} step would send now, unmeasured"
    if decision.allowed:
        return f"a {name} step would send now"
    if decision.left_gib is None or decision.floor_gib is None:
        return f"a {name} step would not send; the {decision.refused_by_label} test refused it"
    return (
        f"a {name} step would not send; the {decision.refused_by_label} test refused it "
        f"({decision.left_gib:.1f} GiB would be left, "
        f"and {decision.floor_gib:.1f} GiB must stay free)"
    )


def _gate_line(name: str, decision: Decision) -> str:
    """One line from the gate: whether it would send, why, and the number behind it."""
    if decision.unmeasured:
        return f"gate now, {name}: sends, unmeasured - {decision.reason}"
    if decision.allowed:
        return f"gate now, {name}: sends - {decision.reason}"
    return f"gate now, {name}: is refused - {decision.reason}"


def readout(
    reading: BoxReading,
    steps: Sequence[StepCost] = (),
    floors: Floors = FLOORS,
    asking: str | None = None,
) -> list[str]:
    """The three pools in plain words, each with the metric it came from.

    The order is the order to read them in: what the box has, then the GPU's share
    of it, then the part a process can use. Every line names its source, so a figure
    can be checked by hand instead of trusted.

    `asking` is the id of the step that ran the command. It is listed with the crew,
    because it is running, but left out of the gate lines: those ask whether one more
    step of a class would send, and counting the asker as running would subtract its
    own cost twice.
    """
    installed = _gib(reading.installed_bytes)
    carveout = _gib(reading.carveout_bytes)
    total = _gib(reading.total_bytes)
    lines: list[str] = [
        f"read at {reading.read_at}",
        f"memory: {installed} GiB installed; {carveout} GiB is set aside for the GPU, "
        f"so Windows sees {total} GiB "
        f"(GetPhysicallyInstalledSystemMemory, and the registry value "
        f"HardwareInformation.qwMemorySize)",
    ]

    if reading.carveout_bytes is None:
        gpu_line = (
            "GPU side: not readable on this box (no registry value under the display "
            "class), so the gate skips the GPU check"
        )
    elif reading.gpu_used_bytes is None:
        gpu_line = (
            f"GPU side: {carveout} GiB set aside, use not read "
            f"(no GPU Adapter Memory counter on this machine)"
        )
    else:
        used = _gib(reading.gpu_used_bytes)
        free = _gib(reading.gpu_free_bytes)
        holder = f"; held by {reading.gpu_holder}" if reading.gpu_holder else ""
        which = f" ({reading.gpu_adapter})" if reading.gpu_adapter else ""
        gpu_line = (
            f"GPU side: {used} GiB in use of the {carveout} GiB the GPU reserved, "
            f"{free} GiB free (GPU Adapter Memory(*)\\Dedicated Usage, the largest "
            f"adapter{which}){holder}"
        )
    lines.append(gpu_line)

    if reading.available_bytes is None:
        lines.append(
            "CPU side: not readable on this box (GlobalMemoryStatusEx did not "
            "answer), so the gate fails open"
        )
    else:
        commit_used = None
        if reading.commit_limit_bytes is not None:
            commit_used = reading.commit_limit_bytes - (reading.commit_headroom_bytes or 0)
        page = ""
        if reading.pagefile_bytes is not None:
            page = f"; page file {_gib(reading.pagefile_bytes)} GiB"
        if reading.pagefile_percent is not None:
            in_use = reading.pagefile_percent / 100 * (reading.pagefile_bytes or 0)
            page += (
                f", {reading.pagefile_percent:.2f}% in use ({_gib(int(in_use))} GiB) "
                f"(Paging File(_Total)\\% Usage)"
            )
        lines.append(
            f"CPU side: {total} GiB visible, {_gib(reading.available_bytes)} GiB free "
            f"(GlobalMemoryStatusEx ullAvailPhys, the same number as "
            f"\\Memory\\Available MBytes); {_gib(commit_used)} GiB committed of a "
            f"{_gib(reading.commit_limit_bytes)} GiB limit, "
            f"{_gib(reading.commit_headroom_bytes)} GiB of headroom" + page
        )

    running = [step for step in steps if klass_of(step.cost).name != COST_NONE]
    if running:
        listed = ", ".join(f"{step.id} ({step.cost})" for step in running)
        crew = (
            f"crew: {len(running)} step(s) running, holding "
            f"{sum(klass_of(step.cost).memory_gib for step in running):.1f} GiB of "
            f"memory and "
            f"{sum(klass_of(step.cost).gpu_gib for step in running):.1f} GiB of the GPU "
            f"({listed})"
        )
    else:
        crew = "crew: no step is running, so the crew holds nothing"
    lines.append(crew)

    gate_steps = steps_for_gate(steps, asking)
    for name, cost in (("a suite step", COST_SUITE), ("a model step", COST_MODEL)):
        decision = decide(cost, gate_steps, reading, floors)
        lines.append(_gate_line(name, decision))
        lines.extend(f"  {line}" for line in decision.lines)
        # Each test on its own line, so the readout says which test refused and what
        # the other tests saw, instead of only naming the pool that came up short.
        refusing = decision.refused_by
        lines.extend(f"  {check.line(check.name in refusing)}" for check in decision.checks)

    if reading.unread:
        lines.append("could not be read: " + "; ".join(reading.unread))
    return lines
