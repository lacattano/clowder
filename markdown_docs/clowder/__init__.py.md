# `clowder/__init__.py`

## Purpose

This is the package root for `clowder`, a tool that dispatches work to a crew of
coding agents and keeps their state. It holds no logic. It only names the package
and exposes its version number, so that `import clowder` works and callers can
read `clowder.__version__`.

The module docstring states the design motto: "The spine is code. The agent is
only the front." The behaviour lives in the modules under it; the agent is a thin
layer on top.

## Public API

| Name | Type | Meaning |
| --- | --- | --- |
| `__version__` | `str` | The package version, currently `"0.1.0"`. |
| `__all__` | `list[str]` | `["__version__"]`, so `from clowder import *` imports only the version. |

No classes or functions are defined here.

## How It Works (internals)

There are no `_`-prefixed helpers, no classes and no functions in this file. It is
a pure package marker: the docstring plus two module-level constants.

`__version__` is the single source of truth for the version string; packaging code
and the CLI read it from here rather than repeating the number.
