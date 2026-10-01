"""Composing a report.

The shape is fixed, because the shape is the point: the question first, so a
late answer still knows what it answers. Then one usage line. Then the answer.
Then at most one open decision.
"""

from __future__ import annotations

from pathlib import Path

from .sessions import Usage
from .state import Task
from .timeutil import human_duration

INDENT = "    "


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


def worktree_label(task: Task) -> str:
    """Where the work happened: `branch @ commit`, else a checkout, else the main one."""
    if task.branch:
        return f"{task.branch} @ {task.commit}" if task.commit else task.branch
    if not task.worktree:
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
    return " | ".join(parts)


def build_report(task: Task, usage: Usage | None, answer: str | None) -> str:
    """One report block, in the fixed order."""
    lines = [f"Re: {task.question}", INDENT + usage_line(task, usage)]

    if answer:
        for line in answer.splitlines():
            lines.append(INDENT + line.rstrip())
    elif task.is_abandoned:
        reason = task.abandon_reason or "no reason recorded"
        lines.append(INDENT + f"(abandoned: {reason} - no answer will come)")
    else:
        lines.append(
            INDENT + f"(no answer yet - dispatched {human_duration(task.age_seconds)} ago)"
        )

    if task.open_decision:
        lines.append(INDENT + f"Open decision: {task.open_decision}")

    return "\n".join(lines)


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
