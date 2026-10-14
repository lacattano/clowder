# `clowder/errors.py`

## Purpose

This module defines the tool's error family. Every deliberate failure in `clowder`
raises one of these classes instead of a bare `Exception`, and each class carries
the process exit code the CLI should return. That split lets the CLI catch one
base type, print a clean message, and exit with a code a script can test.

The five concrete errors mirror the five places work can fail: the config file, the
user's request, the saved state, the multiplexer, git, and dispatch.

## Public API

All classes derive from `ClowderError`, so `except ClowderError:` catches any
intended failure. `exit_code` is a class attribute, read from the class (or from an
instance) by the top-level error handler.

| Class | Base | `exit_code` | Raised when |
| --- | --- | --- | --- |
| `ClowderError` | `Exception` | `1` | Base class. Not raised directly. |
| `ConfigError` | `ClowderError` | `1` | The config file is missing, unreadable or wrong. |
| `UsageError` | `ClowderError` | `1` | The user asked for something the tool cannot mean. |
| `StateError` | `ClowderError` | `1` | The state file is corrupt, or an id does not exist. |
| `MuxError` | `ClowderError` | `2` | The multiplexer could not be run, or its output made no sense. |
| `GitError` | `ClowderError` | `2` | A git command failed, or a tree was in no state to be worked in. |
| `DispatchError` | `ClowderError` | `2` | A task could not be sent to an agent. |

Signatures (all bodies are docstring only; the work is in the class attribute):

```python
class ClowderError(Exception):
    exit_code = 1

class ConfigError(ClowderError): ...
class UsageError(ClowderError): ...
class StateError(ClowderError): ...
class MuxError(ClowderError): exit_code = 2
class GitError(ClowderError): exit_code = 2
class DispatchError(ClowderError): exit_code = 2
```

## How It Works (internals)

There are no `_`-prefixed helpers, no functions and no instance logic in this
module. Each class is a docstring plus the inherited or overridden `exit_code`
integer.

Note the split in exit codes: "the user's input is wrong" (`ConfigError`,
`UsageError`, `StateError`) exits `1`, while "the environment failed" (`MuxError`,
`GitError`, `DispatchError`) exits `2`. Scripts can tell the two apart without
parsing the message text.
