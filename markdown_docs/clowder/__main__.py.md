# `clowder/__main__.py`

## Purpose

This file makes the package runnable as a module. `py -3.15 -m clowder ...` and the
installed `clowder` command do the same thing: both end up in `clowder.cli.main`.
It exists because a Python package needs a `__main__.py` before `python -m clowder`
can execute it.

## Public API

No public names are defined here. The file only wires the package entry point to
the CLI.

## How It Works (internals)

There are no `_`-prefixed helpers. The whole module is three statements:

- `import sys` - used to pass the CLI's return value to the process exit code.
- `from .cli import main` - imports the single CLI entry function.
- `if __name__ == "__main__": sys.exit(main())` - runs the CLI only when the file is
  executed, not when it is imported. `main()` returns an integer exit code, which
  `sys.exit` turns into the process status.

The relative import (`.cli`) is important: it keeps the entry point working whether
the package is installed or run from a source checkout.
