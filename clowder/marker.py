"""The dispatch marker.

A brief arrives in a worker's pane as plain text, which makes it look exactly
like the human typing. That is the failure this line prevents: a worker cannot
otherwise tell a peer job from its owner, and the two need different answers.

The marker is added by the tool, at dispatch time. Rules in prose get forgotten;
rules in a tool cannot be.

    [clowder job t-0004 | ship | clowder | from topcat]
    <how to report, added by the tool>
    <the brief, unchanged>

It carries the four things a worker needs before it starts: that this is a job,
which job it is, which repo it belongs to, and who sent it. The tool then tells
the worker to send its report over the agent bus when the step is done, resolved
by name in the worker's own peer list. The answer still stays in the worker's
session, so `clowder inbox` can find it if the send does not arrive.
"""

from __future__ import annotations

MARKER_PREFIX = "clowder job"
DEFAULT_SENDER = "the front door"

# Appended to every dispatched brief. It names no address on purpose: addresses are
# per-observer and move when panes reload, so the worker resolves the name afresh.
# A pane can only send once the remote-pi Docker service runs and the pane is joined,
# and only a human can do that from the pane.
REPORT_INSTRUCTION = (
    "When this step is done, send its report to {sender} over the agent bus: find them by\n"
    "name in your own peer list (never reuse an old address from earlier), then send the\n"
    "job id, a one-line headline, and where the full answer is. A pane can only send once\n"
    "two things are true: the remote-pi Docker service is running, and this pane is joined\n"
    "with /remote-pi join. If `list_peers` says 'Not in a session', the pane is not joined -\n"
    "say so rather than fail quietly, and let the human join it; a slash command sent as a\n"
    "prompt is a message, not a command. Your answer also stays in your session, and\n"
    "`clowder inbox` is the fallback if the send does not arrive."
)


def marker_line(task_id: str, shape: str, repo: str, sender: str) -> str:
    """The one-line header that opens a dispatched brief."""
    parts = [f"{MARKER_PREFIX} {task_id}", shape, repo, f"from {sender}"]
    return "[" + " | ".join(parts) + "]"


def apply_marker(brief: str, task_id: str, shape: str, repo: str, sender: str) -> str:
    """The exact text handed to the multiplexer."""
    header = marker_line(task_id, shape, repo, sender)
    instruction = REPORT_INSTRUCTION.format(sender=sender)
    return f"{header}\n{instruction}\n{brief}"
