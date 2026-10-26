"""Identify a durable queue claimant without relying on a long stale timeout."""
from __future__ import annotations

import os


def process_identity(pid: int) -> str | None:
    """Return the OS process birth identity, or None if it is gone/unknown."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
        api.GetProcessTimes.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not api.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                return None
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            api.CloseHandle(handle)
    try:
        from pathlib import Path

        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def claim_owner() -> dict:
    return {"pid": os.getpid(), "birth": process_identity(os.getpid())}


def owner_is_gone(owner: object) -> bool:
    if not isinstance(owner, dict) or not owner.get("birth") or type(owner.get("pid")) is not int:
        return False
    observed = process_identity(owner["pid"])
    if observed is not None:
        return observed != owner["birth"]
    # Query failure alone is not proof of death (e.g. access denied). On
    # Windows an invalid PID returns ERROR_INVALID_PARAMETER; on POSIX a
    # missing /proc entry identifies a dead process.
    if os.name == "nt":
        import ctypes

        return ctypes.get_last_error() == 87
    from pathlib import Path

    return not Path(f"/proc/{owner['pid']}").exists()
