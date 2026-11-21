"""Patrol signals (docs/m4a-quality-signals.md).

One read-only report joins quality groups, search misses, usage heat,
pending proposals, automatic rollbacks and errata.
"""

from __future__ import annotations

import json

import pytest

from app.curation import tools
from app.curation.signals import collect_signals
from app.db import init_db, make_engine, session_factory
from app.index.activity import Erratum, Task, UsageLog, usage_log_write
from app.models import (
    Chunk,
    CurationJob,
    CurationOutcome,
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
                page_start=1,
                page_end=1,
            )
        )
        await db.flush()
        db.add(
            Chunk(
                doc_id="d1",
                node_id="d1:h:1",
                chunk_index=0,
                content="短",
                heading_path="1 温度试验方法",
                page_start=1,
                page_end=1,
            )
        )
        job = CurationJob(
            id="job-1",
            issue_id="d1:duplicate-heading",
            doc_id="d1",
            kind="duplicate-heading",
            fingerprint="f" * 64,
            status="proposed",
        )
        db.add(job)
        await db.flush()
        db.add(
            CurationProposal(
                id="proposal-1",
                job_id="job-1",
                strategy="agent_toc",
                rationale="页码冲突，等待人工批准",
                payload_json='{"ops": []}',
                verdict="pending",
            )
        )
        db.add(
            CurationOutcome(
                doc_id="d1",
                kind="short-chunks",
                strategy="rule_merge",
                action="auto_rollback",
                verdict="worse",
                actor="curator",
                note="verification degraded: short chunks rose",
            )
        )
        db.add(
            Erratum(
                doc_id="d1",
                body="标题有误",
                proposed_by="agent-a",
                status="proposed",
            )
        )
        db.add(
            Task(
                id="task-review-1",
                title="纲目复审：温度试验方法",
                goal=json.dumps(
                    {"doc_id": "d1", "node_id": "d1:h:1", "reason": "页码漂移"}
                ),
                kind="gangmu-review",
                status="open",
                owner_agent="curator",
            )
        )
        # Two misses and five hits nobody marked used.
        await usage_log_write(db, "量子纠缠", "search", [])
        await usage_log_write(db, "量子纠缠", "web", [])
        for index in range(5):
            db.add(
                UsageLog(
                    query=f"温度 {index}",
                    tool="search",
                    doc_id="d1",
                    node_id="d1:h:1",
                    chunk_id=1,
                    rank=1,
                    used=0,
                )
            )
        await db.commit()
    return engine


@pytest.mark.asyncio
async def test_collect_signals_joins_every_source(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        report = await collect_signals(db, tmp_path, days=7, limit=20)
    kinds = {item["kind"]: item for item in report["quality_issues"]}
    assert "short-chunks" in kinds
    assert kinds["short-chunks"]["documents"] == 1

    assert {item["query"] for item in report["zero_hit_queries"]} == {"量子纠缠"}
    assert sum(item["misses"] for item in report["zero_hit_queries"]) == 2

    assert report["unused_heat"][0]["doc_id"] == "d1"
    assert report["unused_heat"][0]["searched"] == 5
    assert report["pending_proposals"][0]["proposal_id"] == "proposal-1"
    assert report["pending_proposals"][0]["doc_title"] == "温度试验方法"
    assert report["open_review_tasks"][0]["task_id"] == "task-review-1"
    assert report["open_review_tasks"][0]["doc_title"] == "温度试验方法"
    assert report["open_review_tasks"][0]["reason"] == "页码漂移"
    assert report["auto_rollbacks"][0]["strategy"] == "rule_merge"
    assert report["errata"] == {"proposed": 1}
    await engine.dispose()


@pytest.mark.asyncio
async def test_tool_returns_budget_and_signals(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        result = await tools.list_quality_signals(
            db, tmp_path, "d1", days=7, limit=5, calls_left=4, tokens_left=900
        )
    assert result["budget"] == {"calls_left": 4, "tokens_left": 900}
    assert result["zero_hit_queries"]
    await engine.dispose()
