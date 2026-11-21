"""T9: a hung ingest child must not leave the upload "indexing" forever.

The upload worker awaits ``process.communicate()`` with no timeout, so a child
that never returns (deadlocked parser, unresponsive OCR endpoint, runaway
loop) keeps the record at "indexing" indefinitely and leaks the process.
``_communicate_with_timeout`` caps the wait, kills the child, and raises a
recoverable error the caller turns into a "failed" record; cancellation also
kills the child so shutdown leaves no orphan.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from app import web_api


def _spawn_sleeper():
    return asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(120)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


@pytest.mark.asyncio
async def test_hung_child_is_killed_on_timeout(monkeypatch):
    monkeypatch.setattr(web_api, "_INGEST_TIMEOUT_S", 0.3)
    process = await _spawn_sleeper()
    try:
        with pytest.raises(RuntimeError, match="解析超时"):
            await web_api._communicate_with_timeout(process)
        # The child was killed and reaped, not left running.
        assert process.returncode is not None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.asyncio
async def test_cancellation_kills_child(monkeypatch):
    monkeypatch.setattr(web_api, "_INGEST_TIMEOUT_S", 60)
    process = await _spawn_sleeper()

    async def _cancel_soon():
        await web_api._communicate_with_timeout(process)

    task = asyncio.create_task(_cancel_soon())
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Cancelled mid-wait: the child must be dead, not orphaned.
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_fast_child_returns_normally(monkeypatch):
    monkeypatch.setattr(web_api, "_INGEST_TIMEOUT_S", 30)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "print('ok')",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _err = await web_api._communicate_with_timeout(process)
    assert b"ok" in out
    assert process.returncode == 0
