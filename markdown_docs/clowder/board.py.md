# `clowder/board.py`

## Purpose

This module generates the board: a self-contained HTML page showing what is
happening in a crew. It is a file, not a panel. It survives a fresh context, holds
far more than a terminal can, and needs no server and no network.

Everything here is a pure function of the data it is handed, so it can be tested
without a browser and rendered without a multiplexer running. The filterable
columns are declared once in `FILTERABLE_FIELDS`; both the filter controls and the
rows' data attributes are generated from that one list, so a filter can never name
a field no row carries.

The page has, in order: "Waiting on you", the filter bar, "Waiting for a worker",
a To Do / In Progress / Done kanban, "Agents and spaces", "Answers", "Dropped
from the queue", and "By model".

## Public API

### Dataclasses

```python
@dataclass
class BoardAgent:
    name: str
    pane_id: str | None = None
    status: str | None = None
    space: str | None = None
    branch: str | None = None
    commit: str | None = None
    saved: bool = True
    main_checkout: bool = False
    detached: bool = False

@dataclass(frozen=True)
class FilterField:
    name: str
    label: str
    control: str
    values: tuple[str, ...] = ()
    note: str = ""

@dataclass
class BoardData:
    tasks: list[Task] = field(default_factory=list)
    jobs: list[Job] = field(default_factory=list)
    queued: list[Queued] = field(default_factory=list)
    agents: list[BoardAgent] = field(default_factory=list)
    state_path: str = ""
    generated_at: str = ""
    live_ok: bool = True
    note: str | None = None
    front_door_name: str | None = None
    stranded: set[str] = field(default_factory=set)
    risk_notes: dict[str, str] = field(default_factory=dict)
    rereview: dict[str, str] = field(default_factory=dict)
    review_unconfirmed: set[str] = field(default_factory=set)
    dropped: list[dict[str, object]] = field(default_factory=list)
```

- `BoardAgent` - one live agent as the board shows it. `space_label` (property)
  names the space; `place_label` (property) names where it is (branch/commit or
  checkout).
- `FilterField` - one filterable column: `name` (the data attribute), `label`,
  `control` (`text`, `choice`, `bool`, `age`, `date`), `values` and a help `note`.
- `BoardData` - everything the page is drawn from. `rereview` maps job id -> the
  commit tip used for the re-review check; `risk_notes` maps id -> a warning;
  `stranded` is the set of commits on no branch; `dropped` is the audit record of
  queue items that were dropped.

### Functions

```python
def label_value(labels: Sequence[str] | None, family: str) -> str
def now_stamp() -> str
def open_tasks(tasks: Sequence[Task]) -> list[Task]
def recent_answers(tasks: Sequence[Task], limit: int = 12) -> list[Task]
def dropped_items(data: BoardData) -> list[dict[str, object]]
def model_rollup(data: BoardData) -> list[tuple[str, int, int]]
def front_door_label(data: BoardData) -> str
def waiting_on_you(data: BoardData) -> list[str]
def waiting_for_worker(data: BoardData) -> list[str]
def render_board(data: BoardData) -> str
def write_board(data: BoardData, path: str | Path) -> Path
```

- `label_value(labels, family)` - the value of a `family:` label, or `""`.
- `now_stamp()` - a readable local time for the page header.
- `open_tasks(tasks)` - the tasks still open. `recent_answers(tasks, limit)` - the
  last `limit` tasks that have an answer, newest first.
- `dropped_items(data)` - the dropped queue items, newest first. A view, so a
  missing field is blank.
- `model_rollup(data)` - per model: changes passed first time, and changes the
  verifier sent back. A change is counted under the model on the writer's last
  reported step, and "sent back" means the reviewer ran more than one review step.
  A change still waiting for the owner's pass is counted in neither column. This is
  a figure to read, never a gate.
- `front_door_label(data)` - the front door's configured name, else
  `"the front door"`.
- `waiting_on_you(data)` - only what the owner must do: a decision, a review to
  merge, work at risk. The front door's own to-do list is not here.
- `waiting_for_worker(data)` - warnings for the front door: a step gone quiet, or a
  commit on no branch.
- `render_board(data)` - the whole page as one self-contained HTML string.
- `write_board(data, path)` - writes `render_board(data)` to `path` (creating
  parents) and returns the `Path`.

Module constants: `QUIET_AFTER_SECONDS = 120.0`, `HELD_QUIET_AFTER_SECONDS =
86400.0`, `TITLE`, `REFRESH_SECONDS = 30`, the CSS `STYLE`, the
`FILTER_SCRIPT`, `FILTERABLE_FIELDS`, `LABEL_FAMILIES`, `DONE_CAP = 20`.

## How It Works (internals)

The helpers are grouped by the public function that calls them. Several helpers
are only reached through `render_board`.

### `render_board(data)` - build the whole page

- `_e(value) -> str` - HTML-escapes any value (quoted). Every piece of text on the
  page came from a file or an agent, so it is always escaped.
- `_filter_controls() -> str` - generates the filter bar from `FILTERABLE_FIELDS`:
  a select for `choice`/`bool`/`age` fields, a text input otherwise; `date` fields
  are sort keys and get no control.
- `_table(headers, rows) -> str` - a simple escaped HTML table.
- `_id(value) -> str` - the small id span used in tables.
- `_section(title, count, inner, css="") -> str` - a titled section with an
  optional count.
- `_nothing(what) -> str` - the "nothing here" paragraph.
- `_drop_when(record) -> float` - seconds since a dropped record's `at` timestamp,
  or `0.0`.
- `_column(css, heading, count, table_id, rows, *, cap=0, note="", abandoned=0) ->
  str` - one kanban column; with a `cap` it becomes a collapsed `<details>` that
  also shows the abandoned count.
- `_kanban(data) -> str` - To Do, In Progress and Done, each the same filterable
  card table. A job and its steps share one card (a step of a job is folded into
  the job's card); a step with no job is its own card.
- `_tokens(task) -> str` / `_cost(task) -> str` - the token and cost cells, using
  `report.format_tokens` / `report.format_cost`.

### Card construction - used by `_kanban` and `render_board`

- `_card(title, ident, state, reason, *, repo="", kind="", labels=None,
  details="") -> str` - one card: badges, the title in the owner's words, the id,
  the state and the reason. No branch, commit or agent handle on the face; those go
  in `details`, folded.
- `_label_pills(labels) -> str` - one pill per label, coloured by its reserved
  family (`sev:`, `phase:`); other labels plain.
- `_job_details(job) -> str` - the folded handles for a job (effect, branch,
  commit, reviewer, pass/word yes/no).
- `_agent_reason(data, name, reviewer=None) -> str` - why a card waits: the
  reviewer is checking it, or the live agent and its status.
- `_queued_card(item) -> tuple[card, fields]`, `_job_card(data, job, state, shape)
  -> tuple[card, fields]`, `_step_card(data, task, state) -> tuple[card, fields]` -
  build one card and its filterable field dict.
- `_card_table(table_id, rows, *, extra="") -> str` - one column of cards as a
  table; each row carries the filterable fields so the page's filter script reaches
  cards with no change.

### Filterable values - the single source of truth

- `_field_names() -> tuple[str, ...]` - the field names from `FILTERABLE_FIELDS`.
- `_row_attrs(values) -> str` - emits `data-<name>="..."` for every filterable
  field, generated from the same list as the controls.
- `_job_fields(job, state) -> dict[str, str]` / `_task_fields(task, jobs=None) ->
  dict[str, str]` / `_queued_fields(item) -> dict[str, str]` - the filterable
  values for each record type. A step has no title of its own, so it inherits the
  job's words.
- `_newest(*moments) -> str` - the newest recorded timestamp among several.
- `_job_by_id(data) -> dict[str, Job]` - the jobs keyed by id.
- `_state_of(data, job) -> str` - the job's state word with the re-review states,
  and `"under review"` while a step is still in flight (an owner-facing gate word
  would be premature).
- `_step_state_in_words(task) -> str` - where a step is, derived from what it
  holds: `answered`, `failed`, `abandoned, no answer came`, `closed, no answer
  came`, or `in progress`.

### Text handling - used across the page

- `_flat(text) -> str` - collapses whitespace runs to one line.
- `_clip(text, limit=160) -> str` - `_flat` then a hard cut with `...`.
- `_full(text) -> str` - escaped and flattened, no cut; used where every word
  matters.
- `_more(text, limit=160) -> str` - escaped; a short preview behind `<details>` when
  long. Nothing an agent or the owner wrote is dropped, only folded, and the page
  stays one self-contained file with no extra script.

### Internal utilities

- `_full`, `_flat`, `_clip`, `_field_names` and `_row_attrs` are reached through
  other helpers rather than a public function by name, but they are part of the
  same rendering pipeline.
