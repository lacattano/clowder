# `clowder/report.py`

## Purpose

This module turns a task (and, optionally, its usage and its answer) into the text
of a report. The shape is fixed, because the shape is the point:

1. The question first, so a late answer still knows what it answers (`Re: ...`).
2. One indented usage line.
3. The answer.
4. At most one open decision.

It also has small formatters for token counts and cost, and a helper that says
where the work happened (main checkout, a named worktree, or a branch at a commit).

The module is pure formatting. It reads no files and runs no commands; the caller
supplies the `Task`, `Usage`, `Job` and answer objects.

## Public API

```python
def format_tokens(count: int) -> str
def format_cost(amount: float) -> str
def worktree_label(task: Task) -> str
def usage_line(task: Task, usage: Usage | None) -> str
def build_report(task: Task, usage: Usage | None, answer: str | None,
                 job: Job | None = None, *, full: bool = False) -> str
def usage_breakdown(usage: Usage) -> str
```

- `format_tokens(count)` - a compact token figure: `1234`, `12.3k`, `1.2M`.
- `format_cost(amount)` - a compact cost string: `$0`, `<$0.0001`, `$0.0123`,
  `$1.23`.
- `worktree_label(task)` - where the work happened. Prefers `branch @ commit`,
  then the branch name, then `main checkout`, then `worktree <name>`.
- `usage_line(task, usage)` - the single line under the question:
  `agent | repo | place | age | tokens | cost | model`. Falls back to `no usage yet`.
- `build_report(task, usage, answer, job=None, *, full=False)` - the whole report
  block in the fixed order. `full=True` disables the answer's line cut. When the
  task is part of a job, a `change:` line naming the job title and state is added.
- `usage_breakdown(usage)` - the indented detail block behind the headline usage
  numbers: turns, input, output, cache read/write, reasoning, total, cost, provider,
  model, and span.

Module constants:

- `INDENT = "    "` - the four-space indent used for every body line.
- `REPORT_LINE_LIMIT = 40` - how many answer lines are shown before the report
  points at `--full`.

## How It Works (internals)

Every private helper is listed under the public function that calls it.

### `worktree_label(task)` - names the place the work happened

- `_same_dir(one, other) -> bool` - compares two paths as directories, ignoring
  case and path shape. It normalizes the case (`os.path.normcase`) and the absolute
  form (`os.path.abspath`) before comparing. Used to decide whether the recorded
  worktree is actually the repo's main checkout.

### `build_report(task, usage, answer, job=None, *, full=False)` - the report block

- `_answer_lines(answer, full) -> tuple[list[str], int]` - splits the answer into
  lines and returns the lines to show plus the count held back. With `full=True`, or
  when the answer is at most `REPORT_LINE_LIMIT` lines, nothing is held back.

The core of `build_report` is the fixed line order: `Re: <question>`, the usage line,
an optional `change:` line, the answer (or an `(abandoned: ...)`, `(closed: ...)` or
`(no answer yet ...)` placeholder), and an optional `Open decision:` line. Long
answers get a `... N more line(s) hidden; read the whole answer with --full` marker.
The JSON output path never truncates; only this human text does.

There are no other `_`-helpers. `format_tokens`, `format_cost`, `usage_line` and
`usage_breakdown` hold their short logic directly.
