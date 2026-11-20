from __future__ import annotations

import json

from agent_helpers import (
    call,
    execute_with_fake_llm,
    make_run,
    reply,
    seed,
    seed_gjb,
)
from sqlalchemy import select

from app.agent.models import AgentRun
from app.agent.runner import execute_run
from app.db import make_engine, session_factory
from app.models import AgentNote


async def _add_notes(db_path, notes: list[dict]) -> None:
    engine = make_engine(db_path)
    async with session_factory(engine)() as db:
        for note in notes:
            db.add(AgentNote(**note))
        await db.commit()
    await engine.dispose()


async def test_run_preloads_recent_notes(tmp_path, monkeypatch):
    db_path = await seed(tmp_path)
    await _add_notes(
        db_path,
        [
            {"author": "agent-a", "kind": "handoff", "body": f"交接事项 {i} 的具体说明", "doc_id": "doc1"}
            for i in range(3)
        ]
        + [
            {"author": "agent-b", "kind": "pitfall", "body": f"页码陷阱 {i} 的具体说明", "doc_id": "doc1"}
            for i in range(3)
        ]
        + [
            {"author": "agent-c", "kind": "note", "body": f"普通备注 {i} 的具体说明"}
            for i in range(4)
        ],
    )

    captured = {}
    engine, run_id = await make_run(db_path)

    def fake_chat(messages, settings=None, **kwargs):
        captured["messages"] = messages
        return reply(
            call(
                "final_answer",
                {
                    "answer": "档案中没有足够证据。",
                    "sources": [],
                    "insufficient": True,
                },
                call_id="call_1",
            )
        )

    monkeypatch.setattr("app.agent.runner.llm_chat", fake_chat)
    await execute_run(run_id, engine=engine, settings=object())
    await engine.dispose()

    user_text = captured["messages"][1].content
    assert "历史手记" in user_text
    assert user_text.count("- #") == 8
    assert "handoff" in user_text
    assert "pitfall" in user_text


async def test_list_notes_filters_by_doc_and_kind(tmp_path, monkeypatch):
    db_path = await seed_gjb(tmp_path)
    await _add_notes(
        db_path,
        [
            {"author": "agent-a", "kind": "pitfall", "body": "页码陷阱", "doc_id": "doc1"},
            {"author": "agent-a", "kind": "note", "body": "普通备注", "doc_id": "doc1"},
        ],
    )
    replies = [
        reply(
            call(
                "list_notes",
                {"doc_id": "doc1", "kind": "pitfall"},
                call_id="call_1",
            )
        ),
        reply(
            call(
                "final_answer",
                {
                    "answer": "档案中没有足够证据。",
                    "sources": [],
                    "insufficient": True,
                },
                call_id="call_2",
            )
        ),
    ]
    run = await execute_with_fake_llm(db_path, monkeypatch, replies)
    step = next(
        step
        for step in run.steps
        if step.name == "list_notes" and step.type == "tool_result"
    )
    result = json.loads(step.result_json)
    assert [note["kind"] for note in result] == ["pitfall"]


async def test_notes_off_still_allows_reading_notes(tmp_path, monkeypatch):
    from app.agent.surface import tool_schemas

    names = [schema["function"]["name"] for schema in await tool_schemas(False)]
    assert "add_note" not in names
    assert "list_notes" in names

    db_path = await seed_gjb(tmp_path)
    await _add_notes(
        db_path,
        [{"author": "agent-a", "kind": "handoff", "body": "只读手记", "doc_id": "doc1"}],
    )
    replies = [
        reply(call("list_notes", {"doc_id": "doc1"}, call_id="call_1")),
        reply(
            call(
                "final_answer",
                {
                    "answer": "档案中没有足够证据。",
                    "sources": [],
                    "insufficient": True,
                },
                call_id="call_2",
            )
        ),
    ]
    run = await execute_with_fake_llm(
        db_path, monkeypatch, replies, allow_notes=False
    )
    step = next(
        step
        for step in run.steps
        if step.name == "list_notes" and step.type == "tool_result"
    )
    result = json.loads(step.result_json)
    assert result[0]["body"] == "只读手记"


async def test_note_body_cannot_be_cited_as_evidence(tmp_path, monkeypatch):
    """Notes are clues, not evidence: a source pointing at one leaves no trace."""
    db_path = await seed_gjb(tmp_path)
    note_body = "手记里的独特结论：试验前必须核对夹具型号。"
    await _add_notes(
        db_path,
        [{"author": "agent-a", "kind": "pitfall", "body": note_body, "doc_id": "doc1"}],
    )
    replies = [
        reply(call("read_section", {"doc_id": "doc1", "node_id": "doc1:h:4"})),
        reply(
            call(
                "final_answer",
                {
                    "answer": "按手记的结论 [1]。",
                    "sources": [{"chunk_id": 999, "quote": note_body}],
                },
                call_id="call_2",
            )
        ),
    ]
    run = await execute_with_fake_llm(db_path, monkeypatch, replies)
    assert run.status == "done", run.error
    assert json.loads(run.citations_json) == []
