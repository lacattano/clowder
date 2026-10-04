"""Every gate this repo has, in one place.

One command runs them, locally and in CI, so the list of gates cannot drift
between what a human runs and what the robot runs. Rules in a tool, not in prose.

    py -3.14 scripts/check.py              # every gate
    py -3.14 scripts/check.py --list        # what they are
    py -3.14 scripts/check.py --only lint   # one of them

Needs no dependencies. ruff and mypy are fetched with `uvx` when they are not
already importable, so the gates work on a machine with nothing installed.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTHON = sys.executable

# ruff and mypy are development tools, not dependencies of the tool.
LINT_TOOLS = ("ruff", "mypy")


@dataclass(frozen=True)
class Gate:
    name: str
    description: str
    commands: list[list[str]]


def tool_command(tool: str, *args: str) -> list[str]:
    """Run a development tool the way this machine can.

    `uvx` first, so nothing has to be installed. Then a module import, then a
    binary on PATH. Anything else is an error, not a silent skip: a gate that
    cannot run has not passed.
    """
    if shutil.which("uvx"):
        return ["uvx", tool, *args]
    probe = subprocess.run(
        [PYTHON, "-c", f"import {tool}"],
        capture_output=True,
        text=True,
    )
    if probe.returncode == 0:
        return [PYTHON, "-m", tool, *args]
    found = shutil.which(tool)
    if found:
        return [found, *args]
    raise SystemExit(
        f"{tool} is not available: install uv (recommended), or pip install {tool}"
    )


def clowder(*args: str) -> list[str]:
    return [PYTHON, "-m", "clowder", *args]


def gates(workdir: Path) -> list[Gate]:
    """The gate list, built fresh so the temp paths are real."""
    state = workdir / "state.json"
    page = workdir / "board.html"

    return [
        Gate(
            name="smoke",
            description="the CLI runs on a machine with no multiplexer",
            commands=[
                clowder("--version"),
                clowder("--help"),
                clowder("tasks", "--state", str(state)),
                clowder("job", "list", "--state", str(state)),
                # The board is the one command that reads the multiplexer and still
                # has to work when it cannot be reached, which is the case in CI.
                clowder("board", "--out", str(page), "--state", str(state)),
                clowder(
                    "dispatch",
                    "nobody",
                    ".",
                    # A scout: it changes nothing, so it needs no place. A ship
                    # would be refused here, which is the rule, not a smoke failure.
                    "scout: smoke test",
                    "--shape",
                    "scout",
                    "--dry-run",
                    "--state",
                    str(state),
                ),
            ],
        ),
        Gate(
            name="lint",
            description="ruff check",
            commands=[tool_command("ruff", "check", ".")],
        ),
        Gate(
            name="format",
            description="ruff format --check",
            commands=[tool_command("ruff", "format", "--check", ".")],
        ),
        Gate(
            name="type",
            description="mypy over the package",
            commands=[tool_command("mypy", "clowder")],
        ),
        Gate(
            name="tests",
            description="the whole test suite",
            commands=[[PYTHON, "-m", "unittest", "discover", "-s", "tests", "-t", "."]],
        ),
    ]


def run(gate: Gate) -> bool:
    print(f"\n=== {gate.name}: {gate.description}")
    for command in gate.commands:
        shown = " ".join(command)
        print(f"--- {shown}")
        completed = subprocess.run(command, cwd=str(REPO))
        if completed.returncode != 0:
            print(f"!!! {gate.name} failed: exit {completed.returncode}")
            return False
    return True


def smoke_assertions(workdir: Path) -> list[str]:
    """Checks that a gate's exit code cannot make: it wrote what it promised."""
    problems: list[str] = []
    page = workdir / "board.html"
    if not page.is_file():
        problems.append(f"the board wrote no page at {page}")
    elif "clowder board" not in page.read_text(encoding="utf-8"):
        problems.append("the board page holds no title")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check", description=__doc__.splitlines()[0])
    parser.add_argument("--list", action="store_true", help="print the gates and stop")
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="GATE",
        help="run one gate; repeatable",
    )
    parser.add_argument(
        "--fail-fast", action="store_true", help="stop at the first failing gate"
    )
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="clowder-check-") as tmp:
        workdir = Path(tmp)
        available = gates(workdir)

        if args.list:
            for gate in available:
                print(f"{gate.name:8} {gate.description}")
            return 0

        chosen = [g for g in available if not args.only or g.name in args.only]
        unknown = set(args.only) - {g.name for g in available}
        if unknown:
            print(f"unknown gate(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        if not chosen:
            print("no gates selected", file=sys.stderr)
            return 2

        results: list[tuple[str, bool]] = []
        for gate in chosen:
            passed = run(gate)
            if passed and gate.name == "smoke":
                problems = smoke_assertions(workdir)
                for problem in problems:
                    print(f"!!! smoke: {problem}")
                passed = not problems
            results.append((gate.name, passed))
            if not passed and args.fail_fast:
                break

    print("\n=== summary")
    for name, passed in results:
        print(f"{'pass' if passed else 'FAIL'}  {name}")
    failed = [name for name, passed in results if not passed]
    if failed:
        print(f"\n{len(failed)} gate(s) failed: {', '.join(failed)}")
        return 1
    print(f"\nall {len(results)} gate(s) passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
