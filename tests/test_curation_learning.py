from __future__ import annotations

import pytest
from sqlalchemy import select

from app.curation.outcomes import rank_strategies, strategies_for
from app.db import init_db, make_engine, session_factory
from app.index.activity import lesson_search, lesson_write
from app.models import CurationOutcome


async def _engine(tmp_path):
    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    return engine


# ── §11.1: strategies ranked by outcome ─────────────────────────────────


@pytest.mark.asyncio
async def test_rank_uses_cost_prior_when_there_is_no_evidence(tmp_path):
    engine = await _engine(tmp_path)
    async with session_factory(engine)() as db:
        ranked = await rank_strategies(db, "short-chunks")
    # Deterministic work before agent work while nothing has been measured.
    assert ranked.index("rule_merge") < ranked.index("agent_chunk")
    await engine.dispose()


@pytest.mark.asyncio
async def test_rank_demotes_a_strategy_that_makes_things_worse(tmp_path):
    engine = await _engine(tmp_path)
    async with session_factory(engine)() as db:
        for _ in range(4):
            db.add(
                CurationOutcome(
                    doc_id="d1", kind="short-chunks", strategy="rule_merge",
                    action="applied", verdict="worse", actor="curator",
                )
            )
        for _ in range(4):
            db.add(
                CurationOutcome(
                    doc_id="d1", kind="short-chunks", strategy="agent_chunk",
                    action="applied", verdict="better", actor="curator",
                )
            )
        await db.commit()
        ranked = await rank_strategies(db, "short-chunks")
    assert ranked.index("agent_chunk") < ranked.index("rule_merge")
    await engine.dispose()


@pytest.mark.asyncio
async def test_rejected_outcomes_do_not_count(tmp_path):
    """A rejected proposal never ran; it must not count against a strategy."""
    engine = await _engine(tmp_path)
    async with session_factory(engine)() as db:
        for _ in range(5):
            db.add(
                CurationOutcome(
                    doc_id="d1", kind="short-chunks", strategy="rule_merge",
                    action="rejected", verdict="worse", actor="human:web",
                )
            )
        await db.commit()
        ranked = await rank_strategies(db, "short-chunks")
    assert ranked.index("rule_merge") < ranked.index("agent_chunk")
    await engine.dispose()


def test_strategies_for_filters_by_kind():
    # A globally well-ranked strategy that cannot address this kind is dropped;
    # the rest keep their rank order.
    ranked = ["agent_toc", "rule_page_ranges", "rule_merge", "agent_chunk"]
    assert strategies_for("short-chunks", ranked) == ["rule_merge", "agent_chunk"]
    assert strategies_for("missing-pages", ranked) == ["agent_toc", "rule_page_ranges"]
    assert strategies_for("unknown-kind", ranked) == []


# ── §11.3: lesson consolidation ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_repeating_a_lesson_bumps_support_instead_of_duplicating(tmp_path):
    engine = await _engine(tmp_path)
    async with session_factory(engine)() as db:
        first = await lesson_write(
            db, trigger="short-chunks 文档的确定性修复",
            issue="12 份文档存在短切片", solution="rule_merge 能处理",
            author_agent="curator",
        )
        second = await lesson_write(
            db, trigger="short-chunks 文档的确定性修复",
            issue="12 份文档存在短切片", solution="rule_merge 能处理，效果稳定",
            author_agent="curator",
        )
        await db.commit()
        assert first.id == second.id
        assert second.support_count == 2
        # The solution is refreshed, not frozen at the first wording.
        assert "效果稳定" in second.solution
        rows = (await db.execute(select(type(first)))).scalars().all()
        assert len(rows) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_lesson_key_ignores_punctuation_and_whitespace(tmp_path):
    engine = await _engine(tmp_path)
    async with session_factory(engine)() as db:
        a = await lesson_write(
            db, trigger="短切片 修复", issue="存在短切片",
            solution="合并", author_agent="curator",
        )
        b = await lesson_write(
            db, trigger="短切片，修复。", issue="存在短切片。",
            solution="合并", author_agent="curator",
        )
        await db.commit()
        assert a.id == b.id
        assert b.support_count == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_different_lessons_stay_separate(tmp_path):
    engine = await _engine(tmp_path)
    async with session_factory(engine)() as db:
        a = await lesson_write(
            db, trigger="短切片", issue="短", solution="合并", author_agent="curator"
        )
        b = await lesson_write(
            db, trigger="缺页", issue="页码缺失", solution="扩展父区间",
            author_agent="curator",
        )
        await db.commit()
        assert a.id != b.id
    await engine.dispose()


@pytest.mark.asyncio
async def test_lesson_search_prefers_frequently_observed(tmp_path):
    engine = await _engine(tmp_path)
    async with session_factory(engine)() as db:
        for _ in range(3):
            await lesson_write(
                db, trigger="短切片 表格", issue="表格被拆散",
                solution="合并", author_agent="curator",
            )
        await lesson_write(
            db, trigger="短切片 标题", issue="标题碎片",
            solution="删除", author_agent="curator",
        )
        await db.commit()
        hits = await lesson_search(db, "短切片")
        assert hits[0].support_count == 3
    await engine.dispose()


@pytest.mark.asyncio
async def test_curator_run_writes_lessons(tmp_path):
    from app.curation import runner
    from app.index.activity import Lesson

    engine = await _engine(tmp_path)
    async with session_factory(engine)() as db:
        await runner.run_once(db, tmp_path, limit=1)
        lessons = (await db.execute(select(Lesson))).scalars().all()
        # An empty library produces no findings, so no lesson is invented.
        assert lessons == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_repeated_detector_lesson_suppresses_the_kind(tmp_path, monkeypatch):
    """A lesson saying "this detector keeps firing with nothing to fix"
    must change what the next run does — it used to be write-only."""
    from app.curation import runner
    from app.models import Namespace, Document, TocNode, Chunk

    monkeypatch.setenv("TOCMEMEX_LLM_API_KEY", "")
    monkeypatch.setenv("TOCMEMEX_LLM_BASE_URL", "")
    monkeypatch.setenv("TOCMEMEX_LLM_MODEL", "")
    engine = await _engine(tmp_path)
    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(
            Document(
                id="d1", namespace_id="shared", title="t", filename="a.pdf",
                file_path="a.pdf", content_hash="h", page_count=2, status="ready",
            )
        )
        db.add(
            TocNode(
                doc_id="d1", node_id="d1:h:1", level=1, title="1 范围",
                heading_path="1 范围", page_start=0, page_end=1,
            )
        )
        await db.flush()
        db.add(
            Chunk(
                doc_id="d1", node_id="d1:h:1", chunk_index=0,
                content="短切片", heading_path="1 范围",
                page_start=0, page_end=1,
            )
        )
        await db.commit()

        for _ in range(3):
            await lesson_write(
                db,
                trigger="short-chunks 告警反复出现但无可修项",
                issue="本轮所有文档的 short-chunks 检查全部没有可修项",
                solution="不要重复派发该 job",
                author_agent="curator",
                keywords="short-chunks,detector",
            )
        await db.commit()

        result = await runner.run_once(db, tmp_path, limit=5)

        short_jobs = [
            job for doc in result["documents"] for job in doc["jobs"]
            if job.get("kind") == "short-chunks"
        ]
        assert short_jobs
        assert all("lesson:" in (job.get("skipped") or "") for job in short_jobs)
    await engine.dispose()
