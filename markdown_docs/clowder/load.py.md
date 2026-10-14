# `clowder/load.py`

## Purpose

This module reads the machine's current load using only the Python standard
library, so `clowder` can optionally refuse to start a heavy step while another one
is running.

The load gate is opt-in: if the config has no `[gates]` threshold for the metric
this platform reports, the check is off. The module only reads; it never changes
anything. If the reading fails for any reason, the gate fails open - a broken probe
must never block work.

The metric differs by platform:

- **Windows**: there is no load average, so the reading is the percent of physical
  memory in use.
- **Unix**: the reading is the 1-minute load average divided by the CPU count, so
  one number means the same thing on a 4-core box and a 16-core box.

> **Python version:** these docs assume clowder's declared target, Python 3.15.
> The `except OSError, AttributeError, ValueError:` form at lines 51 and 64 is
> PEP 758 paren-less syntax, which is valid on Python 3.14 and later. An earlier
> revision of this page called it a syntax error; that was measured under Python
> 3.13, which predates PEP 758, and is wrong for this project.

## Public API

```python
def metric_name() -> str
def memory_percent() -> float | None
def load_per_cpu() -> float | None
def current() -> tuple[str, float] | None
```

- `metric_name()` - the metric this platform reports: `"memory"` on Windows,
  `"load"` everywhere else. Returns `str`.
- `memory_percent()` - percent of physical memory in use, or `None` when it cannot
  be read. Windows only in practice; returns `None` on any failure.
- `load_per_cpu()` - the 1-minute load average divided by the CPU count, or `None`.
  Returns `None` on Windows because `os.getloadavg` is absent there. Returns
  `float`.
- `current()` - the platform's metric name and its current value as
  `(name, value)`, or `None` when the value is unreadable. Returns
  `tuple[str, float] | None`.

Module constants:

- `METRIC_MEMORY = "memory"`
- `METRIC_LOAD = "load"`

## How It Works (internals)

The only private name is a class, not a function:

### `_MemoryStatusEx` - the Windows memory probe

- `class _MemoryStatusEx(ctypes.Structure)` - a `ctypes` structure matching the
  fields that Windows' `GlobalMemoryStatusEx` function fills. Its `_fields_` list
  declares names and C types (`wintypes.DWORD`, `ctypes.c_ulonglong`, ...). The
  field of interest is `dwMemoryLoad`, the percentage of physical memory in use.

Call relationships:

- `current()` calls `metric_name()`, then `memory_percent()` (when the metric is
  memory) or `load_per_cpu()` (otherwise), and returns either the pair or `None`.
- `memory_percent()` builds a `_MemoryStatusEx`, sets `dwLength` to its own size,
  calls `ctypes.windll.kernel32.GlobalMemoryStatusEx(...)`, and reads back
  `dwMemoryLoad`.
- `load_per_cpu()` looks up `os.getloadavg` with `getattr(..., None)` (the
  attribute is Unix-only), divides the first value by `os.cpu_count() or 1`, and
  returns a float.

There are no other `_`-helpers.
