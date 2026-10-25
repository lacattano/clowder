# `clowder/defects.py`

## Purpose

The one home for a known defect, quirk or workaround. Before this module, defects
lived in hand-written handover documents, which drifted: the same one could be
recorded three times and still bite, or vanish for two handovers and come back.
The rule the module exists for is stated at the top of the file: "if a defect is
not here, it is not known." Nothing else stores one - the handover and DESIGN.md
print a defect's one line and its id and never restate it.

One file per state file, beside it: `<state dir>/defects.json`. Plain JSON, written
atomically, under the state file's own lock. A defect is keyed by its normalised
line, so a second sighting is a count on the same record, not a second record.

## Public API

```python
SCHEMA_VERSION = 1
OPEN, HAS_JOB, FIXED, ACCEPTED = "open", "has-job", "fixed", "accepted-workaround"
STATES = (OPEN, HAS_JOB, FIXED, ACCEPTED)
SETTLED = (FIXED, ACCEPTED)

def defects_path(state_path: str | Path) -> Path
def normalise_line(text: str) -> str
def short_date(value: str | None) -> str

@dataclass
class Defect: ...

class DefectStore: ...

def merge_warning(defect: Defect) -> str | None
def describe(defect: Defect, *, open_jobs: Sequence[str] = ()) -> str
```

- `defects_path(state_path)` - `defects.json` beside the state file. One record, one
  place: a test or a second machine points at one state path and gets both.
- `normalise_line(text)` - the matching key: case and whitespace runs collapse, and
  punctuation at either end is dropped. The words in the middle count, so a
  different sentence is a different defect.
- `short_date(value)` - the `YYYY-MM-DD` part of a recorded moment, or `-`. The
  record keeps the whole moment so handovers can window it precisely; people read
  the date.
- `merge_warning(defect)` - the line that says a repetition is now the front door's
  job: seen twice, not settled, no job. `None` when there is nothing to say.
- `describe(defect, open_jobs=())` - one line for a list: id, count, last seen,
  state, job, the line, and whether it needs action.

### `Defect` (dataclass) - one known defect

Fields: `id`, `line`, `first_seen`, `last_seen`, `count`, `job`, `state`, `reason`,
and `extra` (unknown fields from a newer copy, kept).

- `seen_twice` (property) - `count >= 2`.
- `settled` (property) - `state` is `fixed` or `accepted-workaround`.
- `needs_action(open_jobs)` - seen twice, not settled, and either no job or a job
  that is not open. A closed job counts as no job: the fix is not in flight.
- `from_dict` / `to_dict` - like the state records, unknown keys survive a
  round trip.

### `DefectStore`

- `load()` - reads the file, refusing a wrong schema or a malformed record the way
  `StateStore` does. A record with no `id` is kept with an empty id, not filled in.
- `all()` - every defect, by id.
- `get(id)` - one defect, or `StateError` naming the list command.
- `find_by_line(line)` - the defect whose normalised line matches.
- `repeated(open_jobs)` - the defects a second sighting obliges someone to act on.
- `broken()` - `(key, why)` for entries that cannot be named: no id, an id that is
  not the key, or no line. The handover write refuses while one exists.
- `sighting(line, *, job, state, reason)` - one read-modify-write under the lock.
  Returns the defect and whether it is new. The same normalised line bumps the
  count and `last_seen`; a job, a state or a reason given now is recorded without
  erasing what was known. A `fixed` defect seen again is `open` again; a job given
  on a re-sighting makes an `open` one `has-job`.

## How It Works (internals)

- `_read` / `_save` - the JSON envelope: `{schema, next, defects}`. Writes go to a
  unique temp file and are moved into place, so a crash cannot leave half a file.
- `sighting` takes `thread_lock` and the state file's `file_lock` (imported from
  `state`) for the whole read-modify-write, so two `defects add` runs cannot lose
  one another's sighting. A store used twice in one process reloads inside the lock.
- `_int_field` - the id counter, refusing a non-integer rather than resetting it:
  a reset would reuse an id.
