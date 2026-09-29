"""The one state file.

Every task is one record in one JSON file. It is written atomically, so a crash
mid-write cannot leave a half file behind, and it is plain text, so a human can
read it and a diff can show it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .errors import StateError
from .timeutil import elapsed_seconds, now_iso

SCHEMA_VERSION = 2

DISPATCHED = "dispatched"
REPORTED = "reported"
FAILED = "failed"

STATUSES = (DISPATCHED, REPORTED, FAILED)

SHAPES = ("ship", "scout")

OPEN = "open"
CLOSED = "closed"

JOB_STATUSES = (OPEN, CLOSED)


@dataclass
class Task:
    """One dispatched job and everything known about it."""

    id: str
    question: str
    brief: str
    shape: str
    agent: str
    repo: str
    repo_path: str
    status: str = DISPATCHED
    worktree: str | None = None
    sender: str | None = None
    job: str | None = None
    branch: str | None = None
    commit: str | None = None
    created_at: str = field(default_factory=now_iso)
    dispatched_at: str | None = None
    pane_id: str | None = None
    agent_session: str | None = None
    mux_argv: list[str] = field(default_factory=list)
    mux_returncode: int | None = None
    mux_error: str | None = None
    reported_at: str | None = None
    answer: str | None = None
    answer_source: str | None = None
    open_decision: str | None = None
    decision_answer: str | None = None
    decision_answered_at: str | None = None
    usage: dict[str, object] | None = None

    @property
    def decision_is_open(self) -> bool:
        return bool(self.open_decision) and not self.decision_answer

    @property
    def is_open(self) -> bool:
        return self.status == DISPATCHED

    @property
    def age_seconds(self) -> float:
        return elapsed_seconds(self.dispatched_at or self.created_at, self.reported_at)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Task:
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        unknown = set(data) - known
        if unknown:
            raise StateError(f"task record has unknown fields: {sorted(unknown)}")
        try:
            return cls(**data)  # type: ignore[arg-type]
        except TypeError as exc:
            raise StateError(f"task record is malformed: {exc}") from exc

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class Job:
    """One line of work: a branch inside an agent's worktree.

    A job is not a checkout of its own. It is a branch switched inside the
    worktree of the agent that serves the repo, so the expensive part - the
    install - is paid once per agent instead of once per job.
    """

    id: str
    label: str
    repo: str
    repo_path: str
    worktree: str
    branch: str
    base: str
    agent: str
    status: str = OPEN
    created_at: str = field(default_factory=now_iso)
    closed_at: str | None = None
    commit: str | None = None
    reviewer: str | None = None
    review_commit: str | None = None
    handed_over_at: str | None = None
    released_at: str | None = None
    held_ref: str | None = None
    # The owner's recorded words. A pass lets the branch be published; a merge
    # word lets it be merged. They are separate on purpose: one is not the other.
    pass_shown: str | None = None
    pass_answer: str | None = None
    pass_at: str | None = None
    pass_by: str | None = None
    merge_word: str | None = None
    merge_word_at: str | None = None
    merge_word_by: str | None = None
    published_at: str | None = None
    merged_at: str | None = None

    @property
    def is_open(self) -> bool:
        return self.status == OPEN

    @property
    def has_pass(self) -> bool:
        return bool(self.pass_at)

    @property
    def has_merge_word(self) -> bool:
        return bool(self.merge_word_at)

    @property
    def age_seconds(self) -> float:
        return elapsed_seconds(self.created_at, self.closed_at)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Job:
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        unknown = set(data) - known
        if unknown:
            raise StateError(f"job record has unknown fields: {sorted(unknown)}")
        try:
            return cls(**data)  # type: ignore[arg-type]
        except TypeError as exc:
            raise StateError(f"job record is malformed: {exc}") from exc

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class Queued:
    """A decided-but-unsent piece of work.

    It is not a task: nothing has been sent and no agent has seen it. It exists so
    a block that clears later does not take the decision with it when the front
    door's context is refreshed.
    """

    id: str
    brief: str
    repo: str
    why: str
    agent: str | None = None
    role: str | None = None
    shape: str = "ship"
    question: str | None = None
    job: str | None = None
    created_at: str = field(default_factory=now_iso)

    @property
    def target(self) -> str:
        """Who it is for: a named agent, or the role a send must resolve."""
        return self.agent or self.role or "?"

    @property
    def age_seconds(self) -> float:
        return elapsed_seconds(self.created_at, None)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Queued:
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        unknown = set(data) - known
        if unknown:
            raise StateError(f"queued record has unknown fields: {sorted(unknown)}")
        try:
            return cls(**data)  # type: ignore[arg-type]
        except TypeError as exc:
            raise StateError(f"queued record is malformed: {exc}") from exc

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class StateStore:
    """Read and write the state file. One instance per command run."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._seq = 0
        self._job_seq = 0
        self._queue_seq = 0
        self._tasks: dict[str, Task] = {}
        self._jobs: dict[str, Job] = {}
        self._queued: dict[str, Queued] = {}
        self._loaded = False

    # -- io ----------------------------------------------------------------

    def load(self) -> StateStore:
        if self._loaded:
            return self
        if not self.path.exists():
            self._loaded = True
            return self
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StateError(f"{self.path}: cannot read: {exc}") from exc
        if not text.strip():
            self._loaded = True
            return self
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
        seq = raw.get("seq", 0)
        if not isinstance(seq, int):
            raise StateError(f"{self.path}: seq must be an integer")
        self._seq = seq

        job_seq = raw.get("job_seq", 0)
        if not isinstance(job_seq, int):
            raise StateError(f"{self.path}: job_seq must be an integer")
        self._job_seq = job_seq

        queue_seq = raw.get("queue_seq", 0)
        if not isinstance(queue_seq, int):
            raise StateError(f"{self.path}: queue_seq must be an integer")
        self._queue_seq = queue_seq

        jobs = raw.get("jobs", {})
        if not isinstance(jobs, dict):
            raise StateError(f"{self.path}: jobs must be an object")
        for job_id, record in jobs.items():
            if not isinstance(record, dict):
                raise StateError(f"{self.path}: job {job_id} is not an object")
            record.setdefault("id", job_id)
            self._jobs[str(job_id)] = Job.from_dict(record)

        tasks = raw.get("tasks", {})
        if not isinstance(tasks, dict):
            raise StateError(f"{self.path}: tasks must be an object")
        for task_id, record in tasks.items():
            if not isinstance(record, dict):
                raise StateError(f"{self.path}: task {task_id} is not an object")
            record.setdefault("id", task_id)
            self._tasks[str(task_id)] = Task.from_dict(record)

        queued = raw.get("queued", {})
        if not isinstance(queued, dict):
            raise StateError(f"{self.path}: queued must be an object")
        for item_id, record in queued.items():
            if not isinstance(record, dict):
                raise StateError(f"{self.path}: queued item {item_id} is not an object")
            record.setdefault("id", item_id)
            self._queued[str(item_id)] = Queued.from_dict(record)
        self._loaded = True
        return self

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": SCHEMA_VERSION,
            "seq": self._seq,
            "job_seq": self._job_seq,
            "queue_seq": self._queue_seq,
            "tasks": {tid: task.to_dict() for tid, task in self._tasks.items()},
            "jobs": {jid: job.to_dict() for jid, job in self._jobs.items()},
            "queued": {qid: item.to_dict() for qid, item in self._queued.items()},
        }
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        temp = self.path.with_name(self.path.name + ".tmp")
        try:
            temp.write_text(text, encoding="utf-8")
            os.replace(temp, self.path)
        except OSError as exc:
            raise StateError(f"{self.path}: cannot write: {exc}") from exc
        finally:
            if temp.exists() and temp != self.path:
                try:
                    temp.unlink()
                except OSError:
                    pass

    # -- tasks -------------------------------------------------------------

    def next_id(self) -> str:
        self.load()  # the counter lives in the file, so read it before using it
        self._seq += 1
        return f"t-{self._seq:04d}"

    def next_job_id(self) -> str:
        self.load()
        self._job_seq += 1
        return f"j-{self._job_seq:04d}"

    def next_queue_id(self) -> str:
        self.load()
        self._queue_seq += 1
        return f"q-{self._queue_seq:04d}"

    def add_queued(self, item: Queued) -> Queued:
        self.load()
        if item.id in self._queued:
            raise StateError(f"queued item {item.id} already exists")
        self._queued[item.id] = item
        return item

    def get_queued(self, item_id: str) -> Queued:
        self.load()
        item = self._queued.get(item_id)
        if item is None:
            raise StateError(f"no queued item {item_id!r} in {self.path}")
        return item

    def all_queued(self) -> list[Queued]:
        self.load()
        return sorted(self._queued.values(), key=lambda item: item.created_at)

    def remove_queued(self, item_id: str) -> Queued:
        self.load()
        item = self._queued.pop(item_id, None)
        if item is None:
            raise StateError(f"no queued item {item_id!r} in {self.path}")
        return item

    def add_job(self, job: Job) -> Job:
        self.load()
        if job.id in self._jobs:
            raise StateError(f"job {job.id} already exists")
        self._jobs[job.id] = job
        return job

    def get_job(self, job_id: str) -> Job:
        self.load()
        job = self._jobs.get(job_id)
        if job is None:
            raise StateError(f"no job {job_id!r} in {self.path}")
        return job

    def all_jobs(self) -> list[Job]:
        self.load()
        return sorted(self._jobs.values(), key=lambda j: j.created_at)

    def open_jobs(self) -> list[Job]:
        return [job for job in self.all_jobs() if job.is_open]

    def job_for_branch(self, branch: str) -> Job | None:
        for job in self.open_jobs():
            if job.branch == branch:
                return job
        return None

    def add(self, task: Task) -> Task:
        self.load()
        if task.id in self._tasks:
            raise StateError(f"task {task.id} already exists")
        self._tasks[task.id] = task
        return task

    def get(self, task_id: str) -> Task:
        self.load()
        task = self._tasks.get(task_id)
        if task is None:
            raise StateError(f"no task {task_id!r} in {self.path}")
        return task

    def all(self) -> list[Task]:
        self.load()
        return sorted(self._tasks.values(), key=lambda t: t.created_at)

    def select(
        self,
        status: str | None = None,
        agent: str | None = None,
        repo: str | None = None,
        open_only: bool = False,
    ) -> list[Task]:
        tasks = self.all()
        if open_only:
            tasks = [t for t in tasks if t.is_open]
        if status:
            tasks = [t for t in tasks if t.status == status]
        if agent:
            tasks = [t for t in tasks if t.agent == agent]
        if repo:
            tasks = [t for t in tasks if t.repo == repo]
        return tasks

    def __len__(self) -> int:
        self.load()
        return len(self._tasks)

    def __iter__(self) -> Iterator[Task]:
        return iter(self.all())
