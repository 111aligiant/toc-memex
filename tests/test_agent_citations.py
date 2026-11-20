from __future__ import annotations

import json

from agent_helpers import (
    call,
    execute_with_fake_llm,
    reply,
    seed_gjb,
)


def _final_results(run) -> list[dict]:
    return [
        json.loads(step.result_json)
        for step in run.steps
        if step.name == "final_answer" and step.type == "tool_result"
    ]


async def test_read_section_tool_returns_chunk_segments(tmp_path, monkeypatch):
    db_path = await seed_gjb(tmp_path)
    replies = [
        reply(call("read_section", {"doc_id": "doc1", "node_id": "doc1:h:4"})),
        reply(
            call(
                "final_answer",
                {
                    "answer": "见表1 [1]。",
                    "sources": [{"chunk_id": 1}],
                },
                call_id="call_2",
            )
        ),
    ]
    run = await execute_with_fake_llm(db_path, monkeypatch, replies)
    read_step = next(
        step
        for step in run.steps
        if step.name == "read_section" and step.type == "tool_result"
    )
    result = json.loads(read_step.result_json)
    assert [chunk["chunk_id"] for chunk in result["chunks"]] == [1, 2]
    assert "content" in result
    assert result["doc_title"]
    assert result["chunks"][1]["page_end"] == 7
    assert result["chunks"][1]["heading_path"].endswith("4.2 选择振动试验方法")


async def test_final_answer_records_used_chunks_without_quotes(tmp_path, monkeypatch):
    """Sources are a trace: chunk ids are enough, no verbatim quote needed."""
    db_path = await seed_gjb(tmp_path)
    replies = [
        reply(call("read_section", {"doc_id": "doc1", "node_id": "doc1:h:4"})),
        reply(
            call(
                "final_answer",
                {
                    "answer": "先分析技术文件 [1]，再按表1选择方法 [2]。",
                    "sources": [{"chunk_id": 1}, {"chunk_id": 2}],
                },
                call_id="call_2",
            )
        ),
    ]
    run = await execute_with_fake_llm(db_path, monkeypatch, replies)
    assert run.status == "done", run.error
    citations = json.loads(run.citations_json)
    assert [item["ref"] for item in citations] == [1, 2]
    assert [item["chunk_id"] for item in citations] == [1, 2]
    # The console shows the chunk text in place of a model-copied quote.
    assert citations[0]["quote"].startswith("4.1 选择试验方法")
    assert citations[0]["content"].startswith("4.1 选择试验方法")


async def test_final_answer_accepts_answer_without_sources(tmp_path, monkeypatch):
    db_path = await seed_gjb(tmp_path)
    replies = [
        reply(call("read_section", {"doc_id": "doc1", "node_id": "doc1:h:4"})),
        reply(
            call("final_answer", {"answer": "按表1选择振动环境类别。"}, call_id="call_2")
        ),
    ]
    run = await execute_with_fake_llm(db_path, monkeypatch, replies)
    assert run.status == "done", run.error
    assert json.loads(run.citations_json) == []


async def test_unread_chunk_source_is_skipped_not_rejected(tmp_path, monkeypatch):
    """A wrong chunk id no longer fails the run; it just leaves no trace."""
    db_path = await seed_gjb(tmp_path)
    replies = [
        reply(call("read_section", {"doc_id": "doc1", "node_id": "doc1:h:4"})),
        reply(
            call(
                "final_answer",
                {
                    "answer": "一句话答案 [1]。",
                    "sources": [{"chunk_id": 999, "quote": "虚构的片段"}],
                },
                call_id="call_2",
            )
        ),
    ]
    run = await execute_with_fake_llm(db_path, monkeypatch, replies)
    assert run.status == "done", run.error
    assert _final_results(run)[0]["accepted"] is True
    assert json.loads(run.citations_json) == []


async def test_answer_without_reading_evidence_is_refused(tmp_path, monkeypatch):
    """The one remaining gate: an answer must rest on something read."""
    db_path = await seed_gjb(tmp_path)
    replies = [
        reply(call("final_answer", {"answer": "凭记忆回答。"}, call_id="call_1")),
        reply(
            call(
                "final_answer",
                {"answer": "档案中没有足够证据。", "insufficient": True},
                call_id="call_2",
            )
        ),
    ]
    run = await execute_with_fake_llm(db_path, monkeypatch, replies)
    results = _final_results(run)
    assert "还没有读过任何切片" in results[0]["error"]
    assert results[1]["accepted"] is True
    assert results[1]["insufficient"] is True
    assert run.status == "done", run.error


async def test_insufficient_answer_needs_no_evidence(tmp_path, monkeypatch):
    db_path = await seed_gjb(tmp_path)
    replies = [
        reply(
            call(
                "final_answer",
                {"answer": "档案中没有足够证据。", "insufficient": True},
                call_id="call_1",
            )
        ),
    ]
    run = await execute_with_fake_llm(db_path, monkeypatch, replies)
    assert run.status == "done", run.error
    assert json.loads(run.citations_json) == []


async def test_final_answer_attempts_are_capped_at_three(tmp_path, monkeypatch):
    """Malformed submissions (empty answer) cannot loop forever."""
    db_path = await seed_gjb(tmp_path)
    bad = {"answer": ""}
    replies = [
        reply(call("read_section", {"doc_id": "doc1", "node_id": "doc1:h:4"})),
        reply(call("final_answer", dict(bad), call_id="call_2")),
        reply(call("final_answer", dict(bad), call_id="call_3")),
        reply(call("final_answer", dict(bad), call_id="call_4")),
        reply(call("final_answer", dict(bad), call_id="call_5")),
    ]
    run = await execute_with_fake_llm(db_path, monkeypatch, replies)
    assert run.status == "failed"
    assert "提交尝试已用完" in run.error
    # The failure path must still persist what the run spent.
    assert json.loads(run.usage_json)["total"] == 15 * 5


async def test_runner_marks_every_cited_chunk(tmp_path, monkeypatch):
    from app.db import make_engine, session_factory
    from app.index.activity import UsageLog
    from sqlalchemy import select

    db_path = await seed_gjb(tmp_path)
    engine = make_engine(db_path)
    async with session_factory(engine)() as db:
        db.add_all(
            [
                UsageLog(
                    query="剪裁",
                    tool="search",
                    doc_id="doc1",
                    node_id="doc1:h:4",
                    chunk_id=1,
                    rank=1,
                    used=0,
                ),
                UsageLog(
                    query="剪裁",
                    tool="search",
                    doc_id="doc1",
                    node_id="doc1:h:4",
                    chunk_id=2,
                    rank=2,
                    used=0,
                ),
            ]
        )
        await db.commit()
    await engine.dispose()

    replies = [
        reply(call("read_section", {"doc_id": "doc1", "node_id": "doc1:h:4"})),
        reply(
            call(
                "final_answer",
                {
                    "answer": "先分析技术文件 [1]，再按表1选择方法 [2]。",
                    "sources": [{"chunk_id": 1}, {"chunk_id": 2}],
                },
                call_id="call_2",
            )
        ),
    ]
    run = await execute_with_fake_llm(db_path, monkeypatch, replies)
    assert run.status == "done", run.error

    engine = make_engine(db_path)
    async with session_factory(engine)() as db:
        rows = (
            await db.execute(select(UsageLog).order_by(UsageLog.chunk_id))
        ).scalars().all()
        assert [row.used for row in rows] == [1, 1]
    await engine.dispose()
