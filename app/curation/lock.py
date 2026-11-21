"""Single-instance lock for one curator run (ADR-0002 §9.1 step 3).

Two concurrent runs would fight over the same documents: both snapshot the
same pre-write state, both create jobs with the same fingerprint, and the
loser's writes land on rows the winner already changed. The lock is a file
plus a DB heartbeat row:

- the file lock is what actually excludes a second process (atomic create,
  stale locks are detected by pid + age and broken);
- the heartbeat is visible to the Web UI and to a human debugging a stuck
  run, which a bare lock file is not.

The lock is advisory and process-local by design: this is a single-user,
loopback-only service, and a stale lock must never be able to block the
curator forever.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

LOCK_NAME = "curator.lock"
STALE_SECONDS = 15 * 60

_STILL_ACTIVE = 259


def _pid_alive(pid: int) -> bool:
    """Return True if *pid* names a live process, never signalling/killing it.

    On POSIX ``os.kill(pid, 0)`` only probes existence. On Windows, however,
    ``os.kill`` routes through ``TerminateProcess`` — passing signal 0 would
    *terminate* the target, so a liveness check must never use it. There we
    open a handle with the cheapest possible right and read the exit code:
    a still-running process reports ``STILL_ACTIVE`` (259). Any failure to
    open (access denied / no such process) means "cannot prove it is ours",
    so we treat it as not-alive and let the stale lock be broken.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid)
        )
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            if not ok:
                return False
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


class CuratorBusy(RuntimeError):
    """Another curator run holds the lock."""


def lock_path(data_dir) -> Path:
    return Path(data_dir) / LOCK_NAME


def _read_lock(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _is_stale(info: dict | None) -> bool:
    if not info:
        return True
    try:
        started = float(info.get("started") or 0)
    except (TypeError, ValueError):
        return True
    if time.time() - started > STALE_SECONDS:
        return True
    pid = info.get("pid")
    if pid and pid != os.getpid():
        # A pid that no longer exists means the holder died without cleaning up.
        # Must never signal the process (os.kill on Windows would terminate it).
        try:
            if not _pid_alive(int(pid)):
                return True
        except (OSError, ValueError):
            return True
    return False


@asynccontextmanager
async def run_lock(db: AsyncSession, data_dir, run_id: str):
    """Hold the curator lock for one run; raises CuratorBusy if taken."""
    path = lock_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    holder = _read_lock(path)
    if holder is not None and not _is_stale(holder):
        raise CuratorBusy(
            f"another curator run is active (run {holder.get('run_id')}, "
            f"pid {holder.get('pid')}); wait for it or delete {path}"
        )
    payload = json.dumps(
        {
            "run_id": run_id,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started": time.time(),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    # O_EXCL is the atomic part: a second process cannot take the same slot.
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # Somebody else won the race between our staleness check and here.
        if not _is_stale(_read_lock(path)):
            raise CuratorBusy(f"curator lock is held: {path}") from None
        path.unlink(missing_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)

    await _heartbeat(db, run_id, "running")
    try:
        yield run_id
    finally:
        path.unlink(missing_ok=True)
        await _heartbeat(db, run_id, "finished")


async def _heartbeat(db: AsyncSession, run_id: str, state: str) -> None:
    """Record the run in the DB so a human can see what is running.

    Best effort: the lock file is the authority, this row is for visibility.
    """
    try:
        await db.execute(
            text(
                "INSERT INTO curator_runs (run_id, state, pid, started_at, updated_at) "
                "VALUES (:run, :state, :pid, datetime('now'), datetime('now')) "
                "ON CONFLICT(run_id) DO UPDATE SET "
                "state = :state, updated_at = datetime('now')"
            ),
            {"run": run_id, "state": state, "pid": os.getpid()},
        )
        await db.commit()
    except Exception:  # a missing table must not block the run
        await db.rollback()
