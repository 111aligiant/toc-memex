"""Closed-loop verification (self-evolution L2 / docs/m1-verification-plan.md)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.curation import verify as verify_mod
from app.curation.ops import ProposalContext, apply_ops
from app.db import init_db, make_engine, session_factory
from app.index.activity import Lesson
from app.models import (
    Chunk,
    CurationJob,
    CurationOutcome,
    CurationProposal,
    Document,
    Namespace,
    TocNode,
)

PAGE = "1 试验方法\n第一段内容。第二段内容"


async def _seed(tmp_path, *, chunks=None):
    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(
            Document(
                id="d1",
                namespace_id="shared",
                title="试验方法",
                filename="a.pdf",
                file_path="a.pdf",
                content_hash="h",
                page_count=5,
                status="ready",
            )
        )
        await db.flush()
        db.add(
            TocNode(
                doc_id="d1",
                node_id="d1:h:1",
                level=1,
                heading_num="1",
                title="1 试验方法",
                heading_path="1 试验方法",
                page_start=2,
                page_end=2,
            )
        )
        await db.flush()
        db.add_all(
            chunks
            or [
                Chunk(
                    doc_id="d1",
                    node_id="d1:h:1",
                    chunk_index=0,
                    content="第一段内容。第二段内容",
                    heading_path="1 试验方法",
                    page_start=2,
                    page_end=2,
                )
            ]
        )
        await db.flush()
        from app.index.fts import index_document

        doc = await db.get(Document, "d1")
        await index_document(
            db,
            doc,
            (
                await db.execute(select(Chunk).where(Chunk.doc_id == "d1"))
            ).scalars().all(),
        )
        await db.commit()
    return engine


async def _context(db, pages):
    doc = await db.get(Document, "d1")
    return ProposalContext(db=db, doc=doc, pages=pages, data_dir=None)


# ── verdict logic ────────────────────────────────────────────────────


def _m(*, errors=0, ratio=None, short=0, heading=0, consistent=True):
    return {
        "toc": {"errors": errors, "warnings": 0},
        "pages": None if ratio is None else {"checked": 10, "on_page": int(ratio * 10), "ratio": ratio},
        "blocks": {"short": short, "heading_only": heading},
        "fts": {"chunks": 1, "fts_rows": 1 if consistent else 0, "consistent": consistent},
    }


def test_compare_metrics_neutral_when_unchanged():
    result = verify_mod.compare_metrics(_m(ratio=0.9), _m(ratio=0.9))
    assert result["verdict"] == "neutral"
    assert result["reasons"] == []


def test_compare_metrics_worse_on_toc_errors():
    result = verify_mod.compare_metrics(_m(errors=0), _m(errors=2))
    assert result["verdict"] == "worse"
    assert any("TOC errors" in reason for reason in result["reasons"])


def test_compare_metrics_worse_when_titles_leave_their_pages():
    result = verify_mod.compare_metrics(_m(ratio=0.9), _m(ratio=0.5))
    assert result["verdict"] == "worse"
    assert any("title-on-page" in reason for reason in result["reasons"])


def test_compare_metrics_tolerates_small_ratio_wobble():
    result = verify_mod.compare_metrics(_m(ratio=0.90), _m(ratio=0.89))
    assert result["verdict"] != "worse"


def test_compare_metrics_worse_when_short_or_heading_chunks_rise():
    assert verify_mod.compare_metrics(_m(short=1), _m(short=3))["verdict"] == "worse"
    assert verify_mod.compare_metrics(_m(heading=0), _m(heading=1))["verdict"] == "worse"


def test_compare_metrics_worse_when_fts_breaks():
    result = verify_mod.compare_metrics(_m(), _m(consistent=False))
    assert result["verdict"] == "worse"
    assert any("FTS" in reason for reason in result["reasons"])


def test_compare_metrics_better_on_improvement():
    result = verify_mod.compare_metrics(_m(short=3), _m(short=1))
    assert result["verdict"] == "better"


def test_compare_metrics_skips_missing_metrics():
    before = {"toc": None, "pages": None, "blocks": None, "fts": None}
    after = {"toc": {"errors": 0, "warnings": 0}, "pages": None, "blocks": None, "fts": None}
    assert verify_mod.compare_metrics(before, after)["verdict"] == "neutral"


# ── document metrics ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_snapshot_metrics_reports_all_verifiers(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        ctx = await _context(db, {2: PAGE})
        metrics = await verify_mod.snapshot_metrics(ctx)
    assert metrics["toc"]["errors"] == 0
    assert metrics["pages"]["checked"] == 1
    assert metrics["pages"]["ratio"] == 1.0
    assert metrics["blocks"]["short"] == 1
    assert metrics["fts"]["consistent"] is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_pages_metric_is_none_without_cached_pages(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        ctx = await _context(db, {})
        metrics = await verify_mod.snapshot_metrics(ctx)
    assert metrics["pages"] is None
    await engine.dispose()


# ── automatic rollback ───────────────────────────────────────────────


def _split_op():
    return [
        {
            "op": "split_chunk",
            "chunk_id": 1,
            "at": "。第二",
            "evidence": "。第二",
        }
    ]


@pytest.mark.asyncio
async def test_apply_ops_rolls_back_a_degrading_write(tmp_path):
    """Splitting one short slice into two shorter ones must not survive."""
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        ctx = await _context(db, {2: PAGE})
        outcome = await apply_ops(ctx, _split_op(), "p1")
        await db.commit()
        chunks = (
            await db.execute(select(Chunk).where(Chunk.doc_id == "d1"))
        ).scalars().all()
    assert outcome["applied_ops"] == 0
    assert outcome["verify"]["verdict"] == "worse"
    assert len(chunks) == 1, "the split must have been rolled back"
    assert chunks[0].content == "第一段内容。第二段内容"
    await engine.dispose()


@pytest.mark.asyncio
async def test_apply_ops_can_skip_verification(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        ctx = await _context(db, {2: PAGE})
        outcome = await apply_ops(ctx, _split_op(), "p1", verify=False)
        await db.commit()
        chunks = (
            await db.execute(select(Chunk).where(Chunk.doc_id == "d1"))
        ).scalars().all()
    assert outcome["applied_ops"] == 1
    assert outcome["verify"] is None
    assert len(chunks) == 2
    await engine.dispose()


# ── runner integration: ledger + negative lesson ─────────────────────


@pytest.mark.asyncio
async def test_rule_run_records_auto_rollback_and_lesson(tmp_path, monkeypatch):
    from app.curation import runner

    def fake_compare(before, after):
        return {
            "verdict": "worse",
            "reasons": ["forced degradation"],
            "deltas": {},
            "before": before,
            "after": after,
        }

    monkeypatch.setattr(verify_mod, "compare_metrics", fake_compare)
    engine = await _seed(
        tmp_path,
        chunks=[
            Chunk(
                doc_id="d1", node_id="d1:h:1", chunk_index=0,
                content="1 试验方法", heading_path="1 试验方法",
                page_start=2, page_end=2,
            ),
            Chunk(
                doc_id="d1", node_id="d1:h:1", chunk_index=1,
                content="试验箱应能维持规定的温湿度。", heading_path="1 试验方法",
                page_start=2, page_end=2,
            ),
        ],
    )
    async with session_factory(engine)() as db:
        doc = await db.get(Document, "d1")
        job = CurationJob(
            id="j1", issue_id="d1:short", doc_id="d1", kind="short-chunks",
            fingerprint="fp", status="queued",
        )
        db.add(job)
        await db.flush()
        result = await runner.run_rule_strategies(
            db, doc, tmp_path, job, run_id="run_test"
        )
        await db.commit()
        outcomes = (await db.execute(select(CurationOutcome))).scalars().all()
        lessons = (await db.execute(select(Lesson))).scalars().all()
        chunks = (
            await db.execute(select(Chunk).where(Chunk.doc_id == "d1"))
        ).scalars().all()
        assert job.status == "skipped"
        assert "auto rollback" in (job.last_error or "")
        assert result["auto_rolled_back"] is True
        assert len(chunks) == 2, "the merge must have been rolled back"
        rolled = [row for row in outcomes if row.action == "auto_rollback"]
        assert rolled and rolled[0].verdict == "worse"
        assert rolled[0].verify_json and "forced degradation" in rolled[0].verify_json
        assert any("auto-rollback" in (row.keywords or "") for row in lessons)
    await engine.dispose()


# ── observation window ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_settle_outcomes_marks_survived_and_regressed(tmp_path):
    from app.curation.outcomes import mark_regressed, record_outcome, settle_outcomes

    engine = await _seed(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    async with session_factory(engine)() as db:
        kept = await record_outcome(
            db, doc_id="d1", issue_id="a", action="applied", actor="curator",
            kind="short-chunks", strategy="rule_merge",
        )
        undone = await record_outcome(
            db, doc_id="d1", issue_id="b", action="applied", actor="curator",
            kind="short-chunks", strategy="rule_merge",
        )
        fresh = await record_outcome(
            db, doc_id="d1", issue_id="c", action="applied", actor="curator",
            kind="short-chunks", strategy="rule_merge",
        )
        kept.created_at = old
        undone.created_at = old
        await record_outcome(
            db, doc_id="d1", issue_id="b", action="auto_rollback", actor="curator",
            kind="short-chunks", strategy="rule_merge",
        )
        settled = await settle_outcomes(db)
        await db.commit()
    assert settled == 2
    assert kept.survival == "survived"
    assert undone.survival == "regressed"
    assert fresh.survival == "pending", "inside the window stays pending"
    await engine.dispose()


@pytest.mark.asyncio
async def test_mark_regressed_can_cover_the_whole_document(tmp_path):
    from app.curation.outcomes import mark_regressed, record_outcome

    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        first = await record_outcome(
            db, doc_id="d1", issue_id="a", action="applied", actor="curator"
        )
        second = await record_outcome(
            db, doc_id="d1", issue_id="b", action="applied", actor="curator"
        )
        await mark_regressed(db, "d1", None)
        await db.commit()
    assert first.survival == "regressed"
    assert second.survival == "regressed"
    await engine.dispose()


@pytest.mark.asyncio
async def test_dispatcher_degrades_to_auto_rollback(tmp_path, monkeypatch):
    """The agent/web path leaves the same ledger row as the rule path."""
    from app.curation.dispatch import Dispatcher

    monkeypatch.setattr(
        verify_mod,
        "compare_metrics",
        lambda before, after: {
            "verdict": "worse", "reasons": ["forced"], "deltas": {},
            "before": before, "after": after,
        },
    )
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        from app.curation import dispatch as dispatch_mod

        async def _cached_context(db_session, doc, data_dir):
            return ProposalContext(db=db_session, doc=doc, pages={2: PAGE}, data_dir=data_dir)

        monkeypatch.setattr(dispatch_mod, "build_context", _cached_context)
        dispatcher = Dispatcher(db, tmp_path, "d1")
        result = await dispatcher.propose_ops(_split_op(), "force a rollback")
        await db.commit()
        outcomes = (await db.execute(select(CurationOutcome))).scalars().all()
        proposals = (await db.execute(select(CurationProposal))).scalars().all()
    assert result["applied_ops"] == 0
    assert result["verify"]["verdict"] == "worse"
    assert any(row.action == "auto_rollback" for row in outcomes)
    assert proposals and proposals[0].verdict == "rejected"
    await engine.dispose()