# `scripts/check.py`

## Purpose

This script holds every gate the clowder repo has, in one place. One command runs
them locally and in CI, so the list of gates cannot drift between what a human runs
and what the robot runs. The rule is stated at the top: "Rules in a tool, not in
prose."

```text
py -3.15 scripts/check.py            # every gate
py -3.15 scripts/check.py --list     # what they are
py -3.15 scripts/check.py --only lint
```

It needs no dependencies: `ruff` and `mypy` are fetched with `uvx` when they are
not already importable, so the gates work on a machine with only Python installed.

The gates are `smoke` (the CLI runs on a machine with no multiplexer), `lint`,
`format`, `type` (mypy over `clowder`) and `tests` (the whole test suite).

## Public API

### `Gate` (frozen dataclass)

```python
@dataclass(frozen=True)
class Gate:
    name: str
    description: str
    commands: list[list[str]]
```

One gate: a name, a one-line description, and the commands that must all pass.

### Functions

```python
def tool_command(tool: str, *args: str) -> list[str]
def clowder(*args: str) -> list[str]
def gates(workdir: Path) -> list[Gate]
def run(gate: Gate) -> bool
def smoke_assertions(workdir: Path) -> list[str]
def main(argv: list[str] | None = None) -> int
```

- `tool_command(tool, *args)` - returns the command to run a development tool the
  way this machine can: `uvx <tool>` if `uvx` exists, else `python -m <tool>` when
  it imports, else the binary on `PATH`. Exits with a message when none is
  available, because a gate that cannot run has not passed.
- `clowder(*args)` - returns `[python, "-m", "clowder", *args]`, so the CLI is
  always run from the source checkout.
- `gates(workdir)` - builds the gate list fresh, so the temp paths (`state.json`,
  `board.html`) are real.
- `run(gate)` - prints a header per command, runs it in the repo, and returns
  `False` at the first non-zero exit.
- `smoke_assertions(workdir)` - checks the smoke gate wrote what it promised: the
  board page exists and holds the `clowder board` title. These are the checks an
  exit code cannot make.
- `main(argv=None)` - parses `--list`, `--only` and `--fail-fast`, runs the chosen
  gates, prints a pass/FAIL summary, and returns `0`, `1` (a gate failed) or `2`
  (bad selection).

Module constants: `REPO` (the repo root), `PYTHON` (`sys.executable`),
`LINT_TOOLS = ("ruff", "mypy")`.

## How It Works (internals)

There are no `_`-prefixed helpers in this file.

- `main` creates one `tempfile.TemporaryDirectory`, builds the gates against it,
  then runs each chosen gate through `run`. After a passing `smoke` gate it also
  runs `smoke_assertions`, so a command that exits zero but writes nothing is still
  caught. Results are collected and printed as a summary.
- The `smoke` gate deliberately dispatches with `--shape scout --dry-run`, because
  a scout changes nothing and needs no place; a ship here would be refused by the
  rule, not fail as a smoke test.
- `run` streams each command's output straight through (no capture), so a human
  and CI see the same text.
