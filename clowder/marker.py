"""The dispatch marker.

A brief arrives in a worker's pane as plain text, which makes it look exactly
like the human typing. That is the failure this line prevents: a worker cannot
otherwise tell a peer job from its owner, and the two need different answers.

The marker is added by the tool, at dispatch time. Rules in prose get forgotten;
rules in a tool cannot be.

    [clowder job t-0004 | ship | clowder | from topcat]
    <the brief, unchanged>

It carries the four things a worker needs before it starts: that this is a job,
which job it is, which repo it belongs to, and who sent it.
"""

from __future__ import annotations

MARKER_PREFIX = "clowder job"
DEFAULT_SENDER = "the front door"


def marker_line(task_id: str, shape: str, repo: str, sender: str) -> str:
    """The one-line header that opens a dispatched brief."""
    parts = [f"{MARKER_PREFIX} {task_id}", shape, repo, f"from {sender}"]
    return "[" + " | ".join(parts) + "]"


def apply_marker(brief: str, task_id: str, shape: str, repo: str, sender: str) -> str:
    """The exact text handed to the multiplexer."""
    return f"{marker_line(task_id, shape, repo, sender)}\n{brief}"
