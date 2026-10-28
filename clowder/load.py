"""The machine's current load, read with the standard library only.

The numbers here are the ones the cost gate compares against, and every one of
them is a measured figure of this box, not a percentage of a total that leaves
something out:

- `available_bytes` is the memory a process can have now: free plus standby. It is
  `GlobalMemoryStatusEx`'s `ullAvailPhys`, which equals `\\Memory\\Available MBytes`
  within a few MB, so it needs no second call.
- `commit_headroom_bytes` is `ullAvailPageFile`: the hard wall (commit limit minus
  committed bytes) rather than a fraction of it.
- `installed_bytes` is the RAM the box was built with, from the SMBIOS table. On
  this box every Windows total reads half of it, because the GPU takes the other
  half for itself.
- `gpu_carveout_bytes` is that half, read once from the registry.

`dwMemoryLoad` is still here, because a person reads a percentage faster than a
GiB figure. It is not a gate: its denominator excludes the GPU's own memory, so a
resident 25 GiB model moves it not at all.

A probe that fails returns None and never raises: a broken measurement must not
block work. This module only reads.
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

# Windows has no load average, so the reading is the percent of physical memory
# in use. Unix reports the 1-minute load average, divided by the CPU count so one
# number means the same thing on a 4-core and a 16-core box.
METRIC_MEMORY = "memory"
METRIC_LOAD = "load"

GIB = 1024**3

# The display class key that holds every graphics adapter's registry values.
_DISPLAY_CLASS = (
    r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
)
# The value that holds the card's real memory as a 64-bit number. `AdapterRAM`, the
# field a WMI query gives, is 32-bit and wraps: it reports 4 GiB on a 32 GiB card.
_GPU_MEMORY_VALUE = "HardwareInformation.qwMemorySize"


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


@dataclass(frozen=True)
class Memory:
    """One read of the memory struct, in bytes.

    `total_bytes` is the memory Windows itself can hand out, which is not the RAM
    the box has when a GPU has taken a share of it. `installed_bytes()` is that.
    """

    percent: float
    total_bytes: int
    available_bytes: int
    commit_limit_bytes: int
    commit_headroom_bytes: int


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


# A failed read of the GPU carve-out is remembered, so the gate does not retry the
# registry on every dispatch. `_UNREAD` tells "not read yet" from "read, and None".
_UNREAD = object()
_carveout: object = _UNREAD


def memory_reading() -> Memory | None:
    """The memory struct in bytes, or None when it cannot be read."""
    if sys.platform != "win32":
        return None
    try:
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if not _kernel32().GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return Memory(
            percent=float(status.dwMemoryLoad),
            total_bytes=int(status.ullTotalPhys),
            available_bytes=int(status.ullAvailPhys),
            commit_limit_bytes=int(status.ullTotalPageFile),
            commit_headroom_bytes=int(status.ullAvailPageFile),
        )
    except OSError, AttributeError, ValueError:
        return None


def memory_percent() -> float | None:
    """Percent of physical memory in use, or None when it cannot be read."""
    reading = memory_reading()
    return None if reading is None else reading.percent


def installed_bytes() -> int | None:
    """The RAM the box was built with, in bytes, or None when unreadable.

    `GetPhysicallyInstalledSystemMemory` reads the SMBIOS table and returns KiB. It
    is the only honest answer to "what does this box have": every Windows total is
    short by whatever the GPU has reserved, which on this box is half the RAM.
    """
    if sys.platform != "win32":
        return None
    try:
        kib = ctypes.c_ulonglong(0)
        if not _kernel32().GetPhysicallyInstalledSystemMemory(ctypes.byref(kib)):
            return None
        return int(kib.value) * 1024 if kib.value else None
    except OSError, AttributeError, ValueError:
        return None


def gpu_carveout_bytes() -> int | None:
    """The RAM Windows cannot see because the GPU holds it, in bytes, or None.

    Read from the registry, once: the value cannot change while the machine is up,
    and the gate has to stay under a second. The figure is the largest
    `HardwareInformation.qwMemorySize` under the display class, which is the card
    Windows is driving; a machine with no discrete card has no such value.
    """
    global _carveout
    if _carveout is not _UNREAD:
        return _carveout  # type: ignore[return-value]
    _carveout = None
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _DISPLAY_CLASS) as display:
            index = 0
            largest = 0
            while True:
                try:
                    name = winreg.EnumKey(display, index)
                except OSError:
                    break
                index += 1
                if not name.isdigit():
                    continue
                try:
                    with winreg.OpenKey(display, name) as adapter:
                        value, _ = winreg.QueryValueEx(adapter, _GPU_MEMORY_VALUE)
                except OSError:
                    continue
                if isinstance(value, int):
                    largest = max(largest, value)
        _carveout = largest or None
    except OSError, ImportError, AttributeError, ValueError:
        _carveout = None
    return _carveout  # type: ignore[return-value]


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
