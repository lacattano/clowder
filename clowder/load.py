"""The machine's current load, read with the standard library only.

A heavy step is opt-in to the load check: with no [gates] threshold for this
platform's metric, the check is off. When the metric cannot be read the gate
fails open - a broken probe must never block work. This module only reads.
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from typing import Any

# Windows has no load average, so the reading is the percent of physical memory
# in use. Unix reports the 1-minute load average, divided by the CPU count so one
# number means the same thing on a 4-core and a 16-core box.
METRIC_MEMORY = "memory"
METRIC_LOAD = "load"


def metric_name() -> str:
    """The metric this platform reports."""
    return METRIC_MEMORY if sys.platform == "win32" else METRIC_LOAD


class _MemoryStatusEx(ctypes.Structure):
    """The Windows structure GlobalMemoryStatusEx fills."""

    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _kernel32() -> Any:
    """The Windows kernel32 library.

    `ctypes.windll` exists only on Windows. It is looked up by name rather than
    referenced directly, because a direct reference is an attr-defined error on
    the Linux runner; that error is real, so it is fixed here rather than
    silenced at the call site. Off Windows this raises, and the caller turns
    that into None.
    """
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        raise AttributeError("ctypes.windll exists only on Windows")
    return windll.kernel32


def memory_percent() -> float | None:
    """Percent of physical memory in use, or None when it cannot be read."""
    if sys.platform != "win32":
        return None
    try:
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if not _kernel32().GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return float(status.dwMemoryLoad)
    except OSError, AttributeError, ValueError:
        return None


def load_per_cpu() -> float | None:
    """The 1-minute load average divided by the CPU count, or None."""
    # `os.getloadavg` is Unix-only; the attribute is absent on Windows.
    getloadavg = getattr(os, "getloadavg", None)
    if getloadavg is None:
        return None
    try:
        cpus = os.cpu_count() or 1
        return float(getloadavg()[0]) / cpus
    except OSError, AttributeError, ValueError:
        return None


def current() -> tuple[str, float] | None:
    """The platform's metric and its current value, or None when unreadable."""
    name = metric_name()
    value = memory_percent() if name == METRIC_MEMORY else load_per_cpu()
    return (name, value) if value is not None else None
