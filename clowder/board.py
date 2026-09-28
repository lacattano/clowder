"""The board: a generated HTML page of what is happening.

A file, not a panel. It survives a fresh context, it holds far more than a
terminal can, and it needs no server and no network.

Everything here is a pure function of what it is handed, so it can be tested
without a browser and rendered without a multiplexer running.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from pathlib import Path

from .report import format_cost, format_tokens
from .state import CLOSED, Job, Task
from .timeutil import human_age, human_duration

# An open step whose agent is idle for longer than this is worth a second look:
# nothing is running, so nobody may be coming back to it.
QUIET_AFTER_SECONDS = 120.0

TITLE = "clowder board"

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


@dataclass
class BoardData:
    """Everything the page is drawn from."""

    tasks: list[Task] = field(default_factory=list)
    jobs: list[Job] = field(default_factory=list)
    agents: list[BoardAgent] = field(default_factory=list)
    state_path: str = ""
    generated_at: str = ""
    live_ok: bool = True
    note: str | None = None
    # Task ids whose commit is on no branch, so the work exists only in one folder.
    stranded: set[str] = field(default_factory=set)


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


def waiting_on_you(data: BoardData) -> list[str]:
    """The three things the page exists for: decisions, finished reviews, risk."""
    lines: list[str] = []

    for task in data.tasks:
        if task.open_decision and task.status != CLOSED:
            lines.append(
                f"<b>decision</b> <span class='id'>{_e(task.id)}</span> "
                f"{_e(_clip(task.open_decision, 200))} "
                f"<span class='why'>asked by {_e(task.agent)} about "
                f"{_e(_clip(task.question, 120))}</span>"
            )

    for task in data.tasks:
        if task.id in data.stranded:
            lines.append(
                f"<b>work at risk</b> <span class='id'>{_e(task.id)}</span> "
                f"commit {_e(task.commit)} is on no branch "
                f"<span class='why'>it lives only in {_e(task.worktree)}; reusing "
                "that space would lose it</span>"
            )

    for job in data.jobs:
        if not job.is_open:
            continue
        open_steps = [task for task in data.tasks if task.job == job.id and task.is_open]
        if job.reviewer and not open_steps:
            lines.append(
                f"<b>ready to merge</b> <span class='id'>{_e(job.id)}</span> "
                f"{_e(job.branch)} was reviewed by {_e(job.reviewer)} "
                f"<span class='why'>{_e(job.label)}; merging is your step, and the "
                f"tool will not do it</span>"
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

    return lines


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    head = "".join(f"<th>{_e(name)}</th>" for name in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


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
        needs_html = _nothing("Nothing is waiting on you.")

    open_rows = [
        [
            _id(task.id),
            _id(task.job),
            _e(task.agent),
            _e(task.repo),
            _e(task.branch or task.commit or "-"),
            _e(human_age(task.age_seconds)),
            _e(_clip(task.question, 120)),
            _agent_state(data, task.agent),
        ]
        for task in open_tasks(data.tasks)
    ]
    open_html = (
        _table(
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
        [
            _id(job.id),
            f"<span class='pill {'open' if job.is_open else ''}'>{_e(job.status)}</span>",
            _e(job.label),
            _e(job.branch),
            _e(job.agent),
            _e(job.reviewer or "-"),
            _e(job.commit or "-"),
            _e(human_age(job.age_seconds)),
        ]
        for job in data.jobs
    ]
    jobs_html = (
        _table(
            ["id", "state", "work", "branch", "agent", "reviewer", "commit", "age"],
            job_rows,
        )
        if job_rows
        else _nothing("No jobs yet.")
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
<title>{_e(TITLE)}</title>
<style>{STYLE}</style>
</head>
<body>
<main>
<h1>{_e(TITLE)}</h1>
<p class="meta">What is queued, what is underway, and what is waiting on you.</p>
{_section("Waiting on you", len(needs), needs_html, css="needs")}
{_section("Open steps", len(open_rows), open_html)}
{_section("Agents and spaces", len(agent_rows), agents_html)}
{_section("Answers", len(answer_rows), answers_html)}
{_section("Jobs", len(job_rows), jobs_html)}
<footer>{footer}</footer>
</main>
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
