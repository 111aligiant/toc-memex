"""Durable ingest jobs: claiming, the concurrency cap, restart recovery.

These tests never spawn a real ingest child and never touch OCR: they
exercise the database-level contract (``create_job`` / ``claim`` / ``pump`` /
``reconcile_jobs``) that both the web upload endpoint and the MCP tools
depend on.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select, update

from app.config import Settings
from app.db import init_db, make_engine, session_factory
from app.ingest import jobs
from app.models import Collection, Document, IngestJob, Namespace, utcnow_iso


@pytest.fixture
async def engine(tmp_path, monkeypatch):
    db_path = tmp_path / "tocmemex.db"
    engine = make_engine(db_path)
    await init_db(engine)
    settings = Settings(tocmemex_db_path=db_path, tocmemex_data_dir=tmp_path)
    monkeypatch.setattr(jobs, "get_settings", lambda: settings)

    # Never spawn a real ingest child from a test. ``pump`` (and the
    # reconcile that calls it) must not touch the pipeline or OCR.
    async def _fake_spawn(job, *args, **kwargs):
        return None

    monkeypatch.setattr(jobs, "_spawn", _fake_spawn)
    yield engine
    await engine.dispose()


def _file(tmp_path: Path, name: str = "note.md") -> Path:
    path = tmp_path / name
    path.write_text("# 标题\n\n正文内容足够长，用来测试。", encoding="utf-8")
    return path


async def _seed_document(engine, *, doc_id: str, content_hash: str, status: str = "ready"):
    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(
            Document(
                id=doc_id,
                namespace_id="shared",
                title="已有文档",
                filename="a.md",
                file_path="a.md",
                content_hash=content_hash,
                status=status,
            )
        )
        await db.commit()


@pytest.mark.asyncio
async def test_create_job_starts_queued_with_actor(engine, tmp_path):
    path = _file(tmp_path)
    job = await jobs.create_job(
        engine, file_path=path, actor="claude-code", source="mcp", session_id="s1"
    )
    assert job["status"] == "queued"
    assert job["actor"] == "claude-code"
    assert job["source"] == "mcp"
    assert job["session_id"] == "s1"
    assert job["doc_id"] is None


@pytest.mark.asyncio
async def test_claim_is_atomic_and_respects_concurrency(engine, tmp_path, monkeypatch):
    settings = Settings(
        tocmemex_db_path=tmp_path / "tocmemex.db",
        tocmemex_data_dir=tmp_path,
        tocmemex_ingest_concurrency=2,
    )
    monkeypatch.setattr(jobs, "get_settings", lambda: settings)
    for i in range(5):
        await jobs.create_job(engine, file_path=_file(tmp_path, f"n{i}.md"))

    first = await jobs.claim(engine)
    assert len(first) == 2
    # Cap is already full; a second claim must not exceed it.
    assert await jobs.claim(engine) == []


@pytest.mark.asyncio
async def test_claim_never_hands_one_job_to_two_callers(engine, tmp_path):
    await jobs.create_job(engine, file_path=_file(tmp_path))
    claimed = await jobs.claim(engine)
    assert len(claimed) == 1
    job_id = claimed[0].id
    async with session_factory(engine)() as db:
        row = await db.get(IngestJob, job_id)
        assert row.status == "parsing"


@pytest.mark.asyncio
async def test_reconcile_adopts_a_ready_document(engine, tmp_path):
    path = _file(tmp_path)
    job = await jobs.create_job(engine, file_path=path)
    await jobs.claim(engine)
    await _seed_document(engine, doc_id="d1", content_hash="h1", status="ready")
    async with session_factory(engine)() as db:
        await db.execute(
            update(IngestJob).where(IngestJob.id == job["id"]).values(doc_id="d1")
        )
        await db.commit()

    counts = await jobs.reconcile_jobs(engine)
    assert counts["recovered"] == 1
    got = await jobs.get_job(engine, job["id"])
    assert got["status"] == "ready"
    assert got["doc_id"] == "d1"


@pytest.mark.asyncio
async def test_reconcile_fails_a_dead_child(engine, tmp_path):
    job = await jobs.create_job(engine, file_path=_file(tmp_path))
    await jobs.claim(engine)
    # Backdate the heartbeat and point at a pid that cannot be alive.
    async with session_factory(engine)() as db:
        await db.execute(
            update(IngestJob)
            .where(IngestJob.id == job["id"])
            .values(child_pid=999_999_999, heartbeat_at="2000-01-01T00:00:00+00:00")
        )
        await db.commit()

    counts = await jobs.reconcile_jobs(engine)
    assert counts["failed"] == 1
    got = await jobs.get_job(engine, job["id"])
    assert got["status"] == "failed"
    assert "content_hash" in got["error"]


@pytest.mark.asyncio
async def test_reconcile_keeps_a_queued_job_for_pump(engine, tmp_path):
    job = await jobs.create_job(engine, file_path=_file(tmp_path))
    counts = await jobs.reconcile_jobs(engine)
    # Counted as queued, not failed: an unclaimed job must not be discarded.
    assert counts["queued"] == 1
    assert counts["failed"] == 0
    # reconcile ends with pump, so the job then starts (fake spawn here).
    got = await jobs.get_job(engine, job["id"])
    assert got["status"] in {"queued", "parsing"}


@pytest.mark.asyncio
async def test_reconcile_leaves_a_heartbeating_child_alone(engine, tmp_path):
    job = await jobs.create_job(engine, file_path=_file(tmp_path))
    await jobs.claim(engine)
    async with session_factory(engine)() as db:
        await db.execute(
            update(IngestJob)
            .where(IngestJob.id == job["id"])
            .values(heartbeat_at=utcnow_iso())
        )
        await db.commit()
    counts = await jobs.reconcile_jobs(engine)
    assert counts["alive"] == 1
    got = await jobs.get_job(engine, job["id"])
    assert got["status"] == "parsing"


@pytest.mark.asyncio
async def test_upload_record_maps_camel_case(engine, tmp_path):
    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        db.add(
            Collection(id="col1", name="标准资料", name_key="标准资料")
        )
        await db.commit()
    job = await jobs.create_job(
        engine, file_path=_file(tmp_path), collection_id="col1", ingest_note="备注"
    )
    record = jobs.upload_record(job)
    assert record["id"] == job["id"]
    assert record["collectionId"] == "col1"
    assert record["ingestNote"] == "备注"
    assert record["status"] == "queued"
    assert record["similarDocuments"] == []


@pytest.mark.asyncio
async def test_list_jobs_filters_by_status(engine, tmp_path):
    a = await jobs.create_job(engine, file_path=_file(tmp_path, "a.md"))
    b = await jobs.create_job(engine, file_path=_file(tmp_path, "b.md"))
    await jobs.claim(engine, limit=1)  # only the oldest becomes parsing
    queued = await jobs.list_jobs(engine, statuses=("queued",))
    parsing = await jobs.list_jobs(engine, statuses=("parsing",))
    assert {row["id"] for row in queued} == {b["id"]}
    assert {row["id"] for row in parsing} == {a["id"]}


def test_resolve_ingest_path_allows_data_dir(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    inside = data_dir / "a.md"
    inside.write_text("x", encoding="utf-8")
    assert jobs.resolve_ingest_path(inside, data_dir=data_dir) == inside.resolve()


def test_resolve_ingest_path_rejects_outside(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    outside = tmp_path / "secret.md"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="允许的入库目录"):
        jobs.resolve_ingest_path(outside, data_dir=data_dir)


def test_resolve_ingest_path_rejects_missing(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with pytest.raises(ValueError, match="不存在"):
        jobs.resolve_ingest_path(data_dir / "nope.md", data_dir=data_dir)


def test_resolve_ingest_path_extra_roots(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    extra = tmp_path / "workspace"
    extra.mkdir()
    target = extra / "a.md"
    target.write_text("x", encoding="utf-8")
    assert (
        jobs.resolve_ingest_path(target, data_dir=data_dir, extra_roots=[extra])
        == target.resolve()
    )


@pytest.mark.asyncio
async def test_job_table_is_created_on_an_existing_database(tmp_path):
    """An older database must gain ``ingest_jobs`` without a manual step."""
    db_path = tmp_path / "tocmemex.db"
    engine = make_engine(db_path)
    await init_db(engine)
    await engine.dispose()

    # Second init on the same file is the upgrade path a real install takes.
    engine = make_engine(db_path)
    await init_db(engine)
    async with session_factory(engine)() as db:
        rows = (await db.execute(select(IngestJob))).scalars().all()
        assert rows == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_status_survives_a_restart_via_the_database(tmp_path, monkeypatch):
    """A GET by job id must answer a terminal state after a restart.

    The old design kept this in ``app.state.uploads`` (memory); a restart
    answered 404 while the document could be ``ready``. With the job row in
    the database, a brand-new engine over the same file still reads it.
    """
    db_path = tmp_path / "tocmemex.db"
    data_dir = tmp_path
    settings = Settings(tocmemex_db_path=db_path, tocmemex_data_dir=data_dir)
    monkeypatch.setattr(jobs, "get_settings", lambda: settings)

    async def _no_spawn(job, *args, **kwargs):
        return None

    monkeypatch.setattr(jobs, "_spawn", _no_spawn)

    engine = make_engine(db_path)
    await init_db(engine)
    job = await jobs.create_job(engine, file_path=_file(tmp_path), actor="claude-code")
    await jobs.finish_job(engine, job["id"], status="ready", doc_id=None)
    await engine.dispose()

    # Simulate a fresh process: new engine over the same file.
    engine = make_engine(db_path)
    got = await jobs.get_job(engine, job["id"])
    assert got is not None
    assert got["status"] == "ready"
    await engine.dispose()
