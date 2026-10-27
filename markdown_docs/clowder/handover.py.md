# `clowder/handover.py`

## Purpose

The handover document, generated from the record. A handover is written at the end
of a session and read at the start of the next one. Written by hand it drifted: 13
working days had none, and a quirk could be recorded in three handovers in a row
and still bite.

Now a command composes it from what the tool already holds - what waits on the
owner, the steps in flight, the open changes, what merged since the last handover,
the queue count, and the defects whose `last_seen` falls inside the window - and
prints each defect as one line and an id. Exactly one section is hand-written, from
`handover-notes.md`, capped at ten lines.

The file is `HANDOVER-<date>.md` beside the state file. It records the moment it was
written in its own body (`generated: <iso>`), so `clowder start` can say how old it
is and whether work has happened since: a handover nobody updated is stale, and
stale must be visible.

## Public API

```python
HANDOVER_GLOB = "HANDOVER-*.md"
NOTES_NAME = "handover-notes.md"
NOTES_MAX_LINES = 10
GENERATED_MARK = "generated: "

@dataclass(frozen=True)
class Handover:
    path: Path | None
    written_at: datetime | None
    age_days: int | None
    stale: bool
    why: str | None = None

def handover_dir(state_path) -> Path
def handover_files(directory) -> list[Path]
def is_generated(path: Path) -> bool
def target_path(directory, at) -> Path
def written_at(path: Path) -> datetime
def age_days(moment, *, at=None) -> int | None
def last_activity(store, defects=None) -> datetime | None
def latest(store, defects=None, *, at=None) -> Handover
def window_start(store, at) -> Handover
def notes_text(state_path) -> tuple[str, int]
def owner_lines(store) -> list[str]
def compose(store, defects, *, previous, at=None) -> str
def write(store, defects, *, at=None) -> tuple[Path, int]
```

- `handover_dir(state_path)` - beside the state file, with the rest of the record.
- `handover_files(directory)` - every `HANDOVER-*.md`, oldest first by the date in
  the name, with a generated file after a hand-written one for the same day. Read-only
  history: nothing here ever writes to an existing hand-written one.
- `is_generated(path)` - does the body carry the `generated:` mark? That mark is what
  says the tool may replace the file.
- `target_path(directory, at)` - today's path. A generated file for today is
  replaced; a hand-written one is not, and the tool writes
  `HANDOVER-<date>.generated.md` beside it instead.
- `written_at(path)` - the `generated:` line if the file has one (a generated
  handover always does), else the file time, which is the only evidence the older
  hand-written files carry.
- `age_days(moment)` - whole calendar days in UTC, never negative.
- `last_activity(store, defects)` - the newest moment any record was touched: a step
  dispatched or reported, a job opened/handed over/passed/merged/parked, a queue
  item added, a defect sighted.
- `latest(store, defects)` - the newest handover with its age and staleness. Stale
  means recorded activity is newer than the handover's own moment; `why` says so in
  words a reader can act on.
- `window_start(store, at)` - the handover a new one measures its window from: the
  newest file whose name is not today's. A rewrite of today's file therefore keeps
  the day's window instead of dropping what the first write listed.
- `notes_text(state_path)` - the one hand-written section, cut at ten lines. Returns
  the text and how many lines were dropped.
- `owner_lines(store)` - what waits on the owner, from `owner.waiting_on_owner` (the
  same list `clowder start` and the board show), one line each with the word that
  answers it and the record's id, because the handover is a document a later session
  reads.
- `compose(...)` - the document text: six sections, five derived and the notes last.
- `write(...)` - validates the defects (`broken()` entries stop it), composes, writes
  `HANDOVER-<date>.md`, returns the path and the dropped-note count.

## How It Works (internals)

- The window is `[previous.written_at, now)`; with no earlier handover it is the
  whole record. Defects and merges are filtered on that window, so a defect seen
  before the last handover is not listed again.
- `_landed(store, since)` - jobs merged inside the window, oldest first, reversed
  for reading.
- `_window_defects(defects, since)` - entries whose `last_seen` is inside the
  window.
- `_in_flight(store)` - open steps, plus open jobs with no open step. A job whose
  only missing gate is the owner's merge word is not here: it is in the owner's
  section, once.
- `_bullets(lines, empty)` - a bullet list, or one honest "(nothing: ...)" line.
- Composition is a pure function of the store, the defects and the previous
  handover, so writing twice produces the same document rather than an accumulation.
