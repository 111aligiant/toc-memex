from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time

import pytest

from app.curation import lock as curator_lock
from app.curation import runner
from app.db import init_db, make_engine, session_factory
from app.models import AgentNote, Document, Namespace


async def _engine(tmp_path):
    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    return engine


# ── §9.1 step 3: single-instance lock ────────────────────────────────────


@pytest.mark.asyncio
async def test_lock_excludes_a_second_run(tmp_path):
    engine = await _engine(tmp_path)
    async with session_factory(engine)() as db:
        async with curator_lock.run_lock(db, tmp_path, "run_a"):
            with pytest.raises(curator_lock.CuratorBusy):
                async with curator_lock.run_lock(db, tmp_path, "run_b"):
                    pass
    await engine.dispose()


@pytest.mark.asyncio
async def test_lock_is_released_after_the_run(tmp_path):
    engine = await _engine(tmp_path)
    async with session_factory(engine)() as db:
        async with curator_lock.run_lock(db, tmp_path, "run_a"):
            pass
        assert not curator_lock.lock_path(tmp_path).exists()
        async with curator_lock.run_lock(db, tmp_path, "run_b"):
            pass  # taking it again proves it was released
    await engine.dispose()


@pytest.mark.asyncio
async def test_stale_lock_is_broken(tmp_path):
    """A lock left by a dead process must not block the curator forever."""
    engine = await _engine(tmp_path)
    path = curator_lock.lock_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"run_id": "dead", "pid": 999999, "started": 0}), encoding="utf-8"
    )
    async with session_factory(engine)() as db:
        async with curator_lock.run_lock(db, tmp_path, "run_new"):
            assert json.loads(path.read_text(encoding="utf-8"))["run_id"] == "run_new"
    await engine.dispose()


@pytest.mark.asyncio
async def test_lock_records_a_visible_heartbeat(tmp_path):
    from sqlalchemy import text

    engine = await _engine(tmp_path)
    async with session_factory(engine)() as db:
        async with curator_lock.run_lock(db, tmp_path, "run_hb"):
            row = (
                await db.execute(
                    text("SELECT state FROM curator_runs WHERE run_id='run_hb'")
                )
            ).scalar_one()
            assert row == "running"
        row = (
            await db.execute(
                text("SELECT state FROM curator_runs WHERE run_id='run_hb'")
            )
        ).scalar_one()
        assert row == "finished"
    await engine.dispose()


# ── liveness probe must never signal/kill the process it inspects ────────
# On Windows os.kill(pid, 0) calls TerminateProcess and would *kill* a live
# holder. These tests pin the safe ctypes behaviour: a running process is
# reported alive and stays alive; a dead pid is reported dead.


def test_pid_alive_sees_a_running_process_without_killing_it():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        # Give the interpreter a moment to reach sleep().
        deadline = time.time() + 5
        while not curator_lock._pid_alive(child.pid) and time.time() < deadline:
            time.sleep(0.05)
        assert curator_lock._pid_alive(child.pid) is True
        # The bug being guarded against would have terminated it here.
        assert child.poll() is None, "liveness probe must not kill a live process"
    finally:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()


def test_pid_alive_reports_a_dead_pid():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=10)
    assert curator_lock._pid_alive(child.pid) is False


def test_is_stale_keeps_a_live_foreign_lock():
    """A live holder's lock is not stale, and checking it must not kill it."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        deadline = time.time() + 5
        while not curator_lock._pid_alive(child.pid) and time.time() < deadline:
            time.sleep(0.05)
        info = {"run_id": "live", "pid": child.pid, "started": time.time()}
        assert curator_lock._is_stale(info) is False
        assert child.poll() is None
    finally:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()


def test_is_stale_breaks_a_dead_pid_lock():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=10)
    info = {"run_id": "dead", "pid": child.pid, "started": time.time()}
    assert curator_lock._is_stale(info) is True


# ── §6.4: wall-clock budget ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_stops_when_wall_clock_is_exhausted(tmp_path):
    engine = await _engine(tmp_path)
    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        for index in range(3):
            db.add(
                Document(
                    id=f"d{index}", namespace_id="shared", title=f"Doc {index}",
                    filename=f"{index}.pdf", file_path=f"{index}.pdf",
                    content_hash=f"h{index}", page_count=5, status="ready",
                )
            )
        await db.commit()

        # Zero budget: the loop must stop before touching any document.
        result = await runner.run_once(db, tmp_path, limit=3, wall_clock_minutes=0)
        assert result["timed_out"] is True
        assert result["documents"] == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_normal_run_is_not_marked_timed_out(tmp_path):
    engine = await _engine(tmp_path)
    async with session_factory(engine)() as db:
        result = await runner.run_once(db, tmp_path, limit=1, wall_clock_minutes=45)
        assert result["timed_out"] is False
    await engine.dispose()


# ── §9.1 step 5: hand-off note ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_leaves_a_handoff_note(tmp_path):
    from sqlalchemy import select

    engine = await _engine(tmp_path)
    async with session_factory(engine)() as db:
        result = await runner.run_once(db, tmp_path, limit=1)
        notes = (
            (await db.execute(select(AgentNote).where(AgentNote.author == "curator")))
            .scalars()
            .all()
        )
        assert len(notes) == 1
        assert notes[0].kind == "handoff"
        assert result["run_id"] in notes[0].body
    await engine.dispose()


@pytest.mark.asyncio
async def test_handoff_says_so_when_nothing_changed(tmp_path):
    from sqlalchemy import select

    engine = await _engine(tmp_path)
    async with session_factory(engine)() as db:
        await runner.run_once(db, tmp_path, limit=1)
        note = (
            await db.execute(select(AgentNote).where(AgentNote.author == "curator"))
        ).scalars().one()
        assert "没有需要改动" in note.body
    await engine.dispose()
