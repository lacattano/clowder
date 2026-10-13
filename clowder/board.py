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
from .state import (
    CLOSED,
    FAILED,
    OWNER_GATE_WORDS,
    REPORTED,
    Job,
    Queued,
    Task,
    step_in_flight,
)
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
.column.done details > summary .colhead::before { content: "\25b8 "; }
.column.done details[open] > summary .colhead::before { content: "\25be "; }
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


def _e(value: object) -> str:
    """Every piece of text on this page came from a file or an agent. Escape it."""
    return escape(str(value if value is not None else ""), quote=True)


def now_stamp() -> str:
    """A readable local time for the page header."""
    return datetime.now().strftime("%Y-%m-%d %H:%M")


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
                    f"{_full(task.owner_item or '')} "
                    f"<span class='why'>your move, [{_e(task.id)}]</span>",
                )
            )
    for task in data.tasks:
        if task.open_decision and task.decision_is_open and task.status != CLOSED:
            questions.append(
                (
                    task.created_at,
                    f"{_full(task.open_decision)} "
                    f"<span class='why'>decision, [{_e(task.id)}] asked by "
                    f"{_e(task.agent)} about {_full(task.question)}</span>",
                )
            )
    ordered = sorted(questions, key=lambda item: item[0])
    for number, (_, line) in enumerate(ordered, start=1):
        lines.append(f"<b>{number}.</b> {line}")

    jobs_by_id = {job.id: job for job in data.jobs}
    for task in data.tasks:
        if task.id in data.stranded:
            job = jobs_by_id.get(task.job or "")
            belongs = (
                f"the change {_e(job.name_in_words)} [{_e(job.id)}]"
                if job is not None
                else f"step {_e(task.id)}"
            )
            keep = f'git -C "{_e(task.worktree)}" branch keep-{_e(task.id)} {_e(task.commit)}'
            lines.append(
                f"<b>work at risk</b> <span class='id'>{_e(task.id)}</span> "
                f"{belongs}: commit {_e(task.commit)} is genuinely stranded. "
                f"<span class='why'>it is on no branch, and no later commit "
                f"supersedes it. It lives only in {_e(task.worktree)}, so reusing "
                f"that space would lose it. To keep it, run "
                f"<code>{keep}</code></span>"
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
        tip = data.rereview.get(job.id)
        if tip:
            checked = (job.review_commit or "")[:7]
            lines.append(
                f"<b>needs re-review</b> {_e(job.name_in_words)} was checked at "
                f"{_e(checked)} but has moved to {_e(tip[:7])}. "
                f"<span class='why'>[{_e(job.id)}] {_e(job.effect_in_words)}; the "
                "reviewer must check the new commit before you walk it.</span>"
            )
            continue
        if job.id in data.review_unconfirmed:
            checked = (job.review_commit or "")[:7]
            lines.append(
                f"<b>review unconfirmed</b> {_e(job.name_in_words)} was checked at "
                f"{_e(checked)}, but its current commit could not be read. "
                f"<span class='why'>[{_e(job.id)}] {_e(job.effect_in_words)}; confirm "
                "the branch by hand before you walk it.</span>"
            )
            continue
        seconds = elapsed_seconds(job.handed_over_at, None)
        quiet = " - gone quiet" if seconds >= HELD_QUIET_AFTER_SECONDS else ""
        lines.append(
            f"<b>held for your review</b> {_e(job.name_in_words)} was handed to "
            f"{_e(job.reviewer)} "
            f"<span class='why'>[{_e(job.id)}] {_e(job.effect_in_words)}; waiting "
            f"{_e(human_age(seconds))}{quiet}. Walk it in the reviewer's space.</span>"
        )

    for job in data.jobs:
        if not job.is_open:
            continue
        open_steps = [task for task in data.tasks if task.job == job.id and task.is_open]
        if not open_steps and job.has_pass and not job.has_merge_word:
            reviewed_by = f" was reviewed by {_e(job.reviewer)}" if job.reviewer else ""
            lines.append(
                f"<b>your word</b> {_e(job.name_in_words)}{reviewed_by} "
                f"<span class='why'>[{_e(job.id)}] {_e(job.effect_in_words)}; "
                "the front door merges it once you give your merge word and the "
                "checks are green</span>"
            )

    return lines


def waiting_for_worker(data: BoardData) -> list[str]:
    """Warnings for the front door: a step gone quiet, or a commit on no branch.

    The queued pile is not here any more - it is the To Do column, and the live
    status of a running step is on its In Progress card. What stays is the warning
    a card cannot carry: an agent that vanished, and a commit whose change cannot
    be judged. None of these needs the owner.
    """
    lines: list[str] = []

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
    if task.status == REPORTED:
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

    kanban_html = _kanban(data)

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
            + "<p class='why'>These narrow the cards below, on the page, where you "
            "already are. What waits on you is above them and is not narrowed.</p>",
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
