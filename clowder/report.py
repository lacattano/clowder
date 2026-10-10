"""Composing a report.

The shape is fixed, because the shape is the point: the question first, so a
late answer still knows what it answers. Then one usage line. Then the answer.
Then at most one open decision.
"""

from __future__ import annotations

import os
from pathlib import Path

from .sessions import Usage
from .state import Job, Task
from .timeutil import human_duration

INDENT = "    "

# How much of an answer `report` prints before it points at `--full`. The answer is
# the free-form part of a report, so it is the part that can bury a reader; the
# fixed lines above it (question, usage, change) are always shown.
REPORT_LINE_LIMIT = 40


def format_tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def format_cost(amount: float) -> str:
    if amount <= 0:
        return "$0"
    if amount < 0.0001:
        return "<$0.0001"
    if amount < 1:
        return f"${amount:.4f}"
    return f"${amount:.2f}"


def _same_dir(one: str | Path, other: str | Path) -> bool:
    """The same directory, whatever case or shape the two paths are written in."""
    return os.path.normcase(os.path.abspath(str(one))) == os.path.normcase(
        os.path.abspath(str(other))
    )


def worktree_label(task: Task) -> str:
    """Where the work happened: `branch @ commit`, else a checkout, else the main one."""
    if task.branch:
        return f"{task.branch} @ {task.commit}" if task.commit else task.branch
    if not task.worktree:
        return "main checkout"
    # The recorded folder is the main checkout when it is the repo itself, and a
    # named space otherwise. A space that holds an open job must not read as the
    # user's own checkout.
    if task.repo_path and _same_dir(task.worktree, task.repo_path):
        return "main checkout"
    name = Path(str(task.worktree)).name or str(task.worktree)
    return f"worktree {name}"


def usage_line(task: Task, usage: Usage | None) -> str:
    parts = [task.agent, task.repo, worktree_label(task)]
    parts.append(human_duration(task.age_seconds))
    if usage is not None and usage.turns:
        parts.append(format_tokens(usage.total_tokens))
        parts.append(format_cost(usage.cost_total))
    else:
        parts.append("no usage yet")
    # Which model ran the step, so a free or preview model can be compared. Recorded
    # on the step when it reported; taken from the live session otherwise. Nothing
    # here gates anything - it is a figure, not a guard.
    model = task.model or (usage.model if usage is not None else None)
    if model:
        parts.append(model)
    return " | ".join(parts)


def build_report(
    task: Task,
    usage: Usage | None,
    answer: str | None,
    job: Job | None = None,
    *,
    full: bool = False,
) -> str:
    """One report block, in the fixed order.

    When the step is a step of a job, the change is named first, in words, so a
    report reads as "the page-context fix", not as a handle.

    A long answer is cut at REPORT_LINE_LIMIT lines and names `--full`; the JSON
    path never truncates, so a machine always gets the whole answer.
    """
    lines = [f"Re: {task.question}", INDENT + usage_line(task, usage)]

    if job is not None and job.title:
        lines.append(INDENT + f"change: {job.title} - {job.state_in_words}")

    if answer:
        shown, hidden = _answer_lines(answer, full)
        for line in shown:
            lines.append(INDENT + line.rstrip())
        if hidden:
            lines.append(
                INDENT + f"... {hidden} more line(s) hidden; read the whole answer with --full"
            )
    elif task.is_abandoned:
        reason = task.abandon_reason or "no reason recorded"
        lines.append(INDENT + f"(abandoned: {reason} - no answer will come)")
    elif task.is_closed:
        reason = task.close_reason or "no reason recorded"
        lines.append(INDENT + f"(closed: {reason} - no answer came)")
    else:
        lines.append(
            INDENT + f"(no answer yet - dispatched {human_duration(task.age_seconds)} ago)"
        )

    if task.open_decision:
        lines.append(INDENT + f"Open decision: {task.open_decision}")

    return "\n".join(lines)


def _answer_lines(answer: str, full: bool) -> tuple[list[str], int]:
    """The answer lines to print, and how many were held back.

    `full` turns the cut off. An answer shorter than the limit loses nothing.
    """
    lines = answer.splitlines()
    if full or len(lines) <= REPORT_LINE_LIMIT:
        return lines, 0
    return lines[:REPORT_LINE_LIMIT], len(lines) - REPORT_LINE_LIMIT


def usage_breakdown(usage: Usage) -> str:
    """The parts behind the headline numbers."""
    if not usage.turns:
        return "no turns recorded"
    duration = usage.duration_seconds
    rows = [
        f"turns: {usage.turns}",
        f"input: {usage.input_tokens}",
        f"output: {usage.output_tokens}",
        f"cache read: {usage.cache_read}",
        f"cache write: {usage.cache_write}",
        f"reasoning: {usage.reasoning}",
        f"total: {usage.total_tokens}",
        f"cost: {format_cost(usage.cost_total)}",
        f"provider: {usage.provider or 'unknown'}",
        f"model: {usage.model or 'unknown'}",
    ]
    if duration is not None:
        rows.append(f"span: {human_duration(duration)}")
    return "\n".join(INDENT + row for row in rows)
