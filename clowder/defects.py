"""Known defects: one file, one home.

A defect is a quirk, a trap or a workaround someone hit while using this tool: the
thing that cost time, in one line a person can read. Before this file, they lived
in the handover documents, which were written by hand and often not written at all,
so the same one could be recorded three times and still bite, or vanish for two
handovers and come back.

The rule this file exists for: if a defect is not here, it is not known. Nothing
else stores a defect. The handover prints a defect's one line and its id; it never
restates it. The board shows the ones seen twice with no job, because opening that
job is the front door's act.

One file per state file, beside it: `<state dir>/defects.json`. It is plain JSON and
written atomically, for the same reasons the state file is. A defect is keyed by its
normalised line, so adding the same one again is a second sighting, not a second
record.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .errors import StateError
from .state import file_lock
from .timeutil import now, now_iso, parse_iso, to_epoch, to_iso

SCHEMA_VERSION = 1

# A defect that has been seen once, one whose fix is a job, one whose fix has
# landed, and one the owner has accepted as a known cost. `open` and `has-job` are
# the ones a repetition still acts on.
OPEN = "open"
HAS_JOB = "has-job"
FIXED = "fixed"
ACCEPTED = "accepted-workaround"

STATES = (OPEN, HAS_JOB, FIXED, ACCEPTED)

# The states a second sighting does not need to act on. Everything else does: a job
# or an explicit accepted workaround with the reason.
SETTLED = (FIXED, ACCEPTED)


def defects_path(state_path: str | Path) -> Path:
    """The defects file that belongs to a state file: `defects.json` beside it."""
    return Path(state_path).with_name("defects.json")


def words(text: str) -> list[str]:
    """The words a defect line is searched by: lowercase runs of letters and digits.

    Punctuation is a separator, not a character, so "doesn't" and "doesnt" split the
    same way and a comma never hides a word.
    """
    return re.findall(r"[a-z0-9]+", normalise_line(text))


def matches_line(defect_line: str, query: str) -> bool:
    """Does the defect contain every word of the query? The `--like` search.

    Exact words only, all of them, in any order: "reports close step" finds "a step
    does not close when it reports". It cannot find a total rephrase that shares no
    word, and it must not try: aggressive matching would merge two real defects, which
    is worse than a miss a person can see. The miss is pinned by a test.
    """
    wanted = set(words(query))
    if not wanted:
        return False
    return wanted <= set(words(defect_line))


def normalise_line(text: str) -> str:
    """The key a defect is matched on.

    Case, whitespace runs and the punctuation at either end do not matter, because
    the same defect written twice by two sessions rarely matches byte for byte. The
    words in the middle do: a different sentence is a different defect.
    """
    return " ".join(text.split()).casefold().strip(".,;:!?`'\" ")


def moment_of(value: str) -> str:
    """A recorded moment from a date or an ISO timestamp, always a full ISO string.

    A bare date is taken as midnight UTC, which is what a handover gives: the day the
    defect was stated. Storing the moment rather than the date keeps the handover
    window exact, while `short_date` still shows people the day.
    """
    text = value.strip()
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        text = text + "T00:00:00Z"
    return to_iso(parse_iso(text))


def short_date(value: str | None) -> str:
    """The date part of a recorded moment, or `-`. The record keeps the time too."""
    if not value:
        return "-"
    text = value.strip()
    return text[:10] if len(text) >= 10 else text


@dataclass
class Defect:
    """One known defect, in the owner's words, with how often it has bitten."""

    id: str
    line: str
    first_seen: str
    last_seen: str
    count: int = 1
    # The job that fixes it, when one is open. `state` says where the defect is in
    # its life; `reason` is kept only for an accepted workaround.
    job: str | None = None
    # Every job that addresses it, oldest first. `job` is the same fact for a reader
    # that knows only one, and it is always the first entry here. A defect may be
    # fixed by two jobs, and a merge closes it only when it is the last of them.
    jobs: list[str] = field(default_factory=list)
    # What closed it without a person typing "fixed": the moment, the job and the
    # pull request. Empty on a defect fixed by hand.
    fixed_at: str | None = None
    fixed_pr: str | None = None
    # When a linked job closed without merging and put it back to open. It is the
    # moment the defect became actionable again, and the catch-up shows it again then -
    # `last_seen` has not moved, because nobody saw it happen. The count moves with it:
    # two reopens inside one second carry the same timestamp, and the count is what
    # makes the second one news.
    reopened_at: str | None = None
    reopen_seq: int = 0
    state: str = OPEN
    reason: str | None = None
    # The body behind the one line: the workaround, the citations, what was seen.
    # It lives here and nowhere else - not in a handover, not in a doc.
    detail: str | None = None
    # Fields a newer copy wrote that this code does not know. Kept, never dropped.
    extra: dict[str, object] = field(default_factory=dict, repr=False, compare=False)

    @property
    def seen_twice(self) -> bool:
        return self.count >= 2

    @property
    def settled(self) -> bool:
        return self.state in SETTLED

    @property
    def linked_jobs(self) -> list[str]:
        """Every open job that addresses this defect, oldest first.

        A record written before a defect could name more than one job carries only
        `job`; that one link is the whole list, so an old file reads the same way. A
        settled defect has no working links: its `job` is the one that fixed it, and
        `fixed_at` says when.
        """
        if self.jobs:
            return list(self.jobs)
        if self.job and self.state not in SETTLED:
            return [self.job]
        return []

    def needs_action(self, open_jobs: Sequence[str]) -> bool:
        """Seen at least twice, and neither a job nor an accepted workaround.

        This is the check `--repeated` and the board make. Any linked job that is
        still open silences it: the fix is in flight. A closed job counts as no job,
        because a branch that was closed without merging fixed nothing.
        """
        if not self.seen_twice or self.settled:
            return False
        return not any(job in set(open_jobs) for job in self.linked_jobs)

    def state_now(self, open_jobs: Sequence[str] = ()) -> str:
        """The state a reader should show, given which jobs are open.

        The stored state is what the last write knew. A link whose job has closed
        without merging is not "has-job" any more: nothing is in flight, so the
        defect is open again, and the repeat alarm must fire. Nothing here writes;
        it only stops a stale word from being shown as if it were true.
        """
        if self.state in SETTLED:
            return self.state
        links = self.linked_jobs
        if any(job in set(open_jobs) for job in links):
            return HAS_JOB
        return OPEN

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Defect:
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}  # type: ignore[attr-defined]
        extra = {key: value for key, value in data.items() if key not in known}
        clean = {key: value for key, value in data.items() if key in known}
        try:
            defect = cls(**clean)  # type: ignore[arg-type]
        except TypeError as exc:
            raise StateError(f"defect record is malformed: {exc}") from exc
        defect.extra = extra
        # One source of truth for the links: `jobs` holds them all, `job` is the first.
        # A file written before the list existed carries only `job`.
        if not defect.jobs and defect.job:
            defect.jobs = [defect.job]
        if defect.jobs and defect.job not in defect.jobs:
            defect.job = defect.jobs[0]
        return defect

    def to_dict(self) -> dict[str, object]:
        fields = asdict(self)
        extra = fields.pop("extra", {})
        merged: dict[str, object] = dict(extra)  # type: ignore[arg-type]
        merged.update(fields)
        return merged


class DefectStore:
    """Read and write the one defects file. One instance per command run.

    A write is a whole read-modify-write under the state file's own lock: the file
    is small, so the simplest correct thing is one exclusive section per add.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._defects: dict[str, Defect] = {}
        self._next = 0
        self._loaded = False
        self._thread_lock = threading.Lock()

    # -- io ----------------------------------------------------------------

    def load(self) -> DefectStore:
        if self._loaded:
            return self
        raw = self._read()
        if raw is not None:
            self._next = self._int_field(raw, "next")
            records = raw.get("defects", {})
            if not isinstance(records, dict):
                raise StateError(f"{self.path}: defects must be an object")
            for key, record in records.items():
                if not isinstance(record, dict):
                    raise StateError(f"{self.path}: defect {key} is not an object")
                copy = dict(record)
                # A record with no id is kept, not filled in: `broken()` names it, and
                # the handover refuses to be written until it is fixed. Inventing an
                # id here would hide the loss the record exists to prevent.
                copy.setdefault("id", "")
                self._defects[str(key)] = Defect.from_dict(copy)
        self._loaded = True
        return self

    def _read(self) -> dict[str, object] | None:
        if not self.path.exists():
            return None
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StateError(f"{self.path}: cannot read: {exc}") from exc
        if not text.strip():
            return None
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StateError(f"{self.path}: not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise StateError(f"{self.path}: expected a JSON object at the top level")
        version = raw.get("schema")
        if version != SCHEMA_VERSION:
            raise StateError(
                f"{self.path}: schema {version!r}, this build writes "
                f"{SCHEMA_VERSION}. Refusing to guess."
            )
        return raw

    @staticmethod
    def _int_field(raw: dict[str, object], name: str) -> int:
        value = raw.get(name, 0)
        if not isinstance(value, int) or isinstance(value, bool):
            raise StateError(f"{name} must be an integer")
        return value

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": SCHEMA_VERSION,
            "next": self._next,
            "defects": {key: defect.to_dict() for key, defect in self._defects.items()},
        }
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        token = os.urandom(4).hex()
        temp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{token}.tmp")
        try:
            temp.write_text(text, encoding="utf-8")
            os.replace(temp, self.path)
        except OSError as exc:
            raise StateError(f"{self.path}: cannot write: {exc}") from exc
        finally:
            if temp.exists():
                with contextlib.suppress(OSError):
                    temp.unlink()

    # -- reads -------------------------------------------------------------

    def all(self) -> list[Defect]:
        self.load()
        return sorted(self._defects.values(), key=lambda defect: defect.id)

    def get(self, defect_id: str) -> Defect:
        self.load()
        defect = self._defects.get(defect_id)
        if defect is None:
            raise StateError(
                f"no defect {defect_id!r} in {self.path}. List them with: clowder defects list"
            )
        return defect

    def find_by_line(self, line: str) -> Defect | None:
        self.load()
        key = normalise_line(line)
        for defect in self._defects.values():
            if normalise_line(defect.line) == key:
                return defect
        return None

    def repeated(self, open_jobs: Sequence[str]) -> list[Defect]:
        """The defects a second sighting obliges someone to act on."""
        self.load()
        return [d for d in self.all() if d.needs_action(open_jobs)]

    def recent(self, days: int, *, at: datetime | None = None) -> list[Defect]:
        """Defects first seen in the last `days` days, newest first.

        `clowder start` shows these so a person adding a defect can see the shape of
        what is already recorded - the only defence exact matching has against a
        reworded duplicate.
        """
        self.load()
        return recently_seen(self.all(), days, at=at)

    def broken(self) -> list[tuple[str, str]]:
        """Entries that cannot be a defect: no id, an id that is not its key, no line.

        The handover refuses to be written with one of these in it, so a hand-edited
        file cannot quietly lose a defect.
        """
        self.load()
        broken: list[tuple[str, str]] = []
        for key, defect in sorted(self._defects.items()):
            if not defect.id:
                broken.append((key, "it has no id"))
            elif defect.id != key:
                broken.append((key, f"its id is {defect.id!r}, not its key {key!r}"))
            elif not defect.line.strip():
                broken.append((key, "it has no line"))
        return broken

    def __len__(self) -> int:
        self.load()
        return len(self._defects)

    # -- writes ------------------------------------------------------------

    def sighting(
        self,
        line: str,
        *,
        job: str | None = None,
        state: str | None = None,
        reason: str | None = None,
        detail: str | None = None,
        first_seen: str | None = None,
        last_seen: str | None = None,
        count: int = 1,
    ) -> tuple[Defect, bool]:
        """Record one sighting. Returns the defect and whether it is new.

        The same normalised line is the same defect: the count goes up, `last_seen`
        moves, and what the caller knows now (a job, a state, a reason) is recorded
        without erasing what was known before. A defect recorded `fixed` that is
        seen again is open again, because it is not fixed.
        """
        text = " ".join(line.split())
        if not text:
            raise StateError("a defect needs one line of text")
        chosen = state or (HAS_JOB if job else OPEN)
        if chosen not in STATES:
            raise StateError(f"state must be one of {', '.join(STATES)}")
        began = moment_of(first_seen) if first_seen else None
        last = moment_of(last_seen) if last_seen else None
        if count < 1:
            raise StateError("count must be 1 or more")
        if last is not None and began is not None and last < began:
            raise StateError("last seen cannot be before first seen")
        with self._thread_lock, file_lock(self.path.with_name(self.path.name + ".lock")):
            self._loaded = False
            self.load()
            current = self.find_by_line(text)
            created = current is None
            moment = now_iso()
            if current is None:
                self._next += 1
                current = Defect(
                    id=f"d-{self._next:04d}",
                    line=text,
                    first_seen=began or moment,
                    last_seen=last or began or moment,
                    count=count,
                    job=job,
                    jobs=[job] if job else [],
                    state=chosen,
                    reason=reason,
                    detail=(detail or "").strip() or None,
                )
                self._defects[current.id] = current
            else:
                current.count += 1
                current.last_seen = moment
                if current.state == FIXED and chosen in (OPEN, HAS_JOB):
                    # It is not fixed if it happened again, whatever flag the caller
                    # passed. The job that fixed it is history, not a link: left in
                    # place, a later merge would see a closed job and the defect
                    # would never close again. An accepted workaround is a settled
                    # decision and is not reopened this way.
                    current.state = OPEN
                    current.jobs = []
                    current.job = None
                    current.fixed_at = None
                    current.fixed_pr = None
                if job and job not in current.linked_jobs:
                    current.jobs = [*current.linked_jobs, job]
                    current.job = current.jobs[0]
                if state:
                    current.state = chosen
                elif job and current.state == OPEN:
                    current.state = HAS_JOB
                if reason:
                    current.reason = reason
                if detail and (detail.strip() or None) != current.detail:
                    current.detail = detail.strip() or None
            if current.state == HAS_JOB and not current.linked_jobs:
                current.state = OPEN
            self._save()
        return current, created

    def link(self, defect_id: str, job_id: str) -> Defect:
        """Name a job that fixes this defect. More than one is allowed.

        This is the link `job merge` reads: when it lands, every defect naming that
        job is marked fixed, so nobody has to remember to type the same words again.
        Linking a settled defect is refused - a fixed defect that a new job fixes is
        a fresh sighting, and saying it is fixed while a job is open would be false.
        """
        with self._thread_lock, file_lock(self.path.with_name(self.path.name + ".lock")):
            self._loaded = False
            self.load()
            defect = self._defects.get(defect_id)
            if defect is None:
                raise StateError(
                    f"no defect {defect_id!r} in {self.path}. List them with: "
                    "clowder defects list"
                )
            if defect.settled:
                raise StateError(
                    f"{defect.id} is {defect.state}, so a job cannot be linked to it. A "
                    "defect that is fixed and bites again is recorded again: "
                    f'clowder defects add --line "{defect.line}"'
                )
            if job_id not in defect.linked_jobs:
                defect.jobs = [*defect.linked_jobs, job_id]
                defect.job = defect.jobs[0]
                if defect.state == OPEN:
                    defect.state = HAS_JOB
                self._save()
            return defect

    def unlink_job(self, job_id: str) -> list[Defect]:
        """Drop a job's links because the job closed without merging. Returns what changed.

        A closed branch fixed nothing. Leaving its link in place would make the defect
        look in flight for ever, and a later merge would see a link that no longer
        means anything. A settled defect is left alone: an accepted workaround is a
        decision, and closing a job does not decide anything about it.
        """
        with self._thread_lock, file_lock(self.path.with_name(self.path.name + ".lock")):
            self._loaded = False
            self.load()
            affected: list[Defect] = []
            changed = False
            for defect in self._defects.values():
                if defect.settled:
                    continue
                links = defect.linked_jobs
                if job_id not in links:
                    continue
                changed = True
                remaining = [job for job in links if job != job_id]
                defect.jobs = remaining
                if defect.state == HAS_JOB and not remaining:
                    defect.state = OPEN
                    defect.job = None
                    defect.reopened_at = now_iso()
                    defect.reopen_seq += 1
                else:
                    defect.job = remaining[0] if remaining else defect.job
                affected.append(defect)
            if changed:
                self._save()
            return sorted(affected, key=lambda defect: defect.id)

    def unlink(self, defect_id: str, job_id: str) -> Defect:
        """Drop one wrong link from one defect, and return it.

        The link is named by a command, so a wrong one has to be nameable too. A
        settled defect is refused: it records what happened, and relinking does not
        undo a merge.
        """
        with self._thread_lock, file_lock(self.path.with_name(self.path.name + ".lock")):
            self._loaded = False
            self.load()
            defect = self._defects.get(defect_id)
            if defect is None:
                raise StateError(
                    f"no defect {defect_id!r} in {self.path}. List them with: "
                    "clowder defects list"
                )
            if defect.settled:
                raise StateError(
                    f"{defect.id} is {defect.state}, so its links are history, not work "
                    "in flight. Nothing changed."
                )
            links = defect.linked_jobs
            if job_id not in links:
                raise StateError(
                    f"{defect.id} is not linked to {job_id}. Its links: "
                    f"{', '.join(links) or '(none)'}"
                )
            remaining = [job for job in links if job != job_id]
            defect.jobs = remaining
            if remaining:
                defect.job = remaining[0]
            else:
                defect.job = None
                if defect.state == HAS_JOB:
                    defect.state = OPEN
                    defect.reopened_at = now_iso()
                    defect.reopen_seq += 1
            self._save()
            return defect

    def mark_fixed_by_job(
        self, job_id: str, *, pr: str, at: str
    ) -> tuple[list[Defect], list[Defect]]:
        """Mark the defects this job fixes. Returns (closed, still pending).

        A defect naming two jobs is not fixed by the first merge: the other job still
        says the work is coming. Its link is dropped and it stays `has-job`, and it is
        returned as pending; the merge that lands the last link records the moment,
        the job and the PR, and marks it fixed. One write, so a merge cannot leave
        half a record.

        A settled defect is never rewritten. An accepted workaround is a decision the
        owner made, and a merge does not decide anything about it.
        """
        with self._thread_lock, file_lock(self.path.with_name(self.path.name + ".lock")):
            self._loaded = False
            self.load()
            closed: list[Defect] = []
            pending: list[Defect] = []
            changed = False
            for defect in self._defects.values():
                if defect.settled:
                    continue
                links = defect.linked_jobs
                if job_id not in links:
                    continue
                changed = True
                remaining = [job for job in links if job != job_id]
                defect.jobs = remaining
                if remaining:
                    # Another job still addresses it, so it is not fixed yet. The
                    # dropped link is still saved: the record must not say two jobs
                    # when one of them has merged.
                    defect.job = remaining[0]
                    defect.state = HAS_JOB
                    pending.append(defect)
                    continue
                defect.job = job_id
                defect.state = FIXED
                defect.fixed_at = at
                defect.fixed_pr = pr
                closed.append(defect)
            if changed:
                self._save()
            return (
                sorted(closed, key=lambda defect: defect.id),
                sorted(pending, key=lambda defect: defect.id),
            )


def recently_seen(
    defects: Sequence[Defect], days: int, *, at: datetime | None = None
) -> list[Defect]:
    """Those last seen in the last `days` days, newest first.

    Last seen, not first seen: the list exists so a person about to add a defect sees
    the ones in play now. A defect first stated a month ago and still biting is exactly
    the one a reworded duplicate would run from.
    """
    cutoff = (at or now()).timestamp() - days * 86400
    kept = [defect for defect in defects if (to_epoch(defect.last_seen) or 0) >= cutoff]
    return sorted(kept, key=lambda defect: defect.last_seen, reverse=True)


def read_defects(store: DefectStore) -> tuple[list[Defect], str | None]:
    """Every defect, or no defects and the reason the file could not be read.

    The readers that draw a page - `clowder start` and the board - use this. A
    malformed file must not take away the page that says what is happening in the
    crew; `clowder defects list` is the command that refuses, because that command
    exists to read the record.
    """
    try:
        return store.all(), None
    except (StateError, OSError) as exc:
        return [], str(exc)


def merge_warning(defect: Defect) -> str | None:
    """The line that tells the front door a repetition is now its job.

    A second sighting is not cleared by the count going up. It needs a job or an
    explicit accepted workaround, and this is where the tool says so at the moment
    the count moves - the board and `--repeated` keep saying it until it is done.
    """
    if not defect.seen_twice or defect.settled:
        return None
    if defect.linked_jobs:
        return None
    return (
        f"{defect.id} has now been seen {defect.count} times with no job. A second "
        "sighting opens a job or records an accepted workaround with the reason: "
        f'clowder defects add --line "{defect.line}" --job <job-id>'
    )


def describe(defect: Defect, *, open_jobs: Sequence[str] = ()) -> str:
    """One line for a list: id, line, count, state, every linked job."""
    links = defect.linked_jobs
    job = f"jobs {', '.join(links)}" if links else "no job"
    seen = f"seen {defect.count} time{'s' if defect.count != 1 else ''}"
    needs = " - needs a job or an accepted workaround" if defect.needs_action(open_jobs) else ""
    return (
        f"{defect.id} | {seen} | last {short_date(defect.last_seen)} | "
        f"{defect.state_now(open_jobs)} | {job} | {defect.line}{needs}"
    )
