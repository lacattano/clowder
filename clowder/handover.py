"""The handover document, generated from the record.

A handover is written by hand at the end of a session and read at the start of the
next one. Written by hand, it drifted: 13 working days had none, and a quirk could
be recorded in three handovers in a row and still bite.

So the document is now composed by a command from what the tool already holds:

- what waits on the owner, in his words, from the tasks and jobs;
- the steps in flight, and the open jobs with their state;
- the queue count;
- what merged since the last handover;
- the defects whose `last_seen` falls inside the window, printed as one line and an
  id, never restated.

Exactly one section is hand-written: `what a command cannot know`, read from
`handover-notes.md`. It is capped at ten lines, so the document cannot grow back into
the old one. Everything else in it is derived, so nothing in it can be a second copy
of a fact the tool holds.

The file is named `HANDOVER-<date>.md` beside the state file, and it records the
moment it was written. `clowder start` reads the newest one and says how old it is,
and says plainly when work has happened since it was written - a handover nobody
updated is stale, and stale must be visible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .defects import Defect, DefectStore, short_date
from .errors import StateError
from .state import StateStore
from .timeutil import human_age, now, parse_iso, to_epoch, to_iso

HANDOVER_GLOB = "HANDOVER-*.md"
NOTES_NAME = "handover-notes.md"
# The one hand-written section is capped here, so it cannot become the old document.
NOTES_MAX_LINES = 10
# Marked in the generated file, so a reader (and this module) can tell when it was
# written without trusting the file's mtime, which a copy would move.
GENERATED_MARK = "generated: "


@dataclass(frozen=True)
class Handover:
    """The newest handover, and whether work has happened since it was written."""

    path: Path | None
    written_at: datetime | None
    age_days: int | None
    stale: bool
    # Why it is stale, in words a reader can act on. None when it is current.
    why: str | None = None


def handover_dir(state_path: str | Path) -> Path:
    """Where handovers live: beside the state file, with the rest of the record."""
    return Path(state_path).parent


def _date_in_name(path: Path) -> str:
    """The `YYYY-MM-DD` a handover's name carries, or "" when it carries none."""
    match = re.search(r"(\d{4}-\d{2}-\d{2})", path.name)
    return match.group(1) if match else ""


def handover_files(directory: str | Path) -> list[Path]:
    """Every handover document, oldest first. Read-only history: never written.

    Ordered by the date in the name, then generated-after-hand-written for the same
    day, then by the moment it says it was written. So "the newest document" is the
    one a fresh session should read, and a day that has both kinds resolves to the
    generated one. Sorting on the name alone would pick the hand-written one.
    """
    folder = Path(directory)
    if not folder.is_dir():
        return []
    files = list(folder.glob(HANDOVER_GLOB))
    return sorted(
        files,
        key=lambda path: (_date_in_name(path), is_generated(path), written_at(path), path.name),
    )


def is_generated(path: Path) -> bool:
    """Did this tool write the file? A generated handover says so in its own body.

    A file without the mark is history: a hand-written handover from before the
    document was generated. Those are never written to, so the answer decides
    whether a path may be replaced or a new name must be used.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for _, line in zip(range(40), handle, strict=False):
                if line.strip().startswith(GENERATED_MARK):
                    return True
    except OSError:
        return False
    return False


def written_at(path: Path) -> datetime:
    """When a handover was written.

    A generated one says so in its own body. An older hand-written one has no such
    line, so its file time is the only evidence there is - and for history that is
    enough, because only the newest one is compared with recent work.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for _, line in zip(range(40), handle, strict=False):
                text = line.strip()
                if text.startswith(GENERATED_MARK):
                    try:
                        return parse_iso(text[len(GENERATED_MARK) :].strip())
                    except ValueError:
                        break
    except OSError:
        pass
    try:
        moment = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return datetime.min.replace(tzinfo=UTC)
    return moment


def age_days(moment: datetime | None, *, at: datetime | None = None) -> int | None:
    """Calendar days from a moment to today, both in UTC. Never negative."""
    if moment is None:
        return None
    today = (at or now()).astimezone(UTC).date()
    return max(0, (today - moment.astimezone(UTC).date()).days)


def last_activity(store: StateStore, defects: DefectStore | None = None) -> datetime | None:
    """The newest moment any record was touched.

    A session leaves a trace in the record: a step dispatched or reported, a job
    opened, handed over, passed or merged, a queue item added, a defect sighted.
    The newest of those is the last moment the crew did something, which is what a
    handover's age is measured against.
    """
    moments: list[float] = []
    for task in store.all():
        for value in (
            task.created_at,
            task.dispatched_at,
            task.reported_at,
            task.abandoned_at,
            task.closed_at,
            task.owner_item_at,
            task.decision_answered_at,
        ):
            moment = to_epoch(value)
            if moment is not None:
                moments.append(moment)
    for job in store.all_jobs():
        for value in (
            job.created_at,
            job.handed_over_at,
            job.pass_at,
            job.merge_word_at,
            job.published_at,
            job.parked_at,
            job.merged_at,
            job.closed_at,
        ):
            moment = to_epoch(value)
            if moment is not None:
                moments.append(moment)
    for item in store.all_queued():
        moment = to_epoch(item.created_at)
        if moment is not None:
            moments.append(moment)
    if defects is not None:
        for defect in defects.all():
            for value in (defect.first_seen, defect.last_seen):
                moment = to_epoch(value)
                if moment is not None:
                    moments.append(moment)
    if not moments:
        return None
    return datetime.fromtimestamp(max(moments), UTC)


def latest(
    store: StateStore,
    defects: DefectStore | None = None,
    *,
    at: datetime | None = None,
) -> Handover:
    """The newest handover, with its age and whether work has happened since."""
    folder = handover_dir(store.path)
    files = handover_files(folder)
    if not files:
        return Handover(
            path=None,
            written_at=None,
            age_days=None,
            stale=True,
            why="no handover exists",
        )
    path = files[-1]
    moment = written_at(path)
    activity = last_activity(store, defects)
    stale = bool(activity and activity > moment)
    why = None
    if stale and activity is not None:
        newer = path.name
        why = (
            f"work was recorded {human_age(activity.timestamp() - moment.timestamp())} "
            f"after {newer} was written, and no handover has been written since"
        )
    return Handover(
        path=path,
        written_at=moment,
        age_days=age_days(moment, at=at),
        stale=stale,
        why=why,
    )


def window_start(store: StateStore, at: datetime) -> Handover:
    """The handover a new one measures its window from.

    The newest document older than the one about to be written. That matters for a
    rewrite: a session that runs `handover write` twice in one day replaces the file,
    and the window must still start where the day started, or the second write would
    drop everything the first one recorded.
    """
    today = at.astimezone(UTC).date().isoformat()
    prefix = f"HANDOVER-{today}"
    files = [
        path
        for path in handover_files(handover_dir(store.path))
        if not path.name.startswith(prefix)
    ]
    if not files:
        return Handover(path=None, written_at=None, age_days=None, stale=False)
    path = files[-1]
    moment = written_at(path)
    return Handover(
        path=path,
        written_at=moment,
        age_days=age_days(moment, at=at),
        stale=False,
    )


def notes_text(state_path: str | Path) -> tuple[str, int]:
    """The one hand-written section, capped. Returns the text and how many lines fell.

    The cap is a hard cut, not a warning: a section that can grow without limit is
    the document this job replaced. What is cut is counted in the file.
    """
    path = handover_dir(state_path) / NOTES_NAME
    if not path.is_file():
        return "", 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise StateError(f"{path}: cannot read: {exc}") from exc
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    kept = lines[:NOTES_MAX_LINES]
    return "\n".join(kept), max(0, len(lines) - NOTES_MAX_LINES)


def owner_lines(store: StateStore) -> list[str]:
    """Everything that waits on the owner, in his words, one line each."""
    lines: list[str] = []
    tasks = store.all()
    for task in tasks:
        if task.owner_item and not task.is_closed:
            lines.append(f"{task.owner_item}  [{task.id}]")
    for task in tasks:
        if task.decision_is_open and not task.is_closed:
            lines.append(f"{task.open_decision}  [{task.id}, asked by {task.agent}]")
    for job in store.open_jobs():
        in_flight = any(task.job == job.id and task.is_open for task in tasks)
        if job.has_pass and not job.has_merge_word and not in_flight:
            lines.append(f"your merge word for {job.name_in_words}  [{job.id}]")
    return lines


def _in_flight(store: StateStore) -> list[str]:
    """Steps that are still open, and open jobs with no open step on them."""
    lines: list[str] = []
    tasks = store.all()
    moving = {task.job for task in tasks if task.is_open and task.job}
    for job in store.open_jobs():
        if job.id in moving:
            continue  # the step line below says what is actually moving
        if job.has_pass and not job.has_merge_word:
            continue  # only the owner's word is missing; it is in his section
        lines.append(
            f"the change {job.name_in_words} - {job.state_in_words} "
            f"[{job.id}] in {job.agent}'s space"
        )
    for task in store.select(open_only=True):
        who = f" in {task.worktree}" if task.worktree else ""
        lines.append(f"{task.question}  [{task.id}, {task.agent}{who}]")
    return lines


def _landed(store: StateStore, since: float | None) -> list[str]:
    """Jobs merged inside the window, newest first."""
    lines: list[str] = []
    for job in store.all_jobs():
        moment = to_epoch(job.merged_at)
        if moment is None or not job.has_merge_word:
            continue
        if since is not None and moment < since:
            continue
        lines.append(f"{job.name_in_words} [{job.id}] merged {short_date(job.merged_at)}")
    return list(reversed(lines))


def _window_defects(defects: DefectStore, since: float | None) -> list[Defect]:
    kept: list[Defect] = []
    for defect in defects.all():
        moment = to_epoch(defect.last_seen)
        if since is not None and (moment is None or moment < since):
            continue
        kept.append(defect)
    return kept


def _bullets(lines: list[str], empty: str) -> list[str]:
    if not lines:
        return [f"(nothing: {empty})"]
    return [f"- {line}" for line in lines]


def compose(
    store: StateStore,
    defects: DefectStore,
    *,
    previous: Handover,
    at: datetime | None = None,
) -> str:
    """The whole document, derived from the record plus the one notes section."""
    moment = at or now()
    since = previous.written_at.timestamp() if previous.written_at else None
    window = (
        f"{to_iso(previous.written_at)} to {to_iso(moment)}"
        if previous.written_at
        else f"the whole record to {to_iso(moment)}"
    )
    defect_lines = []
    for defect in _window_defects(defects, since):
        job = f", job {defect.job}" if defect.job else ", no job"
        defect_lines.append(
            f"{defect.id} {defect.line} - seen {defect.count} time"
            f"{'s' if defect.count != 1 else ''}{job}, last {short_date(defect.last_seen)}"
        )

    notes, dropped = notes_text(store.path)
    note_lines = notes.splitlines() if notes else []
    if dropped:
        note_lines.append(
            f"(capped at {NOTES_MAX_LINES} lines; {dropped} line(s) were not written)"
        )
    if not note_lines:
        note_lines = ["(nothing: no hand-written note for the next session)"]

    parts: list[str] = [
        f"# Handover - {moment.astimezone(UTC).date().isoformat()}",
        "",
        f"{GENERATED_MARK}{to_iso(moment)}",
        f"window: {window}",
        "",
        "## What waits on the owner",
        "",
        *_bullets(owner_lines(store), "nothing waits on him"),
        "",
        "## In flight",
        "",
        *_bullets(_in_flight(store), "nothing is in flight"),
        "",
        "## Landed in this window",
        "",
        *_bullets(_landed(store, since), "nothing merged since the last handover"),
        "",
        "## The queue",
        "",
        f"{len(store.all_queued())} item(s) waiting to be sent. "
        "Read them with `clowder queue list`.",
        "",
        "## Defects seen in this window",
        "",
        *_bullets(defect_lines, "no defect was recorded in this window"),
        "",
        "## What a command cannot know",
        "",
        *note_lines,
        "",
        "---",
        "",
        "Generated by `clowder handover write`: every section above is derived from the",
        "record except the last. A defect is printed as its one line and its id; its",
        "detail lives in `clowder defects show <id>` and nowhere else.",
        "",
    ]
    return "\n".join(parts)


def target_path(directory: str | Path, at: datetime) -> Path:
    """Where today's handover goes, without ever replacing a hand-written one.

    A generated handover for today may be replaced by a second write. A file for
    today with no generated mark is history - one of the hand-written handovers this
    job replaced - and the tool does not write over it: it writes
    `HANDOVER-<date>.generated.md` beside it instead. The name still carries the
    date, it sorts after the hand-written one, and the old file is untouched.
    """
    folder = Path(directory)
    date = at.astimezone(UTC).date().isoformat()
    standard = folder / f"HANDOVER-{date}.md"
    if not standard.exists() or is_generated(standard):
        return standard
    alternative = folder / f"HANDOVER-{date}.generated.md"
    if alternative.exists() and not is_generated(alternative):
        raise StateError(
            f"{alternative} is not a generated handover, so it cannot be replaced. "
            "Move it aside with the owner's word, then write today's handover again."
        )
    return alternative


def write(
    store: StateStore,
    defects: DefectStore,
    *,
    at: datetime | None = None,
) -> tuple[Path, int]:
    """Write today's handover, replacing one this tool already wrote today.

    Returns the path and how many notes lines were cut. Writing twice for the same
    session replaces the file instead of adding a second one, so a retried write
    cannot duplicate a section or a defect. A hand-written handover is never
    replaced; see `target_path`.
    """
    broken = defects.broken()
    if broken:
        listed = "; ".join(f"{key}: {why}" for key, why in broken)
        raise StateError(
            f"{defects.path}: a defect entry has no id, so it cannot be named in a "
            f"handover. Fix it before writing one: {listed}"
        )
    moment = at or now()
    previous = window_start(store, moment)
    text = compose(store, defects, previous=previous, at=moment)
    path = target_path(handover_dir(store.path), moment)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _, dropped = notes_text(store.path)
    return path, dropped
