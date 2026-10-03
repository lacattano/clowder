"""The board: a generated HTML page of what is happening.

A file, not a panel. It survives a fresh context, it holds far more than a
terminal can, and it needs no server and no network.

Everything here is a pure function of what it is handed, so it can be tested
without a browser and rendered without a multiplexer running. The filterable
columns are declared once, in FILTERABLE_FIELDS, and both the controls and the
rows' data come from that list.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from pathlib import Path

from .report import format_cost, format_tokens
from .state import CLOSED, Job, Queued, Task
from .timeutil import elapsed_seconds, human_age, human_duration

# An open step whose agent is idle for longer than this is worth a second look:
# nothing is running, so nobody may be coming back to it.
QUIET_AFTER_SECONDS = 120.0

# A held change the owner has not walked for this long is worth a flag, so a change
# cannot sit in the queue unnoticed for days.
HELD_QUIET_AFTER_SECONDS = 86400.0

TITLE = "clowder board"

# An open tab follows along on its own: no server, no network. The one script is
# the page's own filter loop, inline; nothing is fetched.
REFRESH_SECONDS = 30

STYLE = """
:root {
  --ink: #1b1b1f; --muted: #6b6b76; --line: #e2e2e8; --bg: #fbfbfd;
  --card: #ffffff; --accent: #2f6f4f; --warn: #8a5300; --warnbg: #fff8e8;
}
@media (prefers-color-scheme: dark) {
  :root {
    --ink: #e8e8ee; --muted: #9a9aa6; --line: #2d2d36; --bg: #131317;
    --card: #1b1b21; --accent: #7fc39b; --warn: #e2b463; --warnbg: #2a2317;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2rem 1.25rem 4rem; background: var(--bg); color: var(--ink);
  font: 15px/1.5 "Segoe UI", system-ui, -apple-system, sans-serif;
}
main { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
h2 {
  font-size: 1rem; margin: 0 0 .6rem; text-transform: uppercase;
  letter-spacing: .06em; color: var(--muted);
}
.meta { color: var(--muted); margin: 0 0 1.5rem; font-size: .85rem; }
code, .id { font-family: Consolas, "Cascadia Mono", monospace; font-size: .87em; }
section {
  background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 1rem 1.1rem; margin: 0 0 1rem;
}
section.needs { background: var(--warnbg); border-color: var(--warn); }
.count { color: var(--muted); font-weight: 400; }
table { width: 100%; border-collapse: collapse; }
th, td {
  text-align: left; padding: .4rem .5rem; border-bottom: 1px solid var(--line);
  vertical-align: top;
}
th {
  color: var(--muted); font-weight: 600; font-size: .78rem; text-transform: uppercase;
  letter-spacing: .04em;
}
tbody tr:last-child td { border-bottom: 0; }
.pill {
  display: inline-block; padding: .05rem .45rem; border-radius: 999px;
  border: 1px solid var(--line); font-size: .75rem; color: var(--muted); white-space: nowrap;
}
.pill.open { color: var(--accent); border-color: var(--accent); }
.pill.bad { color: var(--warn); border-color: var(--warn); }
.nothing { color: var(--muted); margin: 0; }
.why { display: block; color: var(--muted); font-size: .85rem; }
ul { margin: 0; padding-left: 1.1rem; }
li { margin: .25rem 0; }
footer { color: var(--muted); font-size: .8rem; margin-top: 1.5rem; }
.controls { display: flex; flex-wrap: wrap; gap: .75rem 1rem; align-items: center; }
.controls label { display: flex; gap: .3rem; align-items: baseline; font-size: .8rem; }
.controls label span { color: var(--muted); }
.controls select, .controls input {
  font: inherit; font-size: .85rem; padding: .15rem .3rem;
  border: 1px solid var(--line); border-radius: 6px;
  background: var(--bg); color: var(--ink);
}
.empty { color: var(--muted); margin: .6rem 0 0; }
"""

# Client-side filtering over the data already on the page: no server, no store, no query
# engine. Each filter reads `data-<name>` off a row, using the column names in
# FILTERABLE_FIELDS; an age is computed from the row's `created` timestamp rather than
# stored anywhere.
FILTER_SCRIPT = """
(function () {
  var bar = document.querySelector('[data-filters]');
  if (!bar) { return; }
  var bodies = document.querySelectorAll('tbody[data-rows]');
  var controls = bar.querySelectorAll('[data-filter]');
  var sort = bar.querySelector('[data-sort]');

  function cell(row, name) {
    return (row.getAttribute('data-' + name) || '').toLowerCase();
  }
  function keep(row) {
    for (var i = 0; i < controls.length; i++) {
      var name = controls[i].getAttribute('data-filter');
      var wanted = (controls[i].value || '').toLowerCase();
      if (!wanted) { continue; }
      var have = cell(row, name);
      if (name === 'age') {
        if (Number(have) < Number(wanted)) { return false; }
      } else if (have !== wanted) {
        return false;
      }
    }
    return true;
  }
  function newestFirst() { return sort && sort.value === 'newest'; }

  function apply() {
    var flip = newestFirst();
    for (var b = 0; b < bodies.length; b++) {
      var body = bodies[b];
      var rows = body.querySelectorAll('tr');
      var shown = 0;
      for (var r = 0; r < rows.length; r++) {
        var on = keep(rows[r]);
        rows[r].hidden = !on;
        if (on) { shown++; }
      }
      var ordered = [];
      for (var k = 0; k < rows.length; k++) { ordered.push(rows[k]); }
      ordered.sort(function (a, b) {
        var one = Date.parse(cell(a, 'created')) || 0;
        var two = Date.parse(cell(b, 'created')) || 0;
        if (one === two) { return 0; }
        return (one < two ? -1 : 1) * (flip ? -1 : 1);
      });
      for (var j = 0; j < ordered.length; j++) { body.appendChild(ordered[j]); }
      var empty = document.querySelector('[data-empty][data-for="' + body.id + '"]');
      if (empty) { empty.hidden = shown !== 0; }
    }
  }

  for (var c = 0; c < controls.length; c++) { controls[c].addEventListener('change', apply); }
  if (sort) { sort.addEventListener('change', apply); }
  apply();
})();
"""


@dataclass(frozen=True)
class BoardAgent:
    """One live agent, as the board shows it."""

    name: str
    pane_id: str | None = None
    status: str | None = None
    space: str | None = None
    branch: str | None = None
    commit: str | None = None
    saved: bool = True
    main_checkout: bool = False
    detached: bool = False

    @property
    def space_label(self) -> str:
        if not self.space:
            return "(unreported)"
        return self.space

    @property
    def place_label(self) -> str:
        if not self.space:
            return "unknown"
        if self.main_checkout:
            return "the main checkout"
        if self.branch:
            return f"{self.branch} @ {self.commit or 'no commit'}"
        if self.detached:
            return f"free, on {self.commit or 'no commit'}"
        return f"{self.branch or 'free'} @ {self.commit or 'no commit'}"


@dataclass(frozen=True)
class FilterField:
    """One filterable column of the board.

    The name is what a filter control uses and what a row carries as `data-<name>`.
    The control says what kind of input it wants. Every value is read from a field the
    record already holds; nothing here is derived state kept for filtering. The two date
    columns are the sort and age keys, and take no control: the page computes an age from
    `created` rather than being handed one.
    """

    name: str
    label: str
    control: str  # "text", "choice", "bool", or "date"
    values: tuple[str, ...] = ()  # the choices for a "choice" or "bool" control
    note: str = ""


# The column set, declared once. Both the filter controls and every row's data attributes
# are generated from this list, so a filter cannot name a field the rows do not carry, and
# a column is never typed twice. When the store moves to SQL this list is the column set.
FILTERABLE_FIELDS: tuple[FilterField, ...] = (
    FilterField("repo", "repo", "text", note="the record's repo"),
    FilterField("kind", "kind", "choice", ("scout", "ship"), "the record's shape"),
    FilterField("status", "status", "choice", (), "the record's status"),
    FilterField("agent", "agent", "choice", (), "the record's agent"),
    FilterField("job", "job", "text", note="the task's job, or a job's own id"),
    FilterField(
        "waiting-on-you",
        "waiting on you",
        "bool",
        ("yes", "no"),
        "a task with an owner item, or a job with no pass or no merge word",
    ),
    FilterField(
        "blocked",
        "blocked",
        "bool",
        ("yes", "no"),
        "blocked_by - the field is not recorded yet, so this reads empty until it lands",
    ),
    FilterField("created", "created", "date", (), "created_at, ISO-8601"),
    FilterField("updated", "updated", "date", (), "the newest recorded timestamp, ISO-8601"),
    FilterField(
        "age",
        "older than",
        "age",
        (),
        "seconds since created, computed at render time from that recorded field",
    ),
)

# The queue-triage work (t-0327) adds a `verdict` to queued items and a `blocked_by` to jobs.
# When those fields are recorded, add them here and as one column each - nothing else moves:
#
#     FilterField("verdict", "verdict", "choice", ("do", "skip", "done")),
#     FilterField("blocked", "blocked", "choice", (), "the record's blocked_by"),
#
# The `blocked` column is already above, waiting on its field.


@dataclass
class BoardData:
    """Everything the page is drawn from."""

    tasks: list[Task] = field(default_factory=list)
    jobs: list[Job] = field(default_factory=list)
    queued: list[Queued] = field(default_factory=list)
    agents: list[BoardAgent] = field(default_factory=list)
    state_path: str = ""
    generated_at: str = ""
    live_ok: bool = True
    note: str | None = None
    # The front door's own name, for text that means it and not the owner.
    front_door_name: str | None = None
    # Task ids whose commit is on no branch, so the work exists only in one folder.
    stranded: set[str] = field(default_factory=set)
    # Task ids whose commit is on no branch and whose content could not be judged,
    # keyed to why: `merge`, `empty`, or `unknown` (a real read failure).
    risk_notes: dict[str, str] = field(default_factory=dict)


def _e(value: object) -> str:
    """Every piece of text on this page came from a file or an agent. Escape it."""
    return escape(str(value if value is not None else ""), quote=True)


def now_stamp() -> str:
    """A readable local time for the page header."""
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _clip(text: str, limit: int = 160) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "..."


def open_tasks(tasks: Sequence[Task]) -> list[Task]:
    return [task for task in tasks if task.is_open]


def recent_answers(tasks: Sequence[Task], limit: int = 12) -> list[Task]:
    answered = [task for task in tasks if task.answer]
    return list(reversed(answered))[:limit]


def front_door_label(data: BoardData) -> str:
    """The front door's own name, or a plain phrase when none is configured."""
    return data.front_door_name or "the front door"


def waiting_on_you(data: BoardData) -> list[str]:
    """Only what the owner must do: a decision, a review to merge, work at risk.

    The front door's own to-do list is not here. A queued item or a step with no
    answer is chased by the front door, and lives in `waiting_for_worker`.
    """
    lines: list[str] = []

    # Every question the owner must answer is numbered, whether it was recorded
    # with `owner --item` or as an open decision on a report. One sequence, so a
    # number names one question on the whole page.
    questions: list[tuple[str, str]] = []
    for task in data.tasks:
        if task.owner_item and task.status != CLOSED:
            questions.append(
                (
                    task.owner_item_at or task.created_at,
                    f"{_e(_clip(task.owner_item or '', 240))} "
                    f"<span class='why'>your move, [{_e(task.id)}]</span>",
                )
            )
    for task in data.tasks:
        if task.open_decision and task.decision_is_open and task.status != CLOSED:
            questions.append(
                (
                    task.created_at,
                    f"{_e(_clip(task.open_decision, 200))} "
                    f"<span class='why'>decision, [{_e(task.id)}] asked by "
                    f"{_e(task.agent)} about {_e(_clip(task.question, 120))}</span>",
                )
            )
    ordered = sorted(questions, key=lambda item: item[0])
    for number, (_, line) in enumerate(ordered, start=1):
        lines.append(f"<b>{number}.</b> {line}")

    for task in data.tasks:
        if task.id in data.stranded:
            lines.append(
                f"<b>work at risk</b> <span class='id'>{_e(task.id)}</span> "
                f"commit {_e(task.commit)} is on no branch "
                f"<span class='why'>it lives only in {_e(task.worktree)}; reusing "
                "that space would lose it</span>"
            )

    # The held changes waiting for him, oldest first. A queue with an age, so a
    # change cannot sit unnoticed; past a day it is flagged.
    held = [
        job
        for job in data.jobs
        if job.is_open and job.reviewer and job.review_commit and not job.has_pass
    ]
    for job in sorted(held, key=lambda item: item.handed_over_at or item.created_at):
        if any(task.job == job.id and task.is_open for task in data.tasks):
            continue  # the review step is still running; not the owner's turn yet
        seconds = elapsed_seconds(job.handed_over_at, None)
        quiet = " - gone quiet" if seconds >= HELD_QUIET_AFTER_SECONDS else ""
        lines.append(
            f"<b>held for your review</b> {_e(job.branch)} was handed to {_e(job.reviewer)} "
            f"<span class='why'>[{_e(job.id)}] {_e(job.label)}; waiting "
            f"{_e(human_age(seconds))}{quiet}. Walk it in the reviewer's space.</span>"
        )

    for job in data.jobs:
        if not job.is_open:
            continue
        open_steps = [task for task in data.tasks if task.job == job.id and task.is_open]
        if not open_steps and job.has_pass and not job.has_merge_word:
            reviewed_by = f" was reviewed by {_e(job.reviewer)}" if job.reviewer else ""
            lines.append(
                f"<b>your word</b> {_e(job.branch)}{reviewed_by} "
                f"<span class='why'>[{_e(job.id)}] {_e(job.label)}; the front door merges "
                "it once you give your merge word and the checks are green</span>"
            )

    return lines


def waiting_for_worker(data: BoardData) -> list[str]:
    """The front door's own list: queued items, and steps with no answer yet.

    None of these needs the owner, so none is counted under "Waiting on you".
    """
    lines: list[str] = []

    for item in data.queued:
        lines.append(
            f"<b>queued</b> <span class='id'>{_e(item.id)}</span> "
            f"for {_e(item.target)} in {_e(item.repo)} "
            f"<span class='why'>{_e(_clip(item.why, 160))} - "
            f"{_e(_clip(item.question or item.brief, 120))}</span>"
        )

    for task in open_tasks(data.tasks):
        if task.age_seconds < QUIET_AFTER_SECONDS:
            continue
        agent = next((a for a in data.agents if a.name == task.agent), None)
        if agent is None:
            # Only an answer when the live list was actually readable. Otherwise the
            # page would cry wolf every time the multiplexer was down.
            if data.live_ok:
                lines.append(
                    f"<b>gone quiet</b> <span class='id'>{_e(task.id)}</span> "
                    f"{_e(task.agent)} is not in the live agent list at all "
                    f"<span class='why'>{_e(_clip(task.question, 120))}</span>"
                )
            continue
        if (agent.status or "idle") != "working":
            lines.append(
                f"<b>no answer</b> <span class='id'>{_e(task.id)}</span> "
                f"{_e(task.agent)} is {_e(agent.status or 'idle')} and has said nothing "
                f"for {_e(human_age(task.age_seconds))} "
                f"<span class='why'>{_e(_clip(task.question, 120))}</span>"
            )

    for task in data.tasks:
        risk = data.risk_notes.get(task.id)
        if risk is None:
            continue
        if risk == "merge":
            lines.append(
                f"<b>merge commit</b> <span class='id'>{_e(task.id)}</span> "
                f"commit {_e(task.commit)} is on no branch but carries its parents' "
                f"content <span class='why'>held in {_e(task.worktree)}</span>"
            )
        elif risk == "empty":
            lines.append(
                f"<b>empty change</b> <span class='id'>{_e(task.id)}</span> "
                f"commit {_e(task.commit)} is on no branch but changes nothing "
                f"<span class='why'>nothing is lost in {_e(task.worktree)}</span>"
            )
        else:
            lines.append(
                f"<b>could not check</b> <span class='id'>{_e(task.id)}</span> "
                f"commit {_e(task.commit)} is on no branch, and it could not be told "
                "whether its change is already on one "
                f"<span class='why'>held in {_e(task.worktree)}</span>"
            )

    return lines


def _field_names() -> tuple[str, ...]:
    return tuple(field.name for field in FILTERABLE_FIELDS)


def _row_attrs(values: dict[str, str]) -> str:
    """Every filterable field on a row, generated from FILTERABLE_FIELDS.

    One source of truth: the controls read these names, the controls are generated from
    the same list, and a filter cannot name a field no row carries.
    """
    return "".join(f' data-{name}="{_e(values.get(name, ""))}"' for name in _field_names())


def _job_fields(job: Job) -> dict[str, str]:
    """A job's filterable values, each read from a field the job records."""
    updated = _newest(job.merged_at, job.closed_at, job.published_at, job.released_at)
    waiting = not job.has_pass or not job.has_merge_word
    return {
        "repo": job.repo,
        # A job records no shape; its steps do. Empty until a job records one.
        "kind": getattr(job, "shape", "") or "",
        "status": job.status,
        "agent": job.agent,
        "job": job.id,
        "waiting-on-you": "yes" if waiting else "no",
        "blocked": getattr(job, "blocked_by", "") or "",
        "created": job.created_at or "",
        "updated": updated,
        "age": str(int(job.age_seconds)),
    }


def _task_fields(task: Task) -> dict[str, str]:
    """A step's filterable values, each read from a field the step records."""
    updated = _newest(task.reported_at, task.abandoned_at, task.closed_at)
    waiting = bool(task.owner_item) or bool(task.decision_is_open)
    return {
        "repo": task.repo,
        "kind": task.shape,
        "status": task.status,
        "agent": task.agent,
        "job": task.job or "",
        "waiting-on-you": "yes" if waiting else "no",
        "blocked": getattr(task, "blocked_by", "") or "",
        "created": task.dispatched_at or task.created_at or "",
        "updated": updated,
        "age": str(int(task.age_seconds)),
    }


def _newest(*moments: str | None) -> str:
    """The newest of several recorded timestamps, as one ISO-8601 string."""
    stamped = [moment for moment in moments if moment]
    return max(stamped) if stamped else ""


def _filter_controls() -> str:
    """The filter bar, generated from FILTERABLE_FIELDS.

    The fields with a date control are the sort and age keys; they carry data, not a
    control, so the owner is not asked to type a timestamp.
    """
    controls: list[str] = []
    for column in FILTERABLE_FIELDS:
        if column.control == "date":
            continue
        if column.control == "choice" and column.values:
            options = ["<option value=''>any</option>"] + [
                f"<option value='{_e(value)}'>{_e(value)}</option>" for value in column.values
            ]
            control = f'<select data-filter="{_e(column.name)}">{"".join(options)}</select>'
        elif column.control == "bool":
            options = [
                "<option value=''>any</option>",
                "<option value='yes'>yes</option>",
                "<option value='no'>no</option>",
            ]
            control = f'<select data-filter="{_e(column.name)}">{"".join(options)}</select>'
        elif column.control == "age":
            options = [
                "<option value=''>any</option>",
                "<option value='3600'>1 hour</option>",
                "<option value='28800'>8 hours</option>",
                "<option value='86400'>1 day</option>",
                "<option value='259200'>3 days</option>",
                "<option value='604800'>7 days</option>",
            ]
            control = f'<select data-filter="{_e(column.name)}">{"".join(options)}</select>'
        else:
            control = (
                f'<input data-filter="{_e(column.name)}" type="text" '
                f'placeholder="{_e(column.label)}" size="10">'
            )
        controls.append(f"<label><span>{_e(column.label)}</span>{control}</label>")
    controls.append(
        "<label><span>sort</span><select data-sort>"
        "<option value='oldest'>oldest first</option>"
        "<option value='newest'>newest first</option>"
        "</select></label>"
    )
    return "<div class='controls'>" + "".join(controls) + "</div>"


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    head = "".join(f"<th>{_e(name)}</th>" for name in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _filter_table(
    table_id: str,
    headers: Sequence[str],
    rows: Sequence[tuple[Sequence[str], dict[str, str]]],
) -> str:
    """A table whose rows carry the filterable fields, plus its empty state."""
    head = "".join(f"<th>{_e(name)}</th>" for name in headers)
    body = "".join(
        f"<tr{_row_attrs(fields)}>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"
        for cells, fields in rows
    )
    empty = (
        f"<p class='empty' data-empty data-for=\"{_e(table_id)}\" hidden>"
        "Nothing matches those filters.</p>"
    )
    return (
        f"<table><thead><tr>{head}</tr></thead>"
        f'<tbody id="{_e(table_id)}" data-rows>{body}</tbody></table>{empty}'
    )


def _id(value: str | None) -> str:
    return f"<span class='id'>{_e(value or '-')}</span>"


def _section(title: str, count: int | None, inner: str, css: str = "") -> str:
    suffix = f" <span class='count'>{count}</span>" if count is not None else ""
    return f"<section class='{css}'><h2>{_e(title)}{suffix}</h2>{inner}</section>"


def _nothing(what: str) -> str:
    return f"<p class='nothing'>{_e(what)}</p>"


def render_board(data: BoardData) -> str:
    """The whole page, as one self-contained file."""
    needs = waiting_on_you(data)
    if needs:
        needs_html = "<ul>" + "".join(f"<li>{line}</li>" for line in needs) + "</ul>"
    else:
        needs_html = _nothing(
            "Nothing is recorded as waiting on you. The tool shows only what a command "
            "recorded; a report the front door holds in its words will not appear here."
        )

    worker = waiting_for_worker(data)
    if worker:
        listed = "<ul>" + "".join(f"<li>{line}</li>" for line in worker) + "</ul>"
        worker_html = (
            "<p class='why'>These wait for a worker, not for you. "
            f"{_e(front_door_label(data))} chases them.</p>{listed}"
        )
    else:
        worker_html = _nothing(f"Nothing is waiting for {front_door_label(data)}.")

    open_rows = [
        (
            [
                _id(task.id),
                _id(task.job),
                _e(task.agent),
                _e(task.repo),
                _e(task.branch or task.commit or "-"),
                _e(human_age(task.age_seconds)),
                _e(_clip(task.question, 120)),
                _agent_state(data, task.agent),
            ],
            _task_fields(task),
        )
        for task in open_tasks(data.tasks)
    ]
    open_html = (
        _filter_table(
            "rows-open",
            ["id", "job", "agent", "repo", "branch", "age", "question", "agent"],
            open_rows,
        )
        if open_rows
        else _nothing("No open steps. Nothing is being worked on.")
    )

    agent_rows = [
        [
            _e(agent.name),
            _e(agent.pane_id or "-"),
            _e(agent.status or "-"),
            _e(agent.place_label),
            _e("yes" if agent.saved else "no"),
            _e(agent.space_label),
        ]
        for agent in data.agents
    ]
    agents_html = (
        _table(["name", "pane", "state", "space holds", "saved", "folder"], agent_rows)
        if agent_rows
        else _nothing("No live agents were found.")
    )
    if data.note:
        agents_html += f"<p class='nothing'>{_e(data.note)}</p>"

    answer_rows = [
        [
            _id(task.id),
            _e(task.agent),
            _e(human_duration(task.age_seconds)),
            _e(_tokens(task)),
            _e(_cost(task)),
            _e(_clip(task.answer or "", 200)),
        ]
        for task in recent_answers(data.tasks)
    ]
    answers_html = (
        _table(["id", "agent", "took", "tokens", "cost", "answer"], answer_rows)
        if answer_rows
        else _nothing("Nothing has been answered yet.")
    )

    job_rows = [
        (
            [
                _id(job.id),
                f"<span class='pill {'open' if job.is_open else ''}'>{_e(job.status)}</span>",
                _e(job.label),
                _e(job.branch),
                _e(job.agent),
                _e(job.reviewer or "-"),
                _e(job.commit or "-"),
                _e("yes" if job.has_pass else "no"),
                _e("yes" if job.has_merge_word else "no"),
                _e(human_age(job.age_seconds)),
            ],
            _job_fields(job),
        )
        for job in data.jobs
    ]
    jobs_html = (
        _filter_table(
            "rows-jobs",
            [
                "id",
                "state",
                "work",
                "branch",
                "agent",
                "reviewer",
                "commit",
                "pass",
                "word",
                "age",
            ],
            job_rows,
        )
        if job_rows
        else _nothing("No jobs yet.")
    )

    filterable = bool(open_rows or job_rows)
    filters_html = (
        _section(
            "Filters",
            None,
            _filter_controls()
            + "<p class='why'>These narrow the open steps and the jobs below, on the "
            "page, where you already are. What waits on you is above them and is not "
            "narrowed.</p>",
            css="filters",
        )
        if filterable
        else ""
    )

    live = "live" if data.live_ok else "not readable"
    footer = (
        f"generated {_e(data.generated_at)} | state {_e(data.state_path)} | agent list {live}"
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH_SECONDS}">
<title>{_e(TITLE)}</title>
<style>{STYLE}</style>
</head>
<body>
<main>
<h1>{_e(TITLE)}</h1>
<p class="meta">What is queued, what is underway, and what is waiting on you.</p>
<p class="meta">Generated {_e(data.generated_at)}. This page is a snapshot: it changes only
when a clowder command rewrites it, and an open tab keeps what it loaded. If that time is not
now, the page is stale.</p>
{_section("Waiting on you", len(needs), needs_html, css="needs")}
{filters_html}
{_section("Waiting for a worker", len(worker), worker_html)}
{_section("Open steps", len(open_rows), open_html)}
{_section("Agents and spaces", len(agent_rows), agents_html)}
{_section("Answers", len(answer_rows), answers_html)}
{_section("Jobs", len(job_rows), jobs_html)}
<footer>{footer}</footer>
</main>
<script>{FILTER_SCRIPT}</script>
</body>
</html>
"""


def _agent_state(data: BoardData, name: str) -> str:
    agent = next((a for a in data.agents if a.name == name), None)
    if agent is None:
        return "-"
    css = "open" if (agent.status or "") == "working" else ""
    return f"<span class='pill {css}'>{_e(agent.status or '-')}</span>"


def _tokens(task: Task) -> str:
    usage = task.usage or {}
    total = usage.get("total_tokens")
    return format_tokens(int(total)) if isinstance(total, int) else "-"


def _cost(task: Task) -> str:
    usage = task.usage or {}
    total = usage.get("cost_total")
    return format_cost(float(total)) if isinstance(total, (int, float)) else "-"


def write_board(data: BoardData, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_board(data), encoding="utf-8")
    return target
