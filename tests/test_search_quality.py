"""Search-quality loop (docs/m2c-search-quality.md).

Zero-hit queries are recorded, grouped and replayed; the replay also rides on
closed-loop verification so a write that breaks relevance is rolled back.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.curation import tools
from app.curation import verify as verify_mod
from app.curation.ops import ProposalContext, apply_ops
from app.curation.search_quality import (
    candidate_pairs,
    candidate_queries_for_doc,
    replay_zero_hits,
    zero_hit_queries,
)
from app.db import init_db, make_engine, session_factory
from app.index.activity import UsageLog, usage_log_write
from app.models import (
    Chunk,
    CurationJob,
    CurationProposal,
    Document,
    Namespace,
    TocNode,
)


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
                title="温度试验方法",
                filename="a.pdf",
                file_path="a.pdf",
                content_hash="h",
                page_count=3,
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
                title="1 温度试验方法",
                heading_path="1 温度试验方法",
                page_start=2,
                page_end=2,
            )
        )
        await db.flush()
        chunk = Chunk(
            doc_id="d1",
            node_id="d1:h:1",
            chunk_index=0,
            content="温度试验方法。第一段内容。",
            heading_path="1 温度试验方法",
            page_start=2,
            page_end=2,
        )
        db.add(chunk)
        await db.flush()
        from app.index.fts import index_document

        doc = await db.get(Document, "d1")
        await index_document(
            db, doc, (await db.execute(select(Chunk).where(Chunk.doc_id == "d1"))).scalars().all()
        )
        await db.commit()
    return engine


# ── recording ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_zero_hit_writes_one_rank_zero_row(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        count = await usage_log_write(db, "量子纠缠", "search", [], actor_agent="agent-a")
        await db.commit()
        rows = (await db.execute(select(UsageLog))).scalars().all()
    assert count == 0
    assert len(rows) == 1
    assert rows[0].rank == 0
    assert rows[0].doc_id is None
    assert rows[0].chunk_id is None
    assert rows[0].query == "量子纠缠"
    await engine.dispose()


# ── report ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_zero_hit_queries_groups_and_windows(tmp_path):
    engine = await _seed(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    async with session_factory(engine)() as db:
        await usage_log_write(db, "量子纠缠", "search", [])
        await usage_log_write(db, "量子纠缠", "search", [])
        await usage_log_write(db, "量子纠缠", "web", [])
        db.add(UsageLog(query="很久以前", tool="search", rank=0, created_at=old))
        await db.commit()
        rows = await zero_hit_queries(db, days=7, limit=10)
    assert [(row["query"], row["tool"], row["misses"]) for row in rows] == [
        ("量子纠缠", "search", 2),
        ("量子纠缠", "web", 1),
    ]
    await engine.dispose()


# ── replay ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_replay_reports_recovered_and_still_zero(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        # Both were misses when recorded; only one can be answered now.
        await usage_log_write(db, "温度试验", "search", [])
        await usage_log_write(db, "量子纠缠", "search", [])
        await db.commit()
        report = await replay_zero_hits(db, days=7, limit=10, top_k=8)
    by_query = {item["query"]: item for item in report["queries"]}
    assert report["replayed"] == 2
    assert report["recovered"] == 1
    assert report["still_zero"] == 1
    assert by_query["温度试验"]["recovered"] is True
    assert by_query["温度试验"]["docs"][0]["doc_id"] == "d1"
    assert by_query["量子纠缠"]["recovered"] is False
    assert by_query["量子纠缠"]["hits"] == 0
    await engine.dispose()


# ── closing the loop: set_keywords reaches search ────────────────────


@pytest.mark.asyncio
async def test_set_keywords_makes_a_missed_query_find_the_document(tmp_path):
    """A keyword write is a real fix: doc_fts fallback answers the query."""
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        from app.index.fts import search_chunks

        assert await search_chunks(db, "低温", top_k=8) == []
        await usage_log_write(db, "低温", "search", [])
        await db.commit()

        doc = await db.get(Document, "d1")
        ctx = ProposalContext(db=db, doc=doc, pages={}, data_dir=None)
        outcome = await apply_ops(
            ctx,
            [{"op": "set_keywords", "keywords": "低温 寒冷"}],
            "proposal-keywords",
        )
        await db.commit()
        assert outcome["applied_ops"] == 1

        hits = await search_chunks(db, "低温", top_k=8)
        assert hits and hits[0]["doc_id"] == "d1"
        assert hits[0]["match_type"] == "doc"

        report = await replay_zero_hits(db, days=7, limit=10)
    item = report["queries"][0]
    assert item["query"] == "低温"
    assert item["recovered"] is True
    assert item["docs"][0]["doc_id"] == "d1"
    await engine.dispose()


# ── candidate mapping and jobs ───────────────────────────────────────


@pytest.mark.asyncio
async def test_candidate_pairs_need_partial_term_overlap(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        # Shares 温度 with the title (coverage 0.5) — a lead.
        await usage_log_write(db, "温度 冲击", "search", [])
        # Shares all of it — the document fallback already answers it.
        await usage_log_write(db, "温度试验", "search", [])
        # Shares nothing with the title.
        await usage_log_write(db, "量子纠缠", "search", [])
        await db.commit()

        pairs = await candidate_pairs(db, days=7)
        for_doc = await candidate_queries_for_doc(db, "d1", days=7)
    assert len(pairs) == 1
    assert pairs[0]["query"] == "温度 冲击"
    assert pairs[0]["doc_id"] == "d1"
    assert pairs[0]["shared"] == ["温度"]
    assert pairs[0]["coverage"] == 0.5
    assert for_doc == pairs
    await engine.dispose()


@pytest.mark.asyncio
async def test_agent_tool_reports_search_signals(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        await usage_log_write(db, "温度 冲击", "search", [])
        await db.commit()
        result = await tools.get_search_signals(
            db, tmp_path, "d1", calls_left=2, tokens_left=50
        )
    assert result["keywords"] in (None, "")
    assert result["candidate_queries"][0]["query"] == "温度 冲击"
    assert result["budget"] == {"calls_left": 2, "tokens_left": 50}
    await engine.dispose()


@pytest.mark.asyncio
async def test_defer_low_risk_keeps_keywords_pending(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        doc = await db.get(Document, "d1")
        ctx = ProposalContext(db=db, doc=doc, pages={}, data_dir=None)
        deferred = await apply_ops(
            ctx,
            [{"op": "set_keywords", "keywords": "低温"}],
            "proposal-defer",
            defer_low_risk=True,
        )
        await db.commit()
        assert deferred["applied_ops"] == 0
        assert deferred["pending_ops"] == 1
        assert deferred["results"][0]["gate"] == "pending_human"
        assert (await db.get(Document, "d1")).keywords in (None, "")

        # The human approval path (defer off) is unchanged.
        applied = await apply_ops(
            ctx, [{"op": "set_keywords", "keywords": "低温"}], "proposal-apply"
        )
        await db.commit()
        assert applied["applied_ops"] == 1
        assert (await db.get(Document, "d1")).keywords == "低温"
    await engine.dispose()


@pytest.mark.asyncio
async def test_zero_result_job_files_a_pending_keyword_proposal(tmp_path, monkeypatch):
    """The full loop: miss -> candidate job -> proposal -> human approve -> replay."""
    import json
    import time

    from app.curation import runner
    from app.curation.dispatch import Dispatcher
    from app.index.fts import search_chunks
    from app.llm.client import ChatReply

    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        await usage_log_write(db, "温度 冲击", "search", [])
        await db.commit()

        script = [
            ChatReply(
                content=None,
                tool_calls=[
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "propose_ops",
                            "arguments": json.dumps(
                                {
                                    "ops": [
                                        {"op": "set_keywords", "keywords": "温度 冲击"}
                                    ],
                                    "rationale": "文档覆盖温度试验",
                                },
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
                tokens={"prompt": 1, "completion": 1, "total": 2, "source": "api_usage"},
            ),
            ChatReply(
                content="已提案",
                tool_calls=[],
                tokens={"prompt": 1, "completion": 1, "total": 2, "source": "api_usage"},
            ),
        ]
        calls = {"n": 0}

        def fake_chat(messages, settings=None, **kwargs):
            reply = script[min(calls["n"], len(script) - 1)]
            calls["n"] += 1
            return reply

        monkeypatch.setattr("app.llm.agent.llm_chat", fake_chat)
        summary = {"applied": 0, "skipped": 0}
        results = await runner._run_zero_result_jobs(
            db,
            tmp_path,
            "run_zr",
            (object(), "endpoint", {"max_calls": 5}),
            verify=True,
            deadline=time.monotonic() + 60,
            summary=summary,
        )
        assert results[0]["query"] == "温度 冲击"
        assert results[0]["pending"] == 1

        job = (await db.execute(select(CurationJob))).scalars().one()
        assert job.kind == "zero-result-queries"
        assert job.status == "proposed"
        proposal = (await db.execute(select(CurationProposal))).scalars().one()
        assert proposal.verdict == "pending"
        assert (await db.get(Document, "d1")).keywords in (None, "")

        # A human approves; only then do the keywords change and the miss clear.
        dispatcher = Dispatcher(db, tmp_path, "d1")
        approval = await dispatcher.apply_proposal(proposal.id)
        await db.commit()
        assert approval["applied"] == 1
        assert (await db.get(Document, "d1")).keywords == "温度 冲击"
        hits = await search_chunks(db, "温度 冲击", top_k=8)
        assert hits and hits[0]["doc_id"] == "d1"
        report = await replay_zero_hits(db, days=7)
    assert report["recovered"] == 1
    await engine.dispose()


# ── verifier integration ─────────────────────────────────────────────

async def _context_with_history(db):
    chunk = (
        await db.execute(select(Chunk).where(Chunk.doc_id == "d1"))
    ).scalar_one()
    await usage_log_write(
        db,
        "温度试验",
        "search",
        [{"doc_id": "d1", "node_id": "d1:h:1", "chunk_id": chunk.id}],
    )
    await db.commit()
    doc = await db.get(Document, "d1")
    return ProposalContext(db=db, doc=doc, pages={}, data_dir=None)


@pytest.mark.asyncio
async def test_snapshot_metrics_replays_historical_queries(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        ctx = await _context_with_history(db)
        metrics = await verify_mod.snapshot_metrics(ctx)
    assert metrics["search"]["queries"] == 1
    assert metrics["search"]["hit_docs"] == 1
    assert metrics["search"]["mean_rank"] == 1.0
    await engine.dispose()


@pytest.mark.asyncio
async def test_search_metric_is_none_without_history(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        doc = await db.get(Document, "d1")
        ctx = ProposalContext(db=db, doc=doc, pages={}, data_dir=None)
        metrics = await verify_mod.snapshot_metrics(ctx)
    assert metrics["search"] is None
    await engine.dispose()


def _search(hit_docs: int, mean_rank: float) -> dict:
    return {"queries": 5, "hit_docs": hit_docs, "mean_rank": mean_rank, "top_k": 10}


def test_compare_metrics_worse_when_a_query_loses_the_document():
    result = verify_mod.compare_metrics(
        {"search": _search(5, 2.0)}, {"search": _search(4, 3.4)}
    )
    assert result["verdict"] == "worse"
    assert any("historical queries" in reason for reason in result["reasons"])
    assert result["deltas"]["searchHits"] == -1


def test_compare_metrics_worse_when_rank_falls():
    result = verify_mod.compare_metrics(
        {"search": _search(5, 2.0)}, {"search": _search(5, 4.5)}
    )
    assert result["verdict"] == "worse"
    assert any("replay rank" in reason for reason in result["reasons"])


def test_compare_metrics_tolerates_small_rank_movement():
    result = verify_mod.compare_metrics(
        {"search": _search(5, 2.0)}, {"search": _search(5, 2.8)}
    )
    assert result["verdict"] == "neutral"


def test_compare_metrics_better_when_replay_improves():
    result = verify_mod.compare_metrics(
        {"search": _search(3, 6.0)}, {"search": _search(4, 3.0)}
    )
    assert result["verdict"] == "better"
