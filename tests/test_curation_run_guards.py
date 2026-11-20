from __future__ import annotations

import asyncio
import json

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
