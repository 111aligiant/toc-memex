from __future__ import annotations

import pytest
from sqlalchemy import select

from app.curation import runner
from app.db import init_db, make_engine, session_factory
from app.models import Chunk, CurationJob, CurationProposal, Document, Namespace, TocNode


async def _seed(tmp_path):
    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(
            Document(
                id="d1",
                namespace_id="shared",
                title="GB/T 2423.3-2006 试验 Cab 恒定湿热",
                filename="a.pdf",
                file_path="a.pdf",
                content_hash="h",
                page_count=20,
                status="ready",
                summary="前言 本文开头是同一段前言……",
            )
        )
        await db.flush()
        db.add_all(
            [
                TocNode(
                    doc_id="d1", node_id="d1:h:前言", level=1, title="前言",
                    heading_path="前言", page_start=0, page_end=0,
                ),
                TocNode(
                    doc_id="d1", node_id="d1:h:1", level=1, heading_num="1", title="1 范围",
                    heading_path="1 范围", page_start=1, page_end=1,
                ),
                TocNode(
                    doc_id="d1", node_id="d1:h:2", level=1, heading_num="2", title="2 试验设备",
                    heading_path="2 试验设备", page_start=2, page_end=4,
                ),
            ]
        )
        await db.flush()
        db.add_all(
            [
                Chunk(
                    doc_id="d1", node_id="d1:h:1", chunk_index=0,
                    content="1 范围\n\n本文件规定了恒定湿热试验的方法。", heading_path="1 范围",
                    page_start=1, page_end=1,
                ),
                Chunk(
                    doc_id="d1", node_id="d1:h:2", chunk_index=1,
                    content="2 试验设备", heading_path="2 试验设备",
                    page_start=2, page_end=2,
                ),
                Chunk(
                    doc_id="d1", node_id="d1:h:2", chunk_index=2,
                    content="试验箱应能维持规定的温湿度。", heading_path="2 试验设备",
                    page_start=3, page_end=3,
                ),
            ]
        )
        await db.commit()
    return engine


@pytest.mark.asyncio
async def test_run_once_without_llm_leaves_summary_alone(tmp_path, monkeypatch):
    """Summaries are not generated (ADR-0002 §7.2 revised).

    An extractive summary is the standard's preamble boilerplate and a
    TOC-derived one only restates get_toc; both measured as noise. The field
    stays as-is until an LLM-written or hand-written summary exists.
    """
    monkeypatch.setenv("TOCMEMEX_LLM_API_KEY", "")
    monkeypatch.setenv("TOCMEMEX_LLM_BASE_URL", "")
    monkeypatch.setenv("TOCMEMEX_LLM_MODEL", "")
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        result = await runner.run_once(db, tmp_path, limit=5)
        assert result["run_id"].startswith("run_")
        assert result["documents"]

        doc = await db.get(Document, "d1")
        assert doc.summary == "前言 本文开头是同一段前言……"

        jobs = (await db.execute(select(CurationJob))).scalars().all()
        assert {job.kind for job in jobs} == set(runner.RULE_KINDS)
        assert "stale-summary" not in runner.RULE_KINDS
        proposals = (await db.execute(select(CurationProposal))).scalars().all()
        assert all(p.strategy != "rule_summary" for p in proposals)
    await engine.dispose()


@pytest.mark.asyncio
async def test_run_once_is_idempotent_for_same_fingerprint(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        await runner.run_once(db, tmp_path, limit=5)
        first_jobs = len((await db.execute(select(CurationJob))).scalars().all())
        second = await runner.run_once(db, tmp_path, limit=5)
        second_jobs = len((await db.execute(select(CurationJob))).scalars().all())
    assert second_jobs == first_jobs, "same fingerprint must not create new jobs"
    assert second["skipped"] >= 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_run_once_survives_a_failing_job(tmp_path, monkeypatch):
    engine = await _seed(tmp_path)
    calls = {"n": 0}
    original = runner.run_rule_strategies

    async def boom(db, doc, data_dir, job, run_id, **kwargs):
        calls["n"] += 1
        if job.kind == "missing-pages":
            raise RuntimeError("simulated failure")
        return await original(db, doc, data_dir, job, run_id, **kwargs)

    monkeypatch.setattr(runner, "run_rule_strategies", boom)
    async with session_factory(engine)() as db:
        result = await runner.run_once(db, tmp_path, limit=5)
    assert calls["n"] >= 1
    assert result["documents"], "a failed job must not abort the run"
    await engine.dispose()


def test_score_doc_weights_alerts_and_staleness():
    from types import SimpleNamespace
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    fresh = SimpleNamespace(updated_at=now.isoformat())
    old = SimpleNamespace(updated_at=(now - timedelta(days=60)).isoformat())
    alerts = [{"count": 3}]
    assert runner.score_doc(fresh, alerts, 0.0, now) == 9.0
    assert runner.score_doc(old, [], 0.0, now) == 1.0
    assert runner.score_doc(fresh, [], 2.0, now) == 4.0


@pytest.mark.asyncio
async def test_applied_fix_writes_a_snapshot_keyed_by_its_proposal(
    tmp_path, monkeypatch
):
    """Undo needs the snapshot to name the proposal, not the job.

    The runner used to pass ``job.id`` as the snapshot's proposal id while
    creating the proposal with a different uuid, so the pre-write state was
    written but could never be found from the proposal id.
    """
    from app.curation.snapshots import snapshot_for_proposal
    from app.curation.undo import undo_proposal

    monkeypatch.setenv("TOCMEMEX_LLM_API_KEY", "")
    monkeypatch.setenv("TOCMEMEX_LLM_BASE_URL", "")
    monkeypatch.setenv("TOCMEMEX_LLM_MODEL", "")
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        await runner.run_once(db, tmp_path, limit=5)
        proposals = (
            await db.execute(select(CurationProposal).where(CurationProposal.applied_ops > 0))
        ).scalars().all()
        assert proposals, "seed data should produce at least one applied fix"
        proposal = proposals[0]

        snapshot = await snapshot_for_proposal(db, proposal.id)
        assert snapshot is not None

        merged = (
            await db.execute(
                select(Chunk).where(Chunk.doc_id == "d1").order_by(Chunk.chunk_index)
            )
        ).scalars().all()
        assert len(merged) == 2, "the heading fragment should join its neighbour"

        result = await undo_proposal(db, tmp_path, proposal.id)
        await db.commit()
        assert result["mode"] == "snapshot"
        assert result["stepwise"] is True

        restored = (
            await db.execute(
                select(Chunk).where(Chunk.doc_id == "d1").order_by(Chunk.chunk_index)
            )
        ).scalars().all()
        assert [chunk.content for chunk in restored] == [
            "1 范围\n\n本文件规定了恒定湿热试验的方法。",
            "2 试验设备",
            "试验箱应能维持规定的温湿度。",
        ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_undo_without_snapshot_falls_back_to_baseline(tmp_path, monkeypatch):
    """A change written before snapshots existed still has an undo path.

    It is the adoption baseline (whole document), and the result says so
    (``mode="baseline"``, ``stepwise=False``) instead of pretending to be a
    surgical single-proposal undo.
    """
    from app.curation import baselines
    from app.curation.snapshots import CurationSnapshot
    from app.curation.undo import undo_proposal
    from app.models import CurationProposal

    monkeypatch.setenv("TOCMEMEX_LLM_API_KEY", "")
    monkeypatch.setenv("TOCMEMEX_LLM_BASE_URL", "")
    monkeypatch.setenv("TOCMEMEX_LLM_MODEL", "")
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        await baselines.create_baseline(db, "d1", reason="adoption")
        await db.commit()
        await runner.run_once(db, tmp_path, limit=5)

        proposal = (
            await db.execute(select(CurationProposal).where(CurationProposal.applied_ops > 0))
        ).scalars().first()
        assert proposal is not None

        # Simulate a legacy change: no snapshot row was written.
        snapshots = (await db.execute(select(CurationSnapshot))).scalars().all()
        for row in snapshots:
            await db.delete(row)
        await db.commit()

        result = await undo_proposal(db, tmp_path, proposal.id)
        await db.commit()
        assert result["mode"] == "baseline"
        assert result["stepwise"] is False
        assert result["baseline_id"] is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_repair_snapshots_restores_stepwise_undo_for_legacy_changes(
    tmp_path, monkeypatch
):
    """A legacy proposal can get its pre-write state from the baseline.

    Documents with exactly one applied proposal are the safe case: the
    adoption baseline is that proposal's pre-write state, so copying it
    turns a whole-document fallback into a real per-proposal undo.
    """
    from app.curation import baselines
    from app.curation.snapshots import (
        CurationSnapshot,
        snapshot_for_proposal,
    )
    from app.curation.undo import backfill_snapshots, undo_proposal
    from app.models import CurationProposal

    monkeypatch.setenv("TOCMEMEX_LLM_API_KEY", "")
    monkeypatch.setenv("TOCMEMEX_LLM_BASE_URL", "")
    monkeypatch.setenv("TOCMEMEX_LLM_MODEL", "")
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        await baselines.create_baseline(db, "d1", reason="adoption")
        await db.commit()
        await runner.run_once(db, tmp_path, limit=5)
        proposal = (
            await db.execute(select(CurationProposal).where(CurationProposal.applied_ops > 0))
        ).scalars().first()
        assert proposal is not None

        for row in (await db.execute(select(CurationSnapshot))).scalars().all():
            await db.delete(row)
        await db.commit()

        result = await backfill_snapshots(db)
        await db.commit()
        assert result["repaired"] == 1
        assert result["skipped"]["multi_proposal"] == 0
        assert await snapshot_for_proposal(db, proposal.id) is not None

        undo = await undo_proposal(db, tmp_path, proposal.id)
        await db.commit()
        assert undo["mode"] == "snapshot"
        assert undo["stepwise"] is True
    await engine.dispose()
