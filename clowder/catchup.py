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

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from . import owner as owner_mod
from .defects import Defect
from .state import Job, Queued, StateStore, Task
from .timeutil import elapsed_seconds, human_age

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


def landed(jobs: Iterable[Job]) -> list[Item]:
    """What merged, newest first, with what it changes for a user."""
    items = [
        Item(
            key=f"landed:{job.id}",
            kind=LANDED,
            at=_moment(job.merged_at),
            text=f"Now live: {job.effect or job.name_in_words}",
        )
        for job in jobs
        if job.merged_at
    ]
    return sorted(items, key=lambda item: item.at, reverse=True)


def at_risk(defects: Sequence[Defect], open_jobs: Sequence[str]) -> list[Item]:
    """Defects biting again with nothing in flight, newest first."""
    items = [
        Item(
            key=f"risk:{defect.id}:{defect.last_seen}",
            kind=RISK,
            at=defect.last_seen,
            text=(
                f"Still happening: {defect.line} "
                f"(seen {defect.count} time{'s' if defect.count != 1 else ''})"
            ),
        )
        for defect in defects
        if defect.needs_action(open_jobs)
    ]
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
                f"{task.question} - {task.agent}, "
                f"{human_age(elapsed_seconds(task.dispatched_at or task.created_at, None))}"
            ),
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
            text=f"{item.question or item.brief} - not sent yet: {item.why}",
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


def unseen(items: Sequence[Item], seen: dict[str, str]) -> list[Item]:
    """The items he has not been shown, or whose moment has moved since he was."""
    return [item for item in items if seen.get(item.key) != item.at]


def moments(items: Sequence[Item]) -> dict[str, str]:
    """The key-and-moment record `mark_seen` stores for these items."""
    return {item.key: item.at for item in items}


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
