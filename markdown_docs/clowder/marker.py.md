# `clowder/marker.py`

## Purpose

This module builds the dispatch marker: the header and reporting instruction that
`clowder` prepends to every brief it sends to a worker agent.

The problem it solves: a brief arrives in a worker's pane as plain text, so it looks
exactly like the human typing. A worker otherwise cannot tell a peer's job from its
owner, and those two need different answers. The marker is added by the tool at
dispatch time, because rules in prose get forgotten while rules in a tool cannot be.

A dispatched brief looks like this:

```
[clowder job t-0004 | ship | clowder | from topcat]
Report per the crew skill.
When this step is done, send its report to topcat over the agent bus. ...
<the brief, unchanged>
```

The line carries the four things a worker needs before it starts: that this is a
job, which job, which repo, and who sent it. The instruction tells the worker to
send its report over the agent bus, resolved by name in the worker's own peer list.

## Public API

```python
def marker_line(task_id: str, shape: str, repo: str, sender: str) -> str
def apply_marker(brief: str, task_id: str, shape: str, repo: str, sender: str) -> str
```

- `marker_line(task_id, shape, repo, sender)` - returns the one-line bracketed
  header, e.g. `[clowder job t-0004 | ship | clowder | from topcat]`.
- `apply_marker(brief, task_id, shape, repo, sender)` - returns the exact text
  handed to the multiplexer: header, then the reporting instruction with the
  sender's name filled in, then the brief unchanged.

Module constants:

- `MARKER_PREFIX = "clowder job"` - the words that open every marker line.
- `DEFAULT_SENDER = "the front door"` - used when no sender is known.
- `REPORT_INSTRUCTION` - the template appended to each brief. Its `{sender}`
  placeholder is filled by `apply_marker`.

## How It Works (internals)

This module has no `_`-prefixed helpers.

- `marker_line` joins `MARKER_PREFIX`, `task_id`, `shape`, `repo` and
  `from <sender>` with `" | "` and wraps the result in square brackets.
- `apply_marker` calls `marker_line`, formats `REPORT_INSTRUCTION` with the sender,
  and joins the three pieces with newlines: `<header>\n<instruction>\n<brief>`.

One deliberate detail, explained in a comment in the source: the instruction names
the sender but no bus address. Addresses are per-observer and change when panes
reload, so the worker must look the name up in its own peer list each time. A
remembered address once reached the wrong agent and still looked delivered.
