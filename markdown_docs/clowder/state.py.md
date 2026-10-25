# `clowder/state.py`

## Purpose

This module owns the one state file. Every task, job and queued item is a record
in one JSON file. The file is written atomically, so a crash mid-write cannot
leave a half file, and it is plain text, so a human can read it and a diff can
show it.

The module defines three record types - `Task` (a dispatched piece of work),
`Job` (one line of work: a branch inside an agent's worktree) and `Queued` (a
decided-but-unsent piece of work) - plus `StateStore`, which loads, edits and
saves the file.

It also handles concurrent writers: a save that finds a newer file folds the
other writer's records in, or refuses a clashing edit loudly. A lost update is
never silent.

## Public API

### `Task` (dataclass) - one dispatched job

Key fields: `id`, `question`, `brief`, `shape` (`"ship"`/`"scout"`), `agent`,
`repo`, `repo_path`, `status`, `worktree`, `sender`, `job`, `branch`, `commit`,
timestamps (`created_at`, `dispatched_at`, `reported_at`, `abandoned_at`,
`closed_at`), the answer (`answer`, `answer_source`), the open decision
(`open_decision`, `decision_answer`, `decision_answered_at`), the owner item
(`owner_item`, `owner_item_at`, `owner_detail`), `usage`, `model`, `provider`,
`heavy`, and `extra` (unknown fields from a newer copy, kept).

Properties and methods:

- `decision_is_open` (property) -> `bool` - an `open_decision` exists with no
  `decision_answer`.
- `is_open` / `is_abandoned` / `is_closed` (properties) -> `bool` - test `status`.
- `age_seconds` (property) -> `float` - seconds from dispatch (or creation) to
  report.
- `from_dict(cls, data) -> Task` - build from a dict, routing unknown keys into
  `extra`. Raises `StateError` when the record is malformed.
- `to_dict() -> dict[str, object]` - the record as a dict, with `extra` merged back
  on top.

### `Job` (dataclass) - one line of work

A job is a branch switched inside the worktree of the agent that serves the repo,
so the expensive install is paid once per agent, not once per job.

Key fields: `id`, `label`, `title`, `effect`, `labels`, `repo`, `repo_path`,
`worktree`, `branch`, `base`, `base_commit`, `agent`, `status`, `created_at`,
`commit`, `reviewer`, `review_commit`, `handed_over_at`, `released_at`,
`held_ref`, the owner's words (`pass_shown`, `pass_answer`, `pass_at`, `pass_by`,
`pass_commit`, `merge_word`, `merge_word_at`, `merge_word_by`), `published_at`,
`merged_at`, `parked_at`, `park_commit`, `extra`.

Properties and methods:

- `is_open` (property) -> `bool`.
- `is_parked` (property) -> `bool` - a published job that released its checkout while
  it waits on the merge word.
- `name_in_words` (property) -> `str` - the change named the owner's way
  (`title`, else `label`).
- `effect_in_words` (property) -> `str` - one line on what the change does, else
  the branch.
- `state_in_words` (property) -> `str` - where the change is, derived from the
  record: `merged` -> `closed` -> `waiting on your merge word` -> `passed` ->
  `waiting on a walkthrough` -> `in progress`.
- `needs_rereview(tip) -> bool` - the job's work moved since the reviewer checked
  it (the tip is not the recorded `review_commit`).
- `state_in_words_at(tip, *, unreadable=False) -> str` - the state word, returning
  `needs re-review` or `review unconfirmed` when the tip cannot confirm the check.
- `has_pass` / `has_merge_word` (properties) -> `bool`.
- `age_seconds` (property) -> `float`.
- `from_dict` / `to_dict` - as for `Task`.

### `Queued` (dataclass) - decided but unsent

Fields: `id`, `brief`, `repo`, `why`, `agent`, `role`, `shape`, `question`,
`job`, `heavy`, `labels`, `created_at`, `extra`.

- `target` (property) -> `str` - the named agent, else the role, else `"?"`.
- `age_seconds` (property) -> `float`.
- `from_dict` / `to_dict` - as above.

### Module functions and constants

```python
def step_in_flight(job: Job, tasks: Sequence[Task]) -> bool
```

- `step_in_flight(job, tasks)` - true when any open task belongs to the job, so
  the owner is not invited yet. Applied only to the words in `OWNER_GATE_WORDS`.

Constants: `SCHEMA_VERSION = 2`; statuses `DISPATCHED`, `REPORTED`, `FAILED`,
`ABANDONED`, `CLOSED` and `STATUSES`; `SHAPES = ("ship", "scout")`; `OPEN`,
`JOB_STATUSES`; `OWNER_GATE_WORDS`.

### `StateStore` - read and write the state file

One instance per command run. Construct with a path:

```python
store = StateStore(path).load()
```

IO:

- `load() -> StateStore` - reads the file once and returns `self`. Missing or empty
  file is fine. A bad schema or bad JSON raises `StateError`.
- `save() -> None` - writes atomically (temp file + `os.replace`) under a file
  lock, folding in other writers first.

Ids and queued items: `next_id()`, `next_job_id()`, `next_queue_id()`,
`add_queued(item)`, `get_queued(id)`, `all_queued()`, `remove_queued(id)`.

Jobs: `add_job(job)`, `get_job(id)`, `all_jobs()`, `open_jobs()`,
`job_for_branch(branch)`.

Tasks: `add(task)`, `get(id)`, `all()`, `select(status=None, agent=None,
repo=None, open_only=False)`, `__len__`, `__iter__`.

Maintenance:

- `drop_unknown_fields(field) -> dict[str, int]` - removes one unknown field from
  every record that carries it and returns how many were dropped per record type.
- `rename_agent(old, new, *, old_path=None, new_path=None) -> dict[str, int]` -
  repoints every record that names the agent or its folder. Only in-memory records
  change; the caller saves once.

## How It Works (internals)

Each helper is listed under the public function that calls it.

### Record serialization - `Task.to_dict`, `Job.to_dict`, `Queued.to_dict`

- `_with_extra(record) -> dict[str, object]` - dumps a dataclass with `asdict`,
  pops `extra`, then merges `extra` under the known fields. This is what preserves
  fields a newer copy wrote that this code does not understand.

`from_dict` on each record does the mirror image: it splits keys into known and
unknown, passes the known ones to the dataclass, and stores the rest in `extra`.

### `StateStore.save()` - atomic, concurrency-safe write

- `_file_lock(path)` (context manager) - one writer at a time across processes,
  using `msvcrt.locking` on Windows and `fcntl.flock` elsewhere. The OS releases
  the lock on exit.
- `_temp_path() -> Path` - a unique temp path (pid + thread id + random token), so
  two writers never truncate each other's file.
- `_fold_in_other_writers()` - if the file's `rev` moved since load, merges the
  other writer's records into memory, or raises a clash. It also takes the max of
  each sequence counter.
- `_merge_records(label, current, base, disk, make)` - the merge rule per record:
  an untouched record is taken from disk; a disk record equal to the base wins for
  the other side; if both sides changed the same record differently it raises a
  clash.
- `_clash(label, key) -> StateError` - the error raised when two commands changed
  one record. The message tells the caller to re-run on the current file.

### `StateStore.load()` - read and validate

- `_read_payload() -> dict | None` - reads and parses the JSON, raising
  `StateError` on unreadable bytes, bad JSON, or a non-object top level.
- `_records(raw, name) -> dict` - returns one record group, insisting it is an
  object.
- `_int_field(raw, name) -> int` - reads a required integer field.
- `_record(record, key, label) -> dict` - insists a record is an object and
  defaults its `id` to its key.
- `_snapshot_base()` - stores a copy of every loaded record. `save` compares
  against this base to tell "changed by us" from "changed by someone else".
- `_note_extra(label, key, extra)` - prints one stderr line for each record that
  carried unknown fields, so a reader knows they were kept for a newer copy.

### `StateStore.rename_agent(...)` - repoint a renamed agent

- `_same_path(value, path) -> bool` - compares two folder paths the way the
  filesystem does (`normcase` + `abspath`), or `False` on a non-string or an OS
  error.
