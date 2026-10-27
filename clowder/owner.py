"""What waits on the owner: one list, two views.

Seen 2026-10-26: `clowder start` printed "decisions waiting on you: 0" while a change had
been waiting for a walkthrough for a day. The board showed it, so the two views disagreed,
and the owner stopped trusting either - "anything waiting on me as i can't trust the board
at the moment".

So the list is built once, here, and both views render it. A count can no longer say zero
while a line is on the page.

Three kinds count as waiting on him:

- **decision** - a question recorded on a task: `clowder owner --item`, or an open
  decision on a report. He answers it, and the front door records his words.
- **merge** - a change that is passed, published, and waiting only for his merge word.
- **walkthrough** - a change whose review is finished and which now waits for him to be
  walked through it.

A fourth kind is honest but not his: a change that is passed and not yet published (the
front door publishes it and CI runs), or one whose merge word is recorded and is waiting
for CI before the front door merges it. It is returned separately, so a view can show it
as "not yours" without counting it.

Nothing here runs git or reads a file: it takes the tasks and jobs it is given. The board
passes its re-review notes in; `start` passes none, because it does not read the refs. The
count is the same either way - only a note differs.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .state import Job, Task
from .timeutil import elapsed_seconds, human_age

# A change waiting for a walkthrough past this long is flagged, so a change cannot sit
# unnoticed for days. It is the second quiet bound in the crew's pages: a step is quiet
# after an hour, a held change after a day.
HELD_QUIET_AFTER_SECONDS = 86400.0

DECISION = "decision"
MERGE = "merge"
WALKTHROUGH = "walkthrough"

# The word that answers each kind, in his terms. The front door reads this list, so the
# action has to stand alone without the crew's vocabulary.
ACTIONS = {
    DECISION: "type the number",
    MERGE: "reply merge",
    WALKTHROUGH: "reply pass, or what to change",
}


def answered_by(item: Waiting, number: int) -> str:
    """The exact words that answer one line.

    A recorded question is answered by its number on the page ("type 1"); the other
    kinds by their word. Views number their lines, so they call this; nothing else
    should print `item.action` for a decision.
    """
    if item.kind == DECISION:
        return f"type {number}"
    return item.action


@dataclass(frozen=True)
class Waiting:
    """One thing in his queue. `heading` is the thing, in his words."""

    kind: str
    heading: str
    note: str
    action: str
    since: str
    id: str
    detail: str | None = None

    @property
    def waited(self) -> str:
        return human_age(elapsed_seconds(self.since, None))


@dataclass(frozen=True)
class NotYours:
    """A change he might ask about, which is the front door's to move, not his."""

    heading: str
    note: str
    since: str
    id: str

    @property
    def waited(self) -> str:
        return human_age(elapsed_seconds(self.since, None))


def names_the_merge(text: str) -> bool:
    """Does an owner item name the merge word, so it is the merge decision itself?

    The board derives a merge line for a job that waits on the word, and an owner item can
    be the same decision in the front door's words. That would be one decision shown
    twice, and the item is the one he answers. A word check is the narrowest rule that can
    tell that apart from an item about something else on the same job: a miss shows a
    duplicate (noise), a false hit could hide a decision, so only the explicit word counts.
    """
    return "merge" in " ".join(text.split()).lower()


def _open_steps(tasks: Iterable[Task]) -> dict[str, int]:
    """How many steps are open on each job."""
    counts: dict[str, int] = {}
    for task in tasks:
        if task.is_open and task.job:
            counts[task.job] = counts.get(task.job, 0) + 1
    return counts


def waiting_on_owner(
    tasks: Iterable[Task],
    jobs: Iterable[Job],
    *,
    rereview: Mapping[str, str] | None = None,
    review_unconfirmed: Iterable[str] = (),
) -> tuple[list[Waiting], list[NotYours]]:
    """What waits on him, and what does not, from the state records alone.

    Returns `(his, not_his)`: the first list is the three kinds, oldest first; the second
    is work the front door is moving. Both are ordered oldest first, so the thing that has
    waited longest is answered first.
    """
    task_list = list(tasks)
    job_list = list(jobs)
    open_on_job = _open_steps(task_list)
    unconfirmed = set(review_unconfirmed)

    his: list[Waiting] = []
    named_by_item: set[str] = set()
    for task in task_list:
        # A recorded question waits on him until it is cleared or answered, whatever
        # happened to the step that carried it. Seen 2026-10-27: three owner items sat
        # on closed steps and vanished from both views the moment the step closed.
        if task.owner_item:
            if task.job and names_the_merge(task.owner_item):
                named_by_item.add(task.job)
            his.append(
                Waiting(
                    kind=DECISION,
                    heading=" ".join(task.owner_item.split()),
                    note="your move",
                    action=ACTIONS[DECISION],
                    since=task.owner_item_at or task.created_at,
                    id=task.id,
                    detail=(task.owner_detail or "").strip() or None,
                )
            )
        if task.open_decision and task.decision_is_open:
            his.append(
                Waiting(
                    kind=DECISION,
                    heading=" ".join(task.open_decision.split()),
                    note=(
                        f"decision, asked by {task.agent} about "
                        f"{' '.join(task.question.split())}"
                    ),
                    action=ACTIONS[DECISION],
                    since=task.created_at,
                    id=task.id,
                )
            )

    not_his: list[NotYours] = []
    notes = rereview or {}
    for job in job_list:
        if not job.is_open:
            continue
        busy = open_on_job.get(job.id, 0) > 0
        if job.has_pass and not job.has_merge_word:
            if not job.published_at:
                not_his.append(
                    NotYours(
                        heading=job.name_in_words,
                        note="passed; the front door publishes it and CI runs there",
                        since=job.pass_at or job.created_at,
                        id=job.id,
                    )
                )
            elif not busy and job.id not in named_by_item:
                reviewed = f" reviewed by {job.reviewer}" if job.reviewer else ""
                his.append(
                    Waiting(
                        kind=MERGE,
                        heading=job.name_in_words,
                        note=f"passed{reviewed}; the front door merges it once you give "
                        "your word and the checks are green",
                        action=ACTIONS[MERGE],
                        since=job.published_at or job.created_at,
                        id=job.id,
                    )
                )
            continue
        if job.has_merge_word:
            if not job.merged_at:
                not_his.append(
                    NotYours(
                        heading=job.name_in_words,
                        note="your word is recorded; the front door merges it when CI is green",
                        since=job.merge_word_at or job.created_at,
                        id=job.id,
                    )
                )
            continue
        if job.reviewer and job.review_commit and not busy:
            if job.id in notes:
                note = (
                    f"checked at {(job.review_commit or '')[:7]} but moved to "
                    f"{notes[job.id][:7]}; the reviewer must check it again"
                )
            elif job.id in unconfirmed:
                note = (
                    f"checked at {(job.review_commit or '')[:7]}, and its current commit "
                    "could not be read"
                )
            else:
                note = (
                    f"checked by {job.reviewer} at {(job.review_commit or '')[:7]}; "
                    "waiting for your walkthrough"
                )
            seconds = elapsed_seconds(job.handed_over_at, None)
            if seconds >= HELD_QUIET_AFTER_SECONDS:
                note += " - gone quiet"
            his.append(
                Waiting(
                    kind=WALKTHROUGH,
                    heading=job.name_in_words,
                    note=note,
                    action=ACTIONS[WALKTHROUGH],
                    since=job.handed_over_at or job.created_at,
                    id=job.id,
                )
            )

    his.sort(key=lambda item: (item.since, item.kind, item.id))
    not_his.sort(key=lambda item: (item.since, item.id))
    return his, not_his
