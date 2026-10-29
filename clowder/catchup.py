"""What the owner missed, in his words. One command, one list.

His complaint, 2026-10-26: "a lot of our turns are just me asking you to repeat yourself with
context ... The board was meant to be the way that I could pick up information that I'd missed
in the chat. But the board is just always stale, and I have to ask you to refresh the board."

Two mechanical faults sat behind that. The board is written by a command, so between commands
it is a snapshot of a record that has moved on. And nothing recorded what he had already read,
so "what did I miss" could not be computed at all - every view answered "what is true now",
which is a different question.

So this module derives the answer from the record each time it is asked, and never caches it.
An item is one line he can act on, with the moment it became true. His last read is `catchup_at`
in the state file, and each key's last moment is in `seen`; an item is new when its key carries
a moment he has not been shown. A view can therefore never be stale: there is no copy to be
stale.

Five kinds, in the order he needs them: what landed, what is at risk, what waits on him (with
the word that answers it), what runs now, and what is queued next.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from . import owner as owner_mod
from .defects import Defect
from .state import Job, Queued, StateStore, Task
from .timeutil import elapsed_seconds, human_age, now, to_epoch

LANDED = "landed"
RISK = "risk"
WAITING = "waiting"
RUNNING = "running"
QUEUED = "queued"

# The order he reads them in, and the heading over each group, in his words.
GROUPS = (
    (LANDED, "What landed"),
    (RISK, "What broke or is at risk"),
    (WAITING, "Waiting on you"),
    (RUNNING, "Running now"),
    (QUEUED, "Queued next"),
)

HEADINGS = dict(GROUPS)

# A first read has no mark to read from, so it is bounded: history older than this is
# counted, not listed. The owner's own state listed 244 past landings on the first run,
# which is a wall, not an answer. Waiting, running and queued are always shown whole -
# an old thing he must act on cannot be aged out of the picture.
FIRST_READ_DAYS = 7
FIRST_READ_KINDS = (LANDED, RISK)


@dataclass(frozen=True)
class Item:
    """One thing he missed, or one thing standing.

    `key` is stable while the thing is the same, so a second read does not show it
    again; `at` is the moment it became true, and a later moment under the same key is
    a new item. `answer` is the exact word that moves it, when it is his to move.
    """

    key: str
    kind: str
    at: str
    text: str
    answer: str | None = None
    # What it is doing now: merged, open, dispatched, queued. A line read in one state
    # and found in another is new again, even when its moment has not moved.
    state: str = ""

    @property
    def token(self) -> str:
        """What he saw: the moment, and the state it was in."""
        return f"{self.at}|{self.state}"

    @property
    def line(self) -> str:
        """One line for a person: the thing, then the word that answers it."""
        return f"{self.text} - {self.answer}" if self.answer else self.text

    @property
    def moved_at(self) -> str:
        """The moment as a person reads it. The record keeps the full timestamp."""
        return self.at[:10] if len(self.at) >= 10 else self.at


def _moment(value: str | None) -> str:
    return value or ""


def landed_name(job: Job) -> str:
    """The name a landed change carries to the owner: effect, title, then the label."""
    for value in (job.effect, job.title):
        text = (value or "").strip()
        if text:
            return text
    return job.label


def landed(jobs: Iterable[Job]) -> list[Item]:
    """What merged, newest first, with what it changes for a user."""
    items = [
        Item(
            key=f"landed:{job.id}",
            kind=LANDED,
            at=_moment(job.merged_at),
            # What it changes, in his words: the effect line, then the title he would
            # recognise, then - only when neither was recorded - the label, which is the
            # old behaviour and not a new guess.
            text=f"Now live: {landed_name(job)}",
            state="merged",
        )
        for job in jobs
        if job.merged_at
    ]
    return sorted(items, key=lambda item: item.at, reverse=True)


def at_risk(defects: Sequence[Defect], open_jobs: Sequence[str]) -> list[Item]:
    """Defects biting again with nothing in flight, newest first."""
    items: list[Item] = []
    for defect in defects:
        if not defect.needs_action(open_jobs):
            continue
        # A defect put back to open by a job that closed without merging is news
        # again, even though nobody saw it happen: the moment it was reopened is
        # later than the read that dropped it.
        moment = max(defect.last_seen, defect.reopened_at or "")
        items.append(
            Item(
                key=f"risk:{defect.id}:{moment}",
                kind=RISK,
                at=moment,
                text=(
                    f"Still happening: {defect.line} "
                    f"(seen {defect.count} time{'s' if defect.count != 1 else ''})"
                ),
                # The reopen count is part of the state: a defect reopened inside the
                # same second as the read is still news.
                state=f"{defect.state_now(open_jobs)}#{defect.reopen_seq}",
            )
        )
    return sorted(items, key=lambda item: item.at, reverse=True)


def waiting(tasks: Iterable[Task], jobs: Iterable[Job]) -> list[Item]:
    """What waits on him, oldest first, each with the word that answers it."""
    his, _ = owner_mod.waiting_on_owner(tasks, jobs)
    items: list[Item] = []
    for number, entry in enumerate(his, start=1):
        items.append(
            Item(
                key=f"waiting:{entry.kind}:{entry.id}",
                kind=WAITING,
                at=entry.since,
                text=entry.heading,
                answer=owner_mod.answered_by(entry, number),
                state=entry.kind,
            )
        )
    return items


def running(tasks: Iterable[Task]) -> list[Item]:
    """What is in flight, oldest first. No worker narration: the question he was sent."""
    return [
        Item(
            key=f"running:{task.id}",
            kind=RUNNING,
            at=task.dispatched_at or task.created_at,
            text=(
                f"{plain_question(task.question)} - {task.agent}, "
                f"{human_age(elapsed_seconds(task.dispatched_at or task.created_at, None))}"
            ),
            state=task.status,
        )
        for task in sorted(
            (task for task in tasks if task.is_open),
            key=lambda task: task.dispatched_at or task.created_at,
        )
    ]


def queued(items: Iterable[Queued]) -> list[Item]:
    """What is decided and not sent, oldest first, with why it waits."""
    return [
        Item(
            key=f"queued:{item.id}",
            kind=QUEUED,
            at=item.created_at,
            text=f"{plain_question(item.question or item.brief)} - not sent yet: {item.why}",
            state="queued",
        )
        for item in sorted(items, key=lambda item: item.created_at)
    ]


def build(store: StateStore, defects: Sequence[Defect]) -> list[Item]:
    """Every standing item, derived from the record now. Nothing is cached."""
    tasks = store.all()
    jobs = store.all_jobs()
    open_jobs = [job.id for job in store.open_jobs()]
    return [
        *landed(jobs),
        *at_risk(defects, open_jobs),
        *waiting(tasks, jobs),
        *running(tasks),
        *queued(store.all_queued()),
    ]


HANDLE = re.compile(r"[jtqd]-\d{3,}", re.IGNORECASE)
SHAPE = re.compile(r"^(ship|scout)\s*:\s*", re.IGNORECASE)
EMPTY_BRACKETS = re.compile(r"\(\s*\)|\[\s*\]")


def plain_question(text: str) -> str:
    """A worker's question in his nouns: no shape word, no handle, no stray brackets.

    `question` is the brief's first line, so it can carry the crew's dialect - "ship
    (t-1016): ..." - and a handle in a line he reads is a lookup he did not ask for.
    The shape word is stripped only when it is written as a label, "ship:" or "scout:",
    so an English sentence such as "scout the area" is left alone.
    """
    cleaned = HANDLE.sub("", text or "")
    # Empty brackets go before the shape word: "ship (t-1016):" becomes "ship :" first,
    # and only then is the label stripped.
    cleaned = EMPTY_BRACKETS.sub("", cleaned)
    cleaned = SHAPE.sub("", cleaned.strip())
    cleaned = " ".join(cleaned.replace("()", "").replace("[]", "").split())
    return cleaned.strip(" :-,") or "a step in flight"


def bounded_first_read(
    items: Sequence[Item], *, days: int = FIRST_READ_DAYS, at: datetime | None = None
) -> tuple[list[Item], int]:
    """What a first read shows and how many it left out. Returns (kept, hidden).

    History - what landed and what is at risk - is kept only inside the window, because
    with no read mark every past landing would count as new. Everything he must act on
    now, or that is running, is kept whole: an old decision is not made stale by being
    old.
    """
    cutoff = (at or now()).timestamp() - days * 86400
    kept: list[Item] = []
    hidden = 0
    for item in items:
        if item.kind in FIRST_READ_KINDS and (to_epoch(item.at) or 0.0) < cutoff:
            hidden += 1
            continue
        kept.append(item)
    return kept, hidden


def first_read_note(shown: int, hidden: int, *, days: int = FIRST_READ_DAYS) -> str:
    """The sentence over a first read: that it is one, the window, and what it left out."""
    note = f"First read, so this is the last {days} days: {shown} thing(s)."
    if hidden:
        note += f" {hidden} older thing(s) are not shown; --all shows everything."
    return note


def unseen(items: Sequence[Item], seen: dict[str, str]) -> list[Item]:
    """The items he has not been shown, or whose moment or state has moved since."""
    return [item for item in items if seen.get(item.key) != item.token]


def moments(items: Sequence[Item]) -> dict[str, str]:
    """The key-and-token record a read stores for these items."""
    return {item.key: item.token for item in items}


def groups(items: Sequence[Item]) -> list[tuple[str, list[Item]]]:
    """The items under their headings, in the order he reads them, empty ones dropped."""
    by_kind: dict[str, list[Item]] = {}
    for item in items:
        by_kind.setdefault(item.kind, []).append(item)
    return [(HEADINGS[kind], by_kind[kind]) for kind, _ in GROUPS if by_kind.get(kind)]


def lines(items: Sequence[Item]) -> list[str]:
    """The whole picture as plain lines: a heading, then one line per item."""
    out: list[str] = []
    for heading, entries in groups(items):
        out.append(f"{heading}:")
        out.extend(f"  - {entry.line}" for entry in entries)
    return out


def read_at(value: str | None, *, now: datetime | None = None) -> str:
    """A moment as the sentence about it: 'today at 14:20' or the date."""
    if not value:
        return "never"
    return f"{value[:10]} {value[11:16]}" if len(value) >= 16 else value[:10]


def summary_note(
    new_count: int, standing: int, last_read: str | None, *, now: datetime | None = None
) -> str:
    """The one sentence over a catch-up: what is new, or that nothing is."""
    if new_count:
        return f"{new_count} new since you last looked ({read_at(last_read)})."
    if standing:
        return (
            f"Nothing new since you last looked ({read_at(last_read)}). "
            f"{standing} thing(s) are standing; read them with --all."
        )
    return "Nothing has happened yet, and nothing is waiting on you."
