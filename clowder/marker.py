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
# The bus mechanics - the remote-pi Docker service, `/remote-pi join`, the "Not in a
# session" answer and the `clowder inbox` fallback - live in the shipped crew skill,
# which the worker has in its system prompt. The marker keeps only the instruction to
# send, so shortening it cannot repeat the decision-22 failure: the worker is still
# told to send, with the job id, a headline and the answer's place.
REPORT_INSTRUCTION = (
    "Report per the crew skill.\n"
    "End with one line: For the owner: <two sentences, his words, what changes for him>.\n"
    "Send the report to {sender} over the agent bus, by name in your peer list:\n"
    "the job id, a one-line headline, where the full answer is.\n"
    'Close it: clowder step report {task_id} --headline "<one line>"; clowder inbox\n'
    "finds it."
)


def marker_line(task_id: str, shape: str, repo: str, sender: str) -> str:
    """The one-line header that opens a dispatched brief."""
    parts = [f"{MARKER_PREFIX} {task_id}", shape, repo, f"from {sender}"]
    return "[" + " | ".join(parts) + "]"


def apply_marker(brief: str, task_id: str, shape: str, repo: str, sender: str) -> str:
    """The exact text handed to the multiplexer."""
    header = marker_line(task_id, shape, repo, sender)
    instruction = REPORT_INSTRUCTION.format(sender=sender, task_id=task_id)
    return f"{header}\n{instruction}\n{brief}"
