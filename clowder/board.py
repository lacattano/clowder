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

from . import owner as owner_mod
from .box import COST_NONE, cost_of
from .defects import Defect, short_date
from .report import format_cost, format_tokens
from .state import (
    FAILED,
    OWNER_GATE_WORDS,
    REPORTED,
    Job,
    Queued,
    Task,
    step_in_flight,
)
from .timeutil import elapsed_seconds, human_age, human_duration, to_epoch

# An open step that has recorded nothing for this long is worth a flag when the roster
# was not read live. Sixty minutes: this crew's steps run 10 to 40 minutes routinely
# (2026-10-26: 5, 7, 20, 38, 57 and 73 minutes; the review step itself took 35), so a
# two-minute flag fired on every healthy step and was a flag nobody would read by the
# second day. It sits above any healthy step and below the 24-hour held-change figure
# below, which is the other quiet bound in this file.
QUIET_AFTER_SECONDS = 3600.0

TITLE = "clowder board"

# An open tab follows along on its own: no server, no network. The one script is
# the page's own filter loop, inline; nothing is fetched.
REFRESH_SECONDS = 30
# How many landed changes the "Changed" section prints. His complaint was a wall of
# text, and the first draw of an old record can hold months of merges; the newest few
# are what he can act on, and the count is named so the cut is never hidden.
CHANGED_CAP = 12

STYLE = """
:root {
  --ink: #1b1b1f; --muted: #6b6b76; --line: #e2e2e8; --bg: #fbfbfd;
  --card: #ffffff; --accent: #2f6f4f; --warn: #8a5300; --warnbg: #fff8e8;
  --danger: #b3261e; --todo: #2f6f4f; --doing: #8a5300; --done: #4a4a8a;
}
@media (prefers-color-scheme: dark) {
  :root {
    --ink: #e8e8ee; --muted: #9a9aa6; --line: #2d2d36; --bg: #131317;
    --card: #1b1b21; --accent: #7fc39b; --warn: #e2b463; --warnbg: #2a2317;
    --danger: #f28b82; --todo: #7fc39b; --doing: #e2b463; --done: #a6a6e0;
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
/* The heading of a question, then the body under it. The item is one line by rule,
   so it wraps as a heading; the detail keeps its own line breaks and is never cut. */
.owner-title { font-weight: 600; overflow-wrap: anywhere; }
.owner-detail {
  white-space: pre-wrap; overflow-wrap: anywhere; margin: .35rem 0 0;
  color: var(--ink); font-size: .92rem;
}
/* The exact word to type, and how long a question has waited. */
.type {
  display: inline-block; margin-left: .35rem; padding: .05rem .45rem;
  border: 1px solid var(--accent); border-radius: 999px; color: var(--accent);
  font-size: .78rem; font-family: Consolas, "Cascadia Mono", monospace;
}
.age { color: var(--muted); font-size: .85rem; margin-left: .4rem; }
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
details.more { margin: 0; }
details.more summary { cursor: pointer; color: var(--muted); }
details.more[open] summary { color: var(--ink); }
details.more .full { margin-top: .25rem; }
/* The three columns, the same filterable cards the rest of the page uses. */
.kanban {
  display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 1rem;
  margin: 0 0 1rem; align-items: start;
}
.column { margin: 0; }
.column.todo h2 { color: var(--todo); }
.column.doing h2 { color: var(--doing); }
.column.done summary .colhead { color: var(--done); }
.colhead {
  font-size: 1rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: .06em; color: var(--muted);
}
.column.done details > summary { list-style: none; cursor: pointer; }
.column.done details > summary::-webkit-details-marker { display: none; }
.column.done details > summary::marker { content: ""; }
.cards { border-collapse: separate; border-spacing: 0; }
.cards td.card-cell { padding: 0; border: 0; }
.card {
  border: 1px solid var(--line); border-radius: 8px; padding: .5rem .6rem;
  margin: 0 0 .5rem; background: var(--card);
}
.card-pills { display: flex; flex-wrap: wrap; gap: .25rem; margin-bottom: .3rem; }
.card-title { font-weight: 600; }
.card-line {
  display: flex; flex-wrap: wrap; gap: .4rem; align-items: baseline; margin-top: .2rem;
}
.card .why { margin-top: .25rem; }
.pill.sev-blocker { color: var(--danger); border-color: var(--danger); }
.pill.sev-high { color: var(--warn); border-color: var(--warn); }
.pill.sev-normal { color: var(--muted); }
.pill.sev-low { color: var(--muted); opacity: .7; }
.pill.phase-pre-launch { color: var(--accent); border-color: var(--accent); }
.pill.phase-post-launch { color: var(--muted); }
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

  function filtering() {
    for (var i = 0; i < controls.length; i++) {
      if ((controls[i].value || '').toLowerCase()) { return true; }
    }
    return false;
  }
  function newestFirst(body) {
    var wanted = body.getAttribute('data-order');
    if (wanted) { return wanted === 'newest'; }
    return sort && sort.value === 'newest';
  }
  function capOf(body) {
    var value = Number(body.getAttribute('data-cap') || '0');
    return value > 0 ? value : 0;
  }

  function apply() {
    // A filter is never capped: it reaches every card, including the rest of Done.
    var narrowed = filtering();
    for (var b = 0; b < bodies.length; b++) {
      var body = bodies[b];
      var flip = newestFirst(body);
      var rows = body.querySelectorAll('tr');
      var ordered = [];
      for (var k = 0; k < rows.length; k++) { ordered.push(rows[k]); }
      ordered.sort(function (a, b) {
        var one = Date.parse(cell(a, 'created')) || 0;
        var two = Date.parse(cell(b, 'created')) || 0;
        if (one === two) { return 0; }
        return (one < two ? -1 : 1) * (flip ? -1 : 1);
      });
      var cap = narrowed ? 0 : capOf(body);
      var matched = 0;
      for (var j = 0; j < ordered.length; j++) {
        var row = ordered[j];
        var on = keep(row);
        var show = on && (cap === 0 || matched < cap);
        row.hidden = !show;
        if (on) { matched++; }
      }
      for (var m = 0; m < ordered.length; m++) { body.appendChild(ordered[m]); }
      var empty = document.querySelector('[data-empty][data-for="' + body.id + '"]');
      if (empty) { empty.hidden = matched !== 0; }
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
    # None means the state file does not say: the roster was not read live. The page
    # shows "-", never a guess.
    saved: bool | None = None
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
    FilterField(
        "title",
        "title",
        "text",
        note="the change in the owner's words, title or label",
    ),
    FilterField(
        "state",
        "state",
        "choice",
        # Every word a row can carry, job or step, so the control can reach each
        # one. A value a row produces but the control cannot name is a filter
        # that silently matches nothing - the defect this list exists to stop.
        (
            "in progress",
            "failed",
            "waiting on a walkthrough",
            "under review",
            "needs re-review",
            "review unconfirmed",
            "passed",
            "waiting on your merge word",
            "merged",
            "closed",
            "answered",
            "abandoned, no answer came",
            "closed, no answer came",
        ),
        "derived from the pass and merge word the owner gave, never stored",
    ),
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
    FilterField(
        "severity",
        "severity",
        "choice",
        ("blocker", "high", "normal", "low"),
        "the sev: label on a queued item or job; empty when none is set",
    ),
    FilterField(
        "phase",
        "phase",
        "choice",
        ("pre-launch", "post-launch"),
        "the phase: label on a queued item or job; empty when none is set",
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

# The two reserved label families. A label is `<family>:<value>`; the board colours
# it by family and exposes the value as its own filter column (severity, phase). One
# list, so a new family is one entry here and one column above.
LABEL_FAMILIES: dict[str, tuple[str, ...]] = {
    "sev": ("blocker", "high", "normal", "low"),
    "phase": ("pre-launch", "post-launch"),
}


def label_value(labels: Sequence[str] | None, family: str) -> str:
    """The value of a `family:` label, or empty when the record carries none."""
    prefix = f"{family}:"
    for label in labels or ():
        text = str(label)
        if text.startswith(prefix):
            return text[len(prefix) :]
    return ""


@dataclass
class BoardData:
    """Everything the page is drawn from."""

    tasks: list[Task] = field(default_factory=list)
    jobs: list[Job] = field(default_factory=list)
    queued: list[Queued] = field(default_factory=list)
    agents: list[BoardAgent] = field(default_factory=list)
    state_path: str = ""
    # `generated_at` is the local stamp a person reads; `drawn_at` is the same moment
    # as an ISO timestamp, which is what the page records for itself and what the next
    # draw compares against. Minutes are too coarse to answer "changed since".
    generated_at: str = ""
    drawn_at: str = ""
    # When the previous version of this page was drawn (ISO), or None for the first
    # draw. Read from the file being replaced, so the page can say what changed since
    # it was last drawn without keeping a second record.
    previous_draw: str | None = None
    # The newest moment any record was touched, as a display stamp. This is the honest
    # clock for "nothing has changed since": a page redrawn at 13:10 with no new record
    # says 12:40, not 13:10.
    last_change: str | None = None
    live_ok: bool = True
    note: str | None = None
    # The front door's own name, for text that means it and not the owner.
    front_door_name: str | None = None
    # Task ids whose commit is on no branch, so the work exists only in one folder.
    stranded: set[str] = field(default_factory=set)
    # Task ids whose commit is on no branch and whose content could not be judged,
    # keyed to why: `merge`, `empty`, or `unknown` (a real read failure).
    risk_notes: dict[str, str] = field(default_factory=dict)
    # Job id -> the commit its branch or held ref points at now, for a held change
    # whose tip moved after the reviewer checked it. The owner needs a re-review,
    # not an invitation to walk the stale commit.
    rereview: dict[str, str] = field(default_factory=dict)
    # Job ids whose reviewed commit cannot be confirmed: no ref could be read. They
    # must not be shown as held, which is the stale-walk claim this exists to stop.
    review_unconfirmed: set[str] = field(default_factory=set)
    # Queue items a command dropped, oldest first, read from the audit file. This is
    # the page's history of leaving the queue: what was dropped, why, and by whom.
    dropped: list[dict[str, object]] = field(default_factory=list)
    # Defects seen twice or more with no open job. They wait for the front door, not
    # for the owner: opening the job is its act. One line and an id; never restated.
    repeated_defects: list[Defect] = field(default_factory=list)
    # Why the defects record could not be read, when it could not. The page still
    # draws: one malformed file must not take away what tells the owner what is
    # happening. The line in the worker section names the file and the fault.
    defects_note: str | None = None
    # What the box has left, in one line, with what a suite and a model step would do
    # right now. Filled by whoever builds this data; an empty page draws without it
    # rather than reading a machine from inside a render.
    capacity: str | None = None
    # The catch-up, in his words: one line per thing he has not read yet, and the
    # sentence over them. The builder fills it from the record; drawing never marks a
    # line read - only `clowder catchup` does that.
    catchup: list[str] = field(default_factory=list)
    catchup_note: str | None = None


def _e(value: object) -> str:
    """Every piece of text on this page came from a file or an agent. Escape it."""
    return escape(str(value if value is not None else ""), quote=True)


def now_stamp() -> str:
    """A readable local time for the page header."""
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def stamp_of(moment: datetime) -> str:
    """A recorded moment as the local stamp the page shows."""
    local = moment.astimezone() if moment.tzinfo else moment
    return local.strftime("%Y-%m-%d %H:%M")


def _flat(text: str) -> str:
    """One line: the whitespace runs in recorded text collapsed."""
    return " ".join(text.split())


def _clip(text: str, limit: int = 160) -> str:
    flat = _flat(text)
    return flat if len(flat) <= limit else flat[: limit - 1] + "..."


def _full(text: str) -> str:
    """The whole text, escaped and flattened. Use where every word matters."""
    return _e(_flat(text))


def _more(text: str, limit: int = 160) -> str:
    """The whole text, escaped, folded behind a click when it is long.

    A cell keeps a short preview and opens the rest on click. `<details>` is plain
    HTML, so the page stays one self-contained file with no extra script. Nothing
    written by an agent or the owner is dropped, only folded.
    """
    flat = _flat(text)
    if len(flat) <= limit:
        return _e(flat)
    return (
        f"<details class='more'><summary>{_e(_clip(flat, limit))}</summary>"
        f"<div class='full'>{_e(flat)}</div></details>"
    )


def open_tasks(tasks: Sequence[Task]) -> list[Task]:
    return [task for task in tasks if task.is_open]


def recent_answers(tasks: Sequence[Task], limit: int = 12) -> list[Task]:
    answered = [task for task in tasks if task.answer]
    return list(reversed(answered))[:limit]


def dropped_items(data: BoardData) -> list[dict[str, object]]:
    """The dropped queue items, newest first. A view, so a missing field is blank."""
    return list(reversed(data.dropped))


def _drop_when(record: dict[str, object]) -> float:
    at = record.get("at")
    return elapsed_seconds(str(at), None) if at else 0.0


def model_rollup(data: BoardData) -> list[tuple[str, int, int]]:
    """Per model: changes passed first time, and changes the verifier sent back.

    A change is counted under the model recorded on the writer's last reported step -
    the one that finished it. "Sent back" is the recorded trace of it: the job's
    reviewer ran more than one review step on it, which is what the walkthrough chain
    does when he asks for a change and the walkthrough happens again. A change still
    waiting for his pass is counted in neither column: it has no verdict yet.

    This is a figure to read, never a gate.
    """
    steps_by_job: dict[str, list[Task]] = {}
    for task in data.tasks:
        if task.job:
            steps_by_job.setdefault(task.job, []).append(task)

    counts: dict[str, list[int]] = {}
    for job in data.jobs:
        steps = steps_by_job.get(job.id, [])
        review_steps = sum(1 for task in steps if job.reviewer and task.agent == job.reviewer)
        sent_back = review_steps > 1
        if not sent_back and not job.has_pass:
            continue
        writer_steps = [task for task in steps if task.agent == job.agent and task.model]
        if writer_steps:
            finished = max(
                writer_steps,
                key=lambda task: task.reported_at or task.dispatched_at or "",
            )
            model = finished.model or "not recorded"
        else:
            model = "not recorded"
        row = counts.setdefault(model, [0, 0])
        row[1 if sent_back else 0] += 1
    return sorted((model, values[0], values[1]) for model, values in counts.items())


def front_door_label(data: BoardData) -> str:
    """The front door's own name, or a plain phrase when none is configured."""
    return data.front_door_name or "the front door"


def waiting_on_you(data: BoardData) -> list[str]:
    """Only what the owner must do, from the one list both views render.

    The lines come from `owner.waiting_on_owner`, the same function `clowder start` uses,
    so the section and the count cannot disagree with it. One line each: the number he
    replies with, how long it has waited, the thing in his words, and the word that
    answers it. No task or job id: the number is this page's own.
    """
    his, _ = owner_mod.waiting_on_owner(
        data.tasks,
        data.jobs,
        rereview=data.rereview,
        review_unconfirmed=data.review_unconfirmed,
    )
    lines: list[str] = []
    for number, item in enumerate(his, start=1):
        detail = f"<div class='owner-detail'>{_e(item.detail)}</div>" if item.detail else ""
        lines.append(
            f"<b>{number}.</b> <span class='owner-title'>{_e(item.heading)}</span> "
            f"<span class='type'>{_e(owner_mod.answered_by(item, number))}</span>"
            f"<span class='age'>waited {_e(item.waited)}</span> "
            f"<span class='why'>{_e(item.note)}</span>{detail}"
        )
    return lines


def waiting_for_worker(data: BoardData) -> list[str]:
    """Warnings for the front door: a defect that needs a job, a step gone quiet,
    or a commit on no branch.

    The queued pile is not here any more - it is the To Do column, and the live
    status of a running step is on its In Progress card. What stays is the warning
    a card cannot carry: an agent that vanished, a commit whose change cannot be
    judged, and a defect seen twice with no job. None of these needs the owner.
    """
    lines: list[str] = []

    # What he may ask about but does not have to move, and a commit on no branch.
    # Both are the front door's act, so they are here, not in his queue: "not yours"
    # is a different claim from "waiting on you", and the count must not confuse them.
    _, not_his = owner_mod.waiting_on_owner(
        data.tasks,
        data.jobs,
        rereview=data.rereview,
        review_unconfirmed=data.review_unconfirmed,
    )
    for item in not_his:
        lines.append(
            f"<b>not yours</b> {_e(item.heading)} "
            f"<span class='why'>{_e(item.note)}; waiting {_e(item.waited)}</span>"
        )
    jobs_by_id = {job.id: job for job in data.jobs}
    for task in data.tasks:
        if task.id not in data.stranded:
            continue
        job = jobs_by_id.get(task.job or "")
        belongs = (
            f"the change {_e(job.name_in_words)}"
            if job is not None
            else "a step with no change"
        )
        keep = f'git -C "{_e(task.worktree)}" branch keep-{_e(task.id)} {_e(task.commit)}'
        lines.append(
            f"<b>work at risk</b> {belongs} <span class='id'>{_e(task.id)}</span> "
            f"<span class='why'>commit {_e((task.commit or '')[:7])} is on no branch, and "
            f"no later commit supersedes it. It lives only in {_e(task.worktree)}. The "
            f"front door keeps it with <code>{keep}</code>.</span>"
        )

    if data.defects_note:
        lines.append(
            "<b>defects record unreadable</b> "
            f"<span class='why'>{_e(data.defects_note)}. The repeated list is not "
            "shown until it is fixed; `clowder defects list` prints the error, and the "
            "rest of this page is drawn from everything else.</span>"
        )

    # A second sighting of a defect is not cleared by its count going up. Opening the
    # job is the front door's act, so this list is here and not in the owner's
    # section. One line and an id: the detail stays in `clowder defects show`.
    for defect in data.repeated_defects:
        lines.append(
            f"<b>seen twice, no job</b> <span class='id'>{_e(defect.id)}</span> "
            f"{_e(defect.line)} "
            f"<span class='why'>seen {_e(str(defect.count))} times, last "
            f"{_e(short_date(defect.last_seen))}. A second sighting opens a job or "
            "records an accepted workaround with the reason; bumping the count is "
            "not enough.</span>"
        )

    for task in open_tasks(data.tasks):
        if task.age_seconds < QUIET_AFTER_SECONDS:
            continue
        if not data.live_ok:
            # No live read, so a quiet step is judged on the record's own age: this is
            # the only evidence there is, and it is enough to say "nothing recorded
            # since". It is a different claim from "the worker is idle", and it is the
            # one the page can stand behind.
            lines.append(
                f"<b>not checked live</b> <span class='id'>{_e(task.id)}</span> "
                f"{_e(task.agent)} shipped nothing to the record for "
                f"{_e(human_age(task.age_seconds))}; confirm the worker is still on it "
                f"<span class='why'>{_more(task.question, 120)}</span>"
            )
            continue
        agent = next((a for a in data.agents if a.name == task.agent), None)
        if agent is None:
            # Only an answer when the live list was actually readable. Otherwise the
            # page would cry wolf every time the multiplexer was down.
            if data.live_ok:
                lines.append(
                    f"<b>gone quiet</b> <span class='id'>{_e(task.id)}</span> "
                    f"{_e(task.agent)} is not in the live agent list at all "
                    f"<span class='why'>{_more(task.question, 120)}</span>"
                )
            continue
        if (agent.status or "idle") != "working":
            lines.append(
                f"<b>no answer</b> <span class='id'>{_e(task.id)}</span> "
                f"{_e(task.agent)} is {_e(agent.status or 'idle')} and has said nothing "
                f"for {_e(human_age(task.age_seconds))} "
                f"<span class='why'>{_more(task.question, 120)}</span>"
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


def running_now(data: BoardData) -> list[str]:
    """What each worker is on, how long, and what the step costs the box.

    Without a live read this is "open steps", not "busy workers": the record cannot say
    whether the pane is working, so a step that has recorded nothing for a while is
    marked. The owner must be able to tell a running step from one that was dispatched
    hours ago, and this page must not call the second one running in silence.

    The class is shown rather than "heavy" because it is the thing the gate compares:
    `read` says why nothing was refused, and `model` says why a second step waits.
    """
    lines: list[str] = []
    for task in sorted(
        open_tasks(data.tasks), key=lambda task: task.dispatched_at or task.created_at
    ):
        cost = cost_of(task)
        klass = "" if cost == COST_NONE else f" <span class='pill'>{_e(cost)}</span>"
        quiet = ""
        if not data.live_ok and task.age_seconds >= QUIET_AFTER_SECONDS:
            quiet = (
                f" <span class='pill bad'>nothing recorded for "
                f"{_e(human_age(task.age_seconds))}</span>"
            )
        lines.append(
            f"<b>{_e(task.agent)}</b> on {_full(task.question)}{klass}{quiet} "
            f"<span class='age'>{_e(human_age(task.age_seconds))}</span>"
        )
    return lines


def worker_queue(data: BoardData) -> list[str]:
    """The front door's queue, oldest first: decided, not sent."""
    lines: list[str] = []
    for item in sorted(data.queued, key=lambda item: item.created_at):
        who = item.agent or item.role or "unassigned"
        lines.append(
            f"<b>{_e(who)}</b> - {_full(item.question or item.brief)} "
            f"<span class='why'>waiting {_e(human_age(item.age_seconds))}; "
            f"why: {_e(item.why)}</span>"
        )
    return lines


def changed_for_owner(data: BoardData, *, since: float | None) -> list[str]:
    """What changed for him, one sentence each, newest first.

    Only work that landed, in his words: the change's effect line, or its title. No
    id, no state word, no branch - this is the section he reads without the crew's
    dialect.
    """
    entries: list[tuple[float, str]] = []
    for job in data.jobs:
        moment = to_epoch(job.merged_at)
        if moment is None or (since is not None and moment <= since):
            continue
        what = (job.effect or "").strip() or job.name_in_words
        entries.append((moment, f"Now live: {_e(what)}"))
    return [line for _, line in sorted(entries, key=lambda pair: pair[0], reverse=True)]


def _display_stamp(value: str | None) -> str:
    """A recorded moment as the stamp a person reads, whatever shape it arrived in."""
    if not value:
        return "the first draw"
    try:
        return stamp_of(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return value


def _field_names() -> tuple[str, ...]:
    return tuple(field.name for field in FILTERABLE_FIELDS)


def _row_attrs(values: dict[str, str]) -> str:
    """Every filterable field on a row, generated from FILTERABLE_FIELDS.

    One source of truth: the controls read these names, the controls are generated from
    the same list, and a filter cannot name a field no row carries.
    """
    return "".join(f' data-{name}="{_e(values.get(name, ""))}"' for name in _field_names())


def _state_of(data: BoardData, job: Job) -> str:
    """The job's state word, with the re-review states from the tip check.

    One source for both the pill the owner reads and the row's data-state, so the
    state filter can never name a value the row does not carry. While a step on the
    job is still running, an owner-facing gate word is premature: the reviewer or
    writer has not finished, so the row says "under review" instead.
    """
    state = job.state_in_words_at(
        data.rereview.get(job.id),
        unreadable=job.id in data.review_unconfirmed,
    )
    if state in OWNER_GATE_WORDS and step_in_flight(job, data.tasks):
        return "under review"
    return state


def _job_fields(job: Job, state: str) -> dict[str, str]:
    """A job's filterable values, each read from a field the job records."""
    updated = _newest(job.merged_at, job.closed_at, job.published_at, job.released_at)
    waiting = not job.has_pass or not job.has_merge_word
    return {
        "repo": job.repo,
        "title": job.name_in_words,
        "state": state,
        # A job records no shape; its steps do. Empty until a job records one.
        "kind": getattr(job, "shape", "") or "",
        "status": job.status,
        "agent": job.agent,
        "job": job.id,
        "waiting-on-you": "yes" if waiting else "no",
        "blocked": getattr(job, "blocked_by", "") or "",
        "severity": label_value(job.labels, "sev"),
        "phase": label_value(job.labels, "phase"),
        "created": job.created_at or "",
        "updated": updated,
        "age": str(int(job.age_seconds)),
    }


def _step_state_in_words(task: Task) -> str:
    """Where a step is, in words, derived from what it holds. Never stored."""
    if task.status == REPORTED or (task.is_closed and task.answer):
        # A step that reported and closed itself on that report still answered. The
        # answer is on the record, so it is not "closed, no answer came".
        return "answered"
    if task.status == FAILED:
        # A dispatch whose send failed. It stopped, so it is never called in
        # progress; it never reported, so it is not answered either.
        return "failed"
    if task.is_abandoned:
        return "abandoned, no answer came"
    if task.is_closed:
        return "closed, no answer came"
    return "in progress"


def _job_by_id(data: BoardData) -> dict[str, Job]:
    return {job.id: job for job in data.jobs}


def _task_fields(task: Task, jobs: dict[str, Job] | None = None) -> dict[str, str]:
    """A step's filterable values, each read from a field the step records.

    A step has no title of its own: it inherits the words from the job it is a
    step of, and falls back to its own branch when it has no job.
    """
    updated = _newest(task.reported_at, task.abandoned_at, task.closed_at)
    waiting = bool(task.owner_item) or bool(task.decision_is_open)
    job = (jobs or {}).get(task.job or "")
    title = job.name_in_words if job else (task.question or task.branch or "")
    state = job.state_in_words if job else _step_state_in_words(task)
    return {
        "repo": task.repo,
        "title": title,
        "state": state,
        "kind": task.shape,
        "status": task.status,
        "agent": task.agent,
        "job": task.job or "",
        "waiting-on-you": "yes" if waiting else "no",
        "blocked": getattr(task, "blocked_by", "") or "",
        # A step carries no labels yet; its job's labels are on the job's card.
        "severity": "",
        "phase": "",
        "created": task.dispatched_at or task.created_at or "",
        "updated": updated,
        "age": str(int(task.age_seconds)),
    }


def _queued_fields(item: Queued) -> dict[str, str]:
    """A queued item's filterable values, each read from a field it records."""
    return {
        "repo": item.repo,
        "title": item.question or item.brief,
        "state": "queued",
        "kind": item.shape,
        "status": "queued",
        "agent": item.target,
        "job": item.job or "",
        "waiting-on-you": "no",
        "blocked": "",
        "severity": label_value(item.labels, "sev"),
        "phase": label_value(item.labels, "phase"),
        "created": item.created_at or "",
        "updated": item.created_at or "",
        "age": str(int(item.age_seconds)),
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


def _card_table(
    table_id: str,
    rows: Sequence[tuple[str, dict[str, str]]],
    *,
    extra: str = "",
) -> str:
    """One column of cards: one row per card, each carrying the filterable fields.

    The row is a table row, so the page's own filter script reaches the cards with
    no change to the filter loop. `extra` carries the Done column's cap and its
    newest-first order.
    """
    body = "".join(
        f"<tr{_row_attrs(fields)}><td class='card-cell'>{card}</td></tr>"
        for card, fields in rows
    )
    empty = (
        f"<p class='empty' data-empty data-for=\"{_e(table_id)}\" hidden>"
        "Nothing matches those filters.</p>"
    )
    return (
        "<table class='cards'><tbody "
        f'id="{_e(table_id)}" data-rows{extra}>{body}</tbody></table>{empty}'
    )


def _id(value: str | None) -> str:
    return f"<span class='id'>{_e(value or '-')}</span>"


def _section(title: str, count: int | None, inner: str, css: str = "") -> str:
    suffix = f" <span class='count'>{count}</span>" if count is not None else ""
    return f"<section class='{css}'><h2>{_e(title)}{suffix}</h2>{inner}</section>"


def _nothing(what: str) -> str:
    return f"<p class='nothing'>{_e(what)}</p>"


DONE_CAP = 20


def _label_pills(labels: Sequence[str] | None) -> str:
    """A pill per label, coloured by its reserved family; other labels plain."""
    pills: list[str] = []
    for raw in labels or ():
        text = str(raw)
        family, _, value = text.partition(":")
        css = f" {family} {family}-{value}" if family in LABEL_FAMILIES and value else ""
        pills.append(f"<span class='pill{css}'>{_e(text)}</span>")
    return "".join(pills)


def _card(
    title: str,
    ident: str,
    state: str,
    reason: str,
    *,
    repo: str = "",
    kind: str = "",
    labels: Sequence[str] | None = None,
    details: str = "",
) -> str:
    """One card: badges, the title in his words, the id, the state, the reason.

    No branch, commit or agent handle on the face - those go in `details`, folded,
    because the owner cannot read a handle and does not need one.
    """
    pills: list[str] = []
    if repo:
        pills.append(f"<span class='pill'>{_e(repo)}</span>")
    if kind:
        pills.append(f"<span class='pill'>{_e(kind)}</span>")
    pills.append(_label_pills(labels))
    reason_html = f"<span class='why'>{_e(reason)}</span>" if reason else ""
    return (
        "<div class='card'>"
        f"<div class='card-pills'>{''.join(pills)}</div>"
        f"<div class='card-title'>{_e(title)}</div>"
        f"<div class='card-line'><span class='id'>{_e(ident)}</span>"
        f"<span class='pill'>{_e(state)}</span></div>"
        f"{reason_html}{details}"
        "</div>"
    )


def _agent_reason(data: BoardData, name: str, reviewer: str | None = None) -> str:
    """Why a card waits: the live agent and its status, or the reviewer holding it."""
    if reviewer:
        return f"{reviewer} is checking it"
    agent = next((item for item in data.agents if item.name == name), None)
    if agent is None:
        if data.live_ok:
            return f"{name} is not in the live agent list"
        return f"{name} is not running"
    if agent.status is None:
        # The roster was not read live, so its state is unknown. Saying "idle" here
        # would be a guess, and the page must not guess.
        return f"{name} - its state was not read live"
    return f"{name} is {agent.status or 'idle'}"


def _job_details(job: Job) -> str:
    """The handles kept off the card face, folded so they are never lost."""
    bits: list[str] = []
    if job.effect:
        bits.append(f"effect {job.effect}")
    bits.append(f"branch {job.branch}")
    if job.commit:
        bits.append(f"commit {job.commit}")
    if job.reviewer:
        bits.append(f"reviewer {job.reviewer}")
    bits.append(f"pass {'yes' if job.has_pass else 'no'}")
    bits.append(f"word {'yes' if job.has_merge_word else 'no'}")
    if job.parked_at:
        bits.append(f"parked at {(job.park_commit or job.commit or '')[:7]}")
    return (
        "<details class='more'><summary>details</summary>"
        f"<div class='full'>{_e(' | '.join(bits))}</div></details>"
    )


def _queued_card(item: Queued) -> tuple[str, dict[str, str]]:
    card = _card(
        _clip(item.question or item.brief, 160),
        item.id,
        "queued",
        item.why,
        repo=item.repo,
        kind=item.shape,
        labels=item.labels,
    )
    return card, _queued_fields(item)


def _job_card(data: BoardData, job: Job, state: str, shape: str) -> tuple[str, dict[str, str]]:
    holding = state in ("waiting on a walkthrough", "needs re-review", "review unconfirmed")
    reviewer = job.reviewer if holding else None
    reason = _agent_reason(data, job.agent, reviewer) if job.is_open else ""
    card = _card(
        _clip(job.name_in_words, 160),
        job.id,
        state,
        reason,
        repo=job.repo,
        kind=shape,
        labels=job.labels,
        details=_job_details(job),
    )
    return card, _job_fields(job, state)


def _step_card(data: BoardData, task: Task, state: str) -> tuple[str, dict[str, str]]:
    reason = _agent_reason(data, task.agent) if task.is_open else ""
    card = _card(
        _clip(task.question, 160),
        task.id,
        state,
        reason,
        repo=task.repo,
        kind=task.shape,
    )
    return card, _task_fields(task)


def _column(
    css: str,
    heading: str,
    count: int,
    table_id: str,
    rows: Sequence[tuple[str, dict[str, str]]],
    *,
    cap: int = 0,
    note: str = "",
    abandoned: int = 0,
) -> str:
    if cap:
        inner = _card_table(table_id, rows, extra=f' data-cap="{cap}" data-order="newest"')
        if note:
            inner = f"<p class='why'>{_e(note)}</p>{inner}"
        # The abandoned count rides in the collapsed header, so work that stopped
        # without an answer cannot sit unseen behind the collapse.
        count_text = f"({count}, {abandoned} abandoned)" if abandoned else f"({count})"
        return (
            f"<section class='column {css}'><details><summary>"
            f"<span class='colhead'>{_e(heading)}</span> "
            f"<span class='count'>{_e(count_text)}</span></summary>{inner}</details></section>"
        )
    return (
        f"<section class='column {css}'><h2>{_e(heading)} "
        f"<span class='count'>{count}</span></h2>{_card_table(table_id, rows)}</section>"
    )


def _kanban(data: BoardData) -> str:
    """To Do, In Progress and Done, each the same filterable card table.

    A job and its steps are one card: a step of a job is folded into that job's
    card. A step with no job is its own card.
    """
    jobs_by_id = _job_by_id(data)
    shapes: dict[str, str] = {}
    for task in data.tasks:
        if task.job and task.shape and task.job not in shapes:
            shapes[task.job] = task.shape

    todo = [_queued_card(item) for item in data.queued]
    doing: list[tuple[str, dict[str, str]]] = []
    done: list[tuple[str, dict[str, str]]] = []
    for job in data.jobs:
        state = _state_of(data, job)
        card = _job_card(data, job, state, shapes.get(job.id, ""))
        (doing if job.is_open else done).append(card)
    for task in data.tasks:
        if task.job and task.job in jobs_by_id:
            continue  # folded into its job's card
        state = _step_state_in_words(task)
        card = _step_card(data, task, state)
        if task.is_open:
            doing.append(card)
        else:
            done.append(card)
    done.sort(key=lambda pair: pair[1]["created"], reverse=True)
    abandoned = sum(1 for _, fields in done if fields["state"] == "abandoned, no answer came")
    return (
        "<div class='kanban'>"
        + _column("todo", "To Do", len(todo), "rows-todo", todo)
        + _column("doing", "In Progress", len(doing), "rows-doing", doing)
        + _column(
            "done",
            "Done",
            len(done),
            "rows-done",
            done,
            cap=DONE_CAP,
            note="Showing the newest 20; the filter reaches the rest.",
            abandoned=abandoned,
        )
        + "</div>"
    )


def render_board(data: BoardData) -> str:
    """The whole page, as one self-contained file.

    The catch-up comes first, in his words: what he has not read yet, then the four
    record sections in the order he reads them, then the records, unchanged. The
    catch-up section is the same text `clowder catchup` prints, so the page and the
    command cannot say different things.
    """
    if data.catchup:
        catch_html = "<ul>" + "".join(f"<li>{_e(line)}</li>" for line in data.catchup) + "</ul>"
    else:
        catch_html = _nothing(
            "Nothing new since your last read. The full standing picture is one "
            "command: clowder catchup --all"
        )
    if data.catchup_note:
        catch_html += f"<p class='why'>{_e(data.catchup_note)}</p>"
    for_you = waiting_on_you(data)
    running = running_now(data)
    queue = worker_queue(data)
    worker = waiting_for_worker(data)
    since = to_epoch(data.previous_draw)
    changed = changed_for_owner(data, since=since)
    in_flight = bool(open_tasks(data.tasks))

    if for_you:
        needs_html = "<ul>" + "".join(f"<li>{line}</li>" for line in for_you) + "</ul>"
    else:
        needs_html = _nothing(
            "Nothing is recorded as waiting on you. The tool shows only what a command "
            "recorded; a report the front door holds in its words will not appear here."
        )

    idle_line = "No step is open. This page does not reload itself when idle."
    running_html = (
        "<ul>" + "".join(f"<li>{line}</li>" for line in running) + "</ul>"
        if running
        else _nothing(idle_line)
    )
    if data.live_ok:
        running_note = (
            "A step is one worker's turn. The pill is what the step costs the box - "
            "none, read, suite, install, browser or model - and a step starts only when "
            "the box has that much free after the steps already running."
        )
    else:
        running_note = (
            "These are the open steps in the record. The live state of each worker was "
            "not read, so a step marked 'nothing recorded' has not moved for that long: "
            "it may be working, or it may have stopped."
        )
    running_html += f"<p class='why'>{running_note}</p>"
    if data.capacity:
        running_html += f"<p class='why'>{_e(data.capacity)}</p>"

    # One record, one list, one count. The queue here is the same list the To Do column
    # draws from the record, so both say the same number. Warnings are not queued items:
    # they are shown below with their own count and never added to this one. Seen
    # 2026-10-27: "38 jobs in the todo queue but 40 on the list of waiting for an agent".
    queue_html = (
        "<ul>" + "".join(f"<li>{line}</li>" for line in queue) + "</ul>"
        if queue
        else _nothing(
            f"Nothing is queued for {front_door_label(data)}."
            if worker
            else f"Nothing is waiting for {front_door_label(data)}."
        )
    )
    if worker:
        queue_html += (
            f"<p class='why'>Also for {front_door_label(data)}: {len(worker)} warning(s). "
            "These are not queued items, so none of them is in the queue count above.</p>"
            "<ul>" + "".join(f"<li>{line}</li>" for line in worker) + "</ul>"
        )

    drawn = _display_stamp(data.previous_draw) if data.previous_draw else data.generated_at
    # The section counts landed work only, and says so: a step that started is under
    # "Running now", not here. "Nothing changed" would be false while a step runs.
    if data.previous_draw and not changed:
        freshness = f"Nothing has landed since {drawn}."
    elif data.previous_draw:
        counted = "1 thing landed" if len(changed) == 1 else f"{len(changed)} things landed"
        freshness = f"{counted} since the page was last drawn at {drawn}; the list is below."
    else:
        freshness = f"This is the first draw, at {data.generated_at}."
    shown = changed[:CHANGED_CAP]
    changed_html = (
        "<ul>" + "".join(f"<li>{line}</li>" for line in shown) + "</ul>"
        if changed
        else _nothing(f"Nothing has landed since {drawn}.")
    )
    if len(changed) > len(shown):
        changed_html += (
            f"<p class='why'>Showing the newest {len(shown)} of {len(changed)}; the rest "
            "are in the record below.</p>"
        )
    changed_html = (
        "<p class='why'>The tool cannot know when you opened this page; it knows when it "
        "last drew it. This lists the work that landed since then, and nothing else: a "
        "step that started is under Running now.</p>" + changed_html
    )

    kanban_html = _kanban(data)

    agent_rows = [
        [
            _e(agent.name),
            _e(agent.pane_id or "-"),
            _e(agent.status or "-"),
            _e(agent.place_label),
            _e("yes" if agent.saved else "no") if agent.saved is not None else "-",
            _e(agent.space_label),
        ]
        for agent in data.agents
    ]
    agents_html = (
        _table(["name", "pane", "state", "space holds", "saved", "folder"], agent_rows)
        if agent_rows
        else _nothing(
            "No agents were found in the live roster."
            if data.live_ok
            else "No agents are recorded in the state file."
        )
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
            _e(task.model or "-"),
            _more(task.answer or "", 200),
        ]
        for task in recent_answers(data.tasks)
    ]
    answers_html = (
        _table(["id", "agent", "took", "tokens", "cost", "model", "answer"], answer_rows)
        if answer_rows
        else _nothing("Nothing has been answered yet.")
    )

    dropped = dropped_items(data)
    dropped_rows = [
        [
            _id(str(record.get("item") or "-")),
            _e(str(record.get("repo") or "-")),
            _e(str(record.get("by") or "unknown")),
            _e(human_age(_drop_when(record))) if record.get("at") else "-",
            _more(str(record.get("question") or record.get("brief") or ""), 120),
            _more(str(record.get("reason") or record.get("why") or ""), 160),
        ]
        for record in dropped
    ]
    dropped_html = (
        _table(["item", "repo", "by", "when", "question", "why"], dropped_rows)
        if dropped_rows
        else _nothing("No queue item has been dropped.")
    )

    rollup = model_rollup(data)
    models_html = (
        _table(
            ["model", "passed first time", "sent back"],
            [[_e(model), _e(str(passed)), _e(str(back))] for model, passed, back in rollup],
        )
        + "<p class='why'>A change is counted under the model recorded on the step that "
        "finished it. &quot;Sent back&quot; is the verifier running the review twice on one "
        "change. A change still waiting for your pass is in neither column, and nothing here "
        "gates anything.</p>"
        if rollup
        else _nothing("No step has recorded the model it ran on yet.")
    )

    filterable = bool(data.queued or data.jobs or data.tasks)
    filters_html = (
        _section(
            "Filters",
            None,
            _filter_controls()
            + "<p class='why'>These narrow the records below, on the page, where you "
            "already are. What waits on you is above them and is not narrowed.</p>",
            css="filters",
        )
        if filterable
        else ""
    )

    live = "read live" if data.live_ok else "from the state file"
    changed_at = f" | last change {_e(data.last_change)}" if data.last_change else ""
    footer = (
        f"generated {_e(data.generated_at)}{changed_at} | "
        f"state {_e(data.state_path)} | roster {live}"
    )

    # Reload only while something is in flight. The browser re-reads this file; the box
    # draws nothing until a command rewrites it, so an idle reload is pure flicker.
    refresh_meta = (
        f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">' if in_flight else ""
    )
    reload_line = (
        f"This page reloads every {REFRESH_SECONDS}s while work is running, and stops when "
        "nothing is running."
        if in_flight
        else "Nothing is running, so this page does not reload itself. It changes when a "
        "clowder command rewrites it."
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{refresh_meta}
<!-- drawn: {_e(data.drawn_at or data.generated_at)} -->
<title>{_e(TITLE)}</title>
<style>{STYLE}</style>
</head>
<body>
<main>
<h1>{_e(TITLE)}</h1>
<p class="meta">What waits on you, what is running, what is queued, and what changed.</p>
<p class="meta" id="freshness">{_e(freshness)} {_e(reload_line)}</p>
{_section("Since you last looked", len(data.catchup), catch_html, css="needs")}
{_section("For you", len(for_you), needs_html, css="needs")}
{_section("Running now", len(running), running_html)}
{_section("Waiting for a worker", len(queue), queue_html)}
{_section("What landed since this page was last drawn", len(changed), changed_html)}
{filters_html}
{kanban_html}
{_section("Agents and spaces", len(agent_rows), agents_html)}
{_section("Answers", len(answer_rows), answers_html)}
{_section("Dropped from the queue", len(dropped_rows), dropped_html)}
{_section("By model", len(rollup), models_html) if rollup else ""}
<footer>{footer}</footer>
</main>
<script>{FILTER_SCRIPT}</script>
</body>
</html>
"""


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
