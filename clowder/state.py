"""The one state file.

Every task is one record in one JSON file. It is written atomically, so a crash
mid-write cannot leave a half file behind, and it is plain text, so a human can
read it and a diff can show it.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

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
    # One line the front door records when something waits on the owner and no
    # other field carries it, e.g. a report it is holding for him.
    owner_item: str | None = None
    owner_item_at: str | None = None
    usage: dict[str, object] | None = None
    # Fields a newer copy wrote that this code does not know. Kept, never dropped,
    # so a newer copy can still read them.
    extra: dict[str, object] = field(default_factory=dict, repr=False, compare=False)

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
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}  # type: ignore[attr-defined]
        extra = {key: value for key, value in data.items() if key not in known}
        clean = {key: value for key, value in data.items() if key in known}
        try:
            task = cls(**clean)  # type: ignore[arg-type]
        except TypeError as exc:
            raise StateError(f"task record is malformed: {exc}") from exc
        task.extra = extra
        return task

    def to_dict(self) -> dict[str, object]:
        return _with_extra(self)


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
    # Fields a newer copy wrote that this code does not know; kept, never dropped.
    extra: dict[str, object] = field(default_factory=dict, repr=False, compare=False)

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
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}  # type: ignore[attr-defined]
        extra = {key: value for key, value in data.items() if key not in known}
        clean = {key: value for key, value in data.items() if key in known}
        try:
            job = cls(**clean)  # type: ignore[arg-type]
        except TypeError as exc:
            raise StateError(f"job record is malformed: {exc}") from exc
        job.extra = extra
        return job

    def to_dict(self) -> dict[str, object]:
        return _with_extra(self)


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
    # Fields a newer copy wrote that this code does not know; kept, never dropped.
    extra: dict[str, object] = field(default_factory=dict, repr=False, compare=False)

    @property
    def target(self) -> str:
        """Who it is for: a named agent, or the role a send must resolve."""
        return self.agent or self.role or "?"

    @property
    def age_seconds(self) -> float:
        return elapsed_seconds(self.created_at, None)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Queued:
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}  # type: ignore[attr-defined]
        extra = {key: value for key, value in data.items() if key not in known}
        clean = {key: value for key, value in data.items() if key in known}
        try:
            item = cls(**clean)  # type: ignore[arg-type]
        except TypeError as exc:
            raise StateError(f"queued record is malformed: {exc}") from exc
        item.extra = extra
        return item

    def to_dict(self) -> dict[str, object]:
        return _with_extra(self)


def _with_extra(record: Any) -> dict[str, object]:
    """A record as a dict, with any unknown fields a newer copy wrote kept on top."""
    fields = asdict(record)
    extra = fields.pop("extra", {})
    merged: dict[str, object] = dict(extra)  # type: ignore[arg-type]
    merged.update(fields)
    return merged


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """One writer at a time across processes. The OS releases it on exit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)  # type: ignore[attr-defined]
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
        except OSError:
            pass
        finally:
            handle.close()


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
        # A revision of the file, and the records we loaded. A save that finds a
        # newer revision folds the other writer's records in, or refuses loudly.
        self._base_rev = 0
        self._base_tasks: dict[str, dict[str, object]] = {}
        self._base_jobs: dict[str, dict[str, object]] = {}
        self._base_queued: dict[str, dict[str, object]] = {}
        self._thread_lock = threading.Lock()

    # -- io ----------------------------------------------------------------

    def load(self) -> StateStore:
        if self._loaded:
            return self
        raw = self._read_payload()
        if raw is None:
            self._loaded = True
            return self
        version = raw.get("schema")
        if version != SCHEMA_VERSION:
            raise StateError(
                f"{self.path}: schema {version!r}, this build writes "
                f"{SCHEMA_VERSION}. Refusing to guess."
            )
        self._seq = self._int_field(raw, "seq")
        self._job_seq = self._int_field(raw, "job_seq")
        self._queue_seq = self._int_field(raw, "queue_seq")
        self._base_rev = self._int_field(raw, "rev")

        self._jobs = {
            str(key): Job.from_dict(self._record(record, key, "job"))
            for key, record in self._records(raw, "jobs").items()
        }
        self._tasks = {
            str(key): Task.from_dict(self._record(record, key, "task"))
            for key, record in self._records(raw, "tasks").items()
        }
        self._queued = {
            str(key): Queued.from_dict(self._record(record, key, "queued item"))
            for key, record in self._records(raw, "queued").items()
        }
        # A field this code does not know is a later copy's business. Say which one
        # was skipped, once, instead of refusing the whole file.
        for key, job in self._jobs.items():
            self._note_extra("job", key, job.extra)
        for key, task in self._tasks.items():
            self._note_extra("task", key, task.extra)
        for key, item in self._queued.items():
            self._note_extra("queued item", key, item.extra)
        self._snapshot_base()
        self._loaded = True
        return self

    def _read_payload(self) -> dict[str, object] | None:
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
        return raw

    def _records(self, raw: dict[str, object], name: str) -> dict[str, object]:
        records = raw.get(name, {})
        if not isinstance(records, dict):
            raise StateError(f"{self.path}: {name} must be an object")
        return records

    def _int_field(self, raw: dict[str, object], name: str) -> int:
        value = raw.get(name, 0)
        if not isinstance(value, int):
            raise StateError(f"{self.path}: {name} must be an integer")
        return value

    def _record(self, record: object, key: object, label: str) -> dict[str, object]:
        if not isinstance(record, dict):
            raise StateError(f"{self.path}: {label} {key} is not an object")
        copy = dict(record)
        copy.setdefault("id", key)
        return copy

    def _snapshot_base(self) -> None:
        self._base_tasks = {key: task.to_dict() for key, task in self._tasks.items()}
        self._base_jobs = {key: job.to_dict() for key, job in self._jobs.items()}
        self._base_queued = {key: item.to_dict() for key, item in self._queued.items()}

    def _note_extra(self, label: str, key: object, extra: dict[str, object]) -> None:
        """One line for a field this code does not know, so the reader can tell."""
        if not extra:
            return
        names = ", ".join(sorted(str(name) for name in extra))
        print(
            f"{self.path}: {label} {key}: ignored unknown field(s) {names}; "
            "kept for a newer copy",
            file=sys.stderr,
        )

    def save(self) -> None:
        self.load()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock, _file_lock(self._lock_file()):
            self._fold_in_other_writers()
            payload = {
                "schema": SCHEMA_VERSION,
                "rev": self._base_rev + 1,
                "seq": self._seq,
                "job_seq": self._job_seq,
                "queue_seq": self._queue_seq,
                "tasks": {tid: task.to_dict() for tid, task in self._tasks.items()},
                "jobs": {jid: job.to_dict() for jid, job in self._jobs.items()},
                "queued": {qid: item.to_dict() for qid, item in self._queued.items()},
            }
            text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
            temp = self._temp_path()
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
            self._base_rev += 1
            self._snapshot_base()

    def _lock_file(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    def _temp_path(self) -> Path:
        """A path no other writer can share, so nobody truncates another's file."""
        token = os.urandom(4).hex()
        return self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.{token}.tmp"
        )

    def _fold_in_other_writers(self) -> None:
        """Merge a newer file into memory, or refuse a clashing edit.

        A lost update is never silent. A record another writer changed while this
        store held it is refused; anything else is folded in.
        """
        raw = self._read_payload()
        if raw is None:
            return
        version = raw.get("schema")
        if version != SCHEMA_VERSION:
            raise StateError(
                f"{self.path}: schema {version!r}, this build writes "
                f"{SCHEMA_VERSION}. Refusing to guess."
            )
        disk_rev = self._int_field(raw, "rev")
        if disk_rev == self._base_rev:
            return
        self._tasks = self._merge_records(
            "task", self._tasks, self._base_tasks, self._records(raw, "tasks"), Task.from_dict
        )
        self._jobs = self._merge_records(
            "job", self._jobs, self._base_jobs, self._records(raw, "jobs"), Job.from_dict
        )
        self._queued = self._merge_records(
            "queued item",
            self._queued,
            self._base_queued,
            self._records(raw, "queued"),
            Queued.from_dict,
        )
        self._seq = max(self._seq, self._int_field(raw, "seq"))
        self._job_seq = max(self._job_seq, self._int_field(raw, "job_seq"))
        self._queue_seq = max(self._queue_seq, self._int_field(raw, "queue_seq"))
        self._base_rev = disk_rev
        self._snapshot_base()

    def _merge_records(
        self,
        label: str,
        current: dict[str, Any],
        base: dict[str, dict[str, object]],
        disk: dict[str, object],
        make: Any,
    ) -> dict[str, Any]:
        now = {key: obj.to_dict() for key, obj in current.items()}
        merged: dict[str, dict[str, object]] = {}
        for key, record in disk.items():
            disk_record = self._record(record, key, label)
            if key not in now:
                was = base.get(key)
                if was is not None:
                    if was != disk_record:
                        raise self._clash(label, key)
                    continue  # our delete wins; nobody else touched it
                merged[key] = disk_record
                continue
            was = base.get(key)
            if was is not None and now[key] == was:
                merged[key] = disk_record  # we did not touch it; theirs wins
            elif was == disk_record:
                merged[key] = now[key]  # they did not touch it; ours wins
            elif now[key] == disk_record:
                merged[key] = now[key]  # both made the same record
            else:
                raise self._clash(label, key)
        for key, record in now.items():
            if key not in disk:
                merged[key] = record
        return {key: make(record) for key, record in merged.items()}

    def _clash(self, label: str, key: str) -> StateError:
        return StateError(
            f"{self.path}: {label} {key} was changed by another command; "
            "refusing a stale save. Re-run the command on the current file."
        )

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

    def drop_unknown_fields(self, field: str) -> dict[str, int]:
        """Remove one unknown field from every record that carries it.

        Returns how many were dropped, per record type. Only the in-memory
        records change; the caller takes a backup first and then calls `save`.
        """
        self.load()
        removed = {"task": 0, "job": 0, "queued": 0}
        groups = (("task", self._tasks), ("job", self._jobs), ("queued", self._queued))
        for label, records in groups:
            for record in records.values():
                if field in record.extra:
                    del record.extra[field]
                    removed[label] += 1
        return removed

    def __iter__(self) -> Iterator[Task]:
        return iter(self.all())
