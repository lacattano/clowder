"""The page as it is drawn from the record, with no command behind it.

`board.py` renders what it is handed and stays a pure function, so it can be tested
without a machine. This module is the other half: it builds that data from the state
file and the files beside it, and it is what the state write path calls so the page is
never waiting for somebody to run a command.

Three things it must never do, because it runs on every save:

- it never asks the multiplexer anything, so a state change pays for a page and not for
  a live roster;
- it never reads the box, except the one line `clowder board` adds itself;
- it never raises into the caller: a page that cannot be written is a warning, and a
  failed board must never fail the command that changed the record.

The git look for "is this commit on a branch" is kept, but memoised in a file beside
the state, so it costs a fingerprint per folder and not a process per step.
"""

from __future__ import annotations

from pathlib import Path

from . import audit, gitcmd
from . import catchup as catchup_mod
from . import handover as handover_doc
from .board import BoardAgent, BoardData, now_stamp, stamp_of
from .defects import DefectStore, read_defects
from .state import Job, StateStore, Task
from .timeutil import now_iso

# The file the page is written to, beside the state it is drawn from.
PAGE_NAME = "board.html"

# Say plainly that this roster came from the record, not from the panes.
STATE_ONLY_NOTE = (
    "The agent roster was not read live; this is what the state file records. "
    "Run `clowder board --live` for the live list."
)


def page_path(state_path: str | Path) -> Path:
    """Where the page lives: beside the state file it is drawn from."""
    return Path(state_path).with_name(PAGE_NAME)


def defects_path(state_path: str | Path) -> Path:
    """The one defects record, by the same rule the config uses."""
    return Path(state_path).with_name("defects.json")


def job_review_state(job: Job) -> tuple[str, str | None]:
    """Where a held candidate's work is: held, moved, or unknown.

    The branch tip is the job's current work; the held ref is what remains when the
    branch is gone. "held" means a readable ref still points at the reviewed commit,
    "moved" means a readable ref points elsewhere, and "unknown" means no ref could be
    read - which must never be reported as held.
    """
    resolved = False
    for ref in (job.branch, job.held_ref):
        if not ref:
            continue
        tip = gitcmd.commit_of(job.repo_path, ref)
        if tip is None:
            continue
        resolved = True
        if tip != job.review_commit:
            return "moved", tip
    if resolved:
        return "held", job.review_commit
    return "unknown", None


def job_moved_past(task: Task, job: Job | None) -> bool:
    """Has the task's job branch or held ref moved past the step's commit?

    A job's branch or held ref holds the job's current work. A step commit that is no
    longer reachable from it is an earlier, rewritten version: the job moved past it,
    so it is superseded and not work at risk. A ref that is behind the commit (a reset)
    has not moved past it, so that stays at risk.
    """
    if job is None or not task.commit:
        return False
    for ref in (job.branch, job.held_ref):
        if not ref:
            continue
        tip = gitcmd.commit_of(job.repo_path, ref)
        if not tip:
            continue
        if gitcmd.is_ancestor(job.repo_path, tip, task.commit):
            continue  # the ref is behind the commit, so it has not moved past it
        return True
    return False


def work_has_landed(task: Task, job: Job | None) -> bool:
    """Is this step's work already in the job's history, so its commit is moot?

    A note about a commit on no branch exists to catch work that could be lost before
    it lands. Once the step has reported and its job is merged, the change is in the
    history the owner merged. A later rebase can leave the recorded commit on no
    branch, but that is not a reason to keep chasing it. The claim keys off the step's
    status and the merge, not the commit's reachability alone.

    A closed job is not enough. Closing can release a space whose commit never merged,
    so a closed-but-unmerged commit stays at risk.
    """
    if task.is_open or job is None:
        return False
    return bool(job.merged_at)


def agents_from_state(store: StateStore) -> list[BoardAgent]:
    """The roster as the state file knows it, with no multiplexer call.

    The record says who work was dispatched to, and which space a job lives in; it does
    not say whether a pane is running or idle now. Every field the state cannot know is
    left empty, so the page can show "-" and say the roster was not read live. The
    newest step to an agent wins its space and pane; an open job fills a gap.
    """
    spaces: dict[str, str] = {}
    panes: dict[str, str] = {}
    own_checkout: dict[str, bool] = {}
    for task in store.all():  # oldest first, so the newest step wins
        if not task.agent:
            continue
        if task.worktree:
            spaces[task.agent] = task.worktree
            # The state records both folders, so "this is the main checkout" can be
            # answered without the live list, which is the one thing about the space
            # that costs nothing to know.
            own_checkout[task.agent] = bool(task.repo_path) and _same(
                task.worktree, task.repo_path
            )
        if task.pane_id:
            panes[task.agent] = task.pane_id
    for job in store.all_jobs():
        for name in (job.agent, job.reviewer):
            if name and job.worktree and name not in spaces:
                spaces[name] = job.worktree

    agents: list[BoardAgent] = []
    for name in sorted(spaces):
        space = spaces[name]
        holding = next(
            (
                job
                for job in store.open_jobs()
                if _same(job.worktree, space) and job.agent == name
            ),
            None,
        )
        agents.append(
            BoardAgent(
                name=name,
                pane_id=panes.get(name),
                status=None,
                space=space,
                branch=holding.branch if holding else None,
                commit=holding.commit if holding else None,
                saved=None,
                main_checkout=own_checkout.get(name, False),
                detached=False,
            )
        )
    return agents


def _same(left: str | None, right: str | None) -> bool:
    """Two recorded folders that are the same place, whatever the separators."""
    if not left or not right:
        return False
    return (
        str(Path(left)).replace("\\", "/").rstrip("/").lower()
        == str(Path(right)).replace("\\", "/").rstrip("/").lower()
    )


def build(
    store: StateStore,
    *,
    defects_path_override: Path | None = None,
    front_door_name: str | None = None,
    capacity: str | None = None,
    agents: list[BoardAgent] | None = None,
    live_ok: bool = False,
    note: str | None = None,
) -> BoardData:
    """Everything the page is drawn from, read from the record now.

    The roster is built from the state file unless the caller passes a live one, which
    only `clowder board --live` does. The defects record fails open: a note in the
    worker section names the file and the fault, and the rest of the page still draws.
    """
    tasks = store.all()
    jobs = store.all_jobs()
    jobs_by_id = {job.id: job for job in jobs}
    record_path = defects_path_override or defects_path(store.path)
    defect_entries, defects_note = read_defects(DefectStore(record_path))
    open_job_ids = [job.id for job in jobs if job.is_open]

    # A held change whose branch or held ref has moved past the reviewed commit is not
    # ready for the owner's walk: the reviewer checked an older commit. Record the
    # current tip so the board asks for a re-review, with both commits named. A ref
    # that cannot be read is not called held; it is called unconfirmed.
    rereview: dict[str, str] = {}
    review_unconfirmed: set[str] = set()
    for job in jobs:
        if not (job.is_open and job.reviewer and job.review_commit and not job.has_pass):
            continue
        state, tip = job_review_state(job)
        if state == "moved" and tip:
            rereview[job.id] = tip
        elif state == "unknown":
            review_unconfirmed.add(job.id)

    # A commit on no branch is only at risk when its change is on no branch either. The
    # answer is a pure function of the commit and the refs that hold it, and asking git
    # costs a process, so it is memoised: one look per commit inside a write, and across
    # writes while the refs have not moved.
    stranded: set[str] = set()
    risk_notes: dict[str, str] = {}
    memo = gitcmd.RiskMemo(store.path.with_name(gitcmd.RISK_MEMO_FILENAME))
    memo.load()
    for task in tasks:
        if not (task.commit and task.worktree):
            continue
        task_job = jobs_by_id.get(task.job or "")
        if work_has_landed(task, task_job):
            continue  # the change is merged or closed; the commit is moot
        risk = memo.risk(task.worktree, task.commit)
        if risk == gitcmd.LOST:
            if job_moved_past(task, task_job):
                continue
            stranded.add(task.id)
        elif risk in (gitcmd.EMPTY, gitcmd.MERGE, gitcmd.UNKNOWN):
            risk_notes[task.id] = risk
    memo.save()

    catchup_items = catchup_mod.build(store, defect_entries)
    unseen = catchup_mod.unseen(catchup_items, store.seen())
    activity = handover_doc.last_activity(store)
    return BoardData(
        capacity=capacity,
        catchup=catchup_mod.lines(unseen),
        catchup_note=catchup_mod.summary_note(
            len(unseen), len(catchup_items), store.catchup_at
        ),
        tasks=tasks,
        jobs=jobs,
        queued=store.all_queued(),
        agents=agents if agents is not None else agents_from_state(store),
        state_path=str(store.path),
        generated_at=now_stamp(),
        # The same draw at whole seconds, so the next draw can compare it with the
        # record's last change without falling back to minutes.
        drawn_at=now_iso(),
        last_looked=store.catchup_at,
        last_change=stamp_of(activity) if activity is not None else None,
        last_change_at=activity.isoformat() if activity is not None else None,
        live_ok=live_ok,
        note=note if note is not None else (None if live_ok else STATE_ONLY_NOTE),
        front_door_name=front_door_name,
        stranded=stranded,
        risk_notes=risk_notes,
        rereview=rereview,
        review_unconfirmed=review_unconfirmed,
        dropped=audit.read_actions(store.path, "queue-drop"),
        repeated_defects=[
            defect for defect in defect_entries if defect.needs_action(open_job_ids)
        ],
        defects_note=defects_note,
    )
