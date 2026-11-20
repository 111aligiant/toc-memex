from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agent.models import AgentRun, AgentStep
from app.agent.runner import execute_run
from app.db import init_db, make_engine, session_factory
from app.llm.client import ChatReply
from app.models import (
    AgentNote,
    AgentSetting,
    Chunk,
    Document,
    ModelEndpoint,
    Namespace,
    TocNode,
)
from app.web_api import create_app

from agent_helpers import call, load_run, make_run, reply, seed


@pytest.mark.asyncio
async def test_run_accepts_a_verbatim_citation(tmp_path, monkeypatch):
    db_path = await seed(tmp_path)
    engine, run_id = await make_run(db_path)
    replies = [
        reply(call("read_section", {"doc_id": "doc1", "node_id": "d:8.3"})),
        reply(
            call(
                "final_answer",
                {
                    "answer": "8.3 要求正弦振动，10–2000 Hz [1]，持续 15 分钟 [2]。",
                    "sources": [
                        {
                            "ref": 1,
                            "chunk_id": 1,
                            "quote": "8.3 振动试验应采用正弦振动，频率范围 10 Hz 至 2000 Hz。",
                        },
                        {
                            "ref": 2,
                            "chunk_id": 2,
                            "quote": "试验持续时间按表 8-1 执行，每个轴向 15 分钟。",
                        },
                    ],
                    "insufficient": False,
                },
                call_id="call_2",
            )
        ),
    ]

    def fake_chat(messages, settings=None, **kwargs):
        return replies.pop(0)

    monkeypatch.setattr("app.agent.runner.llm_chat", fake_chat)
    await execute_run(run_id, engine=engine, settings=object())

    run, steps = await load_run(engine, run_id)
    assert run.status == "done", run.error
    citations = json.loads(run.citations_json)
    assert [citation["ref"] for citation in citations] == [1, 2]
    assert citations[0]["heading_path"] == "8.3 振动试验"
    assert citations[0]["page_start"] == 5
    types = [step.type for step in steps]
    assert types[0] == "start"
    assert "llm" in types
    assert "tool_call" in types
    assert "tool_result" in types
    assert "answer" in types
    assert types[-1] == "done"
    await engine.dispose()


@pytest.mark.asyncio
async def test_budget_still_allows_one_final_answer_turn(tmp_path, monkeypatch):
    """The tool-call budget must not strand a run right after the last read."""
    db_path = await seed(tmp_path)
    engine, run_id = await make_run(db_path, max_calls=1)
    replies = [
        reply(call("read_section", {"doc_id": "doc1", "node_id": "d:8.3"})),
        reply(
            call(
                "final_answer",
                {
                    "answer": "要求正弦振动[1]。",
                    "sources": [
                        {"ref": 1, "chunk_id": 1, "quote": "8.3 振动试验应采用正弦振动"}
                    ],
                },
                call_id="call_2",
            )
        ),
    ]

    def fake_chat(messages, settings=None, **kwargs):
        return replies.pop(0)

    monkeypatch.setattr("app.agent.runner.llm_chat", fake_chat)
    await execute_run(run_id, engine=engine, settings=object())

    run, _steps = await load_run(engine, run_id)
    assert run.status == "done", run.error
    await engine.dispose()


@pytest.mark.asyncio
async def test_token_budget_warns_before_the_ceiling(tmp_path, monkeypatch):
    """Past 60% of the token budget the model is told to answer, not read."""
    db_path = await seed(tmp_path)
    # Each fake reply reports 15 tokens, so the third turn starts at 30/45.
    engine, run_id = await make_run(db_path, max_tokens=45)
    prompts: list[list[str]] = []
    replies = [
        reply(call("read_section", {"doc_id": "doc1", "node_id": "d:8.3"}, "c1")),
        reply(call("read_section", {"doc_id": "doc1", "node_id": "d:8.3"}, "c2")),
        reply(
            call(
                "final_answer",
                {
                    "answer": "要求正弦振动[1]。",
                    "sources": [
                        {"ref": 1, "chunk_id": 1, "quote": "8.3 振动试验应采用正弦振动"}
                    ],
                },
                call_id="c3",
            )
        ),
    ]

    def fake_chat(messages, settings=None, **kwargs):
        prompts.append([message.content or "" for message in messages])
        return replies.pop(0)

    monkeypatch.setattr("app.agent.runner.llm_chat", fake_chat)
    await execute_run(run_id, engine=engine, settings=object())

    run, _steps = await load_run(engine, run_id)
    assert run.status == "done", run.error
    assert json.loads(run.usage_json)["total"] == 45
    assert not any("token 预算已用掉" in text for text in prompts[1])
    assert any("token 预算已用掉" in text for text in prompts[2])
    await engine.dispose()


@pytest.mark.asyncio
async def test_truncated_turn_is_discarded_and_retried(tmp_path, monkeypatch):
    """finish_reason=length means the tool-call JSON was cut; do not execute it."""
    db_path = await seed(tmp_path)
    engine, run_id = await make_run(db_path)
    prompts: list[list[str]] = []
    replies = [
        reply(call("read_section", {"doc_id": "doc1", "node_id": "d:8.3"})),
        reply(call("final_answer", {}, call_id="call_2"), finish_reason="length"),
        reply(
            call(
                "final_answer",
                {"answer": "要求正弦振动 [1]。", "sources": [{"chunk_id": 1}]},
                call_id="call_3",
            )
        ),
    ]

    def fake_chat(messages, settings=None, **kwargs):
        prompts.append([message.content or "" for message in messages])
        return replies.pop(0)

    monkeypatch.setattr("app.agent.runner.llm_chat", fake_chat)
    await execute_run(run_id, engine=engine, settings=object())

    run, steps = await load_run(engine, run_id)
    assert run.status == "done", run.error
    assert any("max_tokens 上限" in text for text in prompts[2])
    notes = [step for step in steps if step.type == "info" and step.name == "truncated"]
    assert notes
    # The unusable submission never reached final_answer.
    empty_errors = [
        step
        for step in steps
        if step.type == "tool_result"
        and step.name == "final_answer"
        and step.result_json
        and "answer 不能为空" in step.result_json
    ]
    assert not empty_errors
    await engine.dispose()


@pytest.mark.asyncio
async def test_truncated_final_answer_arguments_are_reported(tmp_path, monkeypatch):
    """Without finish_reason, half a JSON document must still be diagnosed."""
    db_path = await seed(tmp_path)
    engine, run_id = await make_run(db_path)
    broken_call = {
        "id": "call_2",
        "type": "function",
        "function": {"name": "final_answer", "arguments": '{"answer": "结论：正弦振动'},
    }
    replies = [
        reply(call("read_section", {"doc_id": "doc1", "node_id": "d:8.3"})),
        reply(broken_call),
        reply(
            call(
                "final_answer",
                {"answer": "要求正弦振动 [1]。", "sources": [{"chunk_id": 1}]},
                call_id="call_3",
            )
        ),
    ]

    def fake_chat(messages, settings=None, **kwargs):
        return replies.pop(0)

    monkeypatch.setattr("app.agent.runner.llm_chat", fake_chat)
    await execute_run(run_id, engine=engine, settings=object())

    run, steps = await load_run(engine, run_id)
    assert run.status == "done", run.error
    results = [
        json.loads(step.result_json)
        for step in steps
        if step.type == "tool_result" and step.name == "final_answer"
    ]
    assert "完整 JSON" in results[0]["error"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_unknown_collection_falls_back_to_global_search(tmp_path, monkeypatch):
    db_path = await seed(tmp_path)
    engine, run_id = await make_run(db_path)
    replies = [
        reply(call("search", {"query": "振动", "collection": "DO-160G"})),
        reply(
            call(
                "final_answer",
                {"answer": "档案中没有足够证据。", "insufficient": True},
                call_id="call_2",
            )
        ),
    ]

    def fake_chat(messages, settings=None, **kwargs):
        return replies.pop(0)

    monkeypatch.setattr("app.agent.runner.llm_chat", fake_chat)
    await execute_run(run_id, engine=engine, settings=object())

    run, steps = await load_run(engine, run_id)
    assert run.status == "done", run.error
    search_step = next(
        step
        for step in steps
        if step.type == "tool_result" and step.name == "search"
    )
    result = json.loads(search_step.result_json)
    assert "error" not in result
    assert "已改为全库检索" in result["collection_note"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_answer_is_accepted_even_when_a_source_is_wrong(tmp_path, monkeypatch):
    """Citations no longer gate the answer; a bad source just leaves no trace."""
    db_path = await seed(tmp_path)
    engine, run_id = await make_run(db_path)
    replies = [
        reply(call("read_section", {"doc_id": "doc1", "node_id": "d:8.3"})),
        reply(
            call(
                "final_answer",
                {
                    "answer": "8.3 要求正弦振动 [1]。",
                    "sources": [{"chunk_id": 1, "quote": "这句话原文里没有"}],
                },
                call_id="call_2",
            )
        ),
    ]

    def fake_chat(messages, settings=None, **kwargs):
        return replies.pop(0)

    monkeypatch.setattr("app.agent.runner.llm_chat", fake_chat)
    await execute_run(run_id, engine=engine, settings=object())

    run, steps = await load_run(engine, run_id)
    assert run.status == "done", run.error
    citations = json.loads(run.citations_json)
    assert [citation["chunk_id"] for citation in citations] == [1]
    results = [
        json.loads(step.result_json)
        for step in steps
        if step.type == "tool_result" and step.name == "final_answer"
    ]
    assert results[0]["accepted"] is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_insufficient_evidence_refuses_without_citations(tmp_path, monkeypatch):
    db_path = await seed(tmp_path)
    engine, run_id = await make_run(db_path)
    replies = [
        reply(
            call(
                "final_answer",
                {
                    "answer": "档案中没有足够证据回答这个问题。",
                    "sources": [],
                    "insufficient": True,
                }
            )
        )
    ]

    def fake_chat(messages, settings=None, **kwargs):
        return replies.pop(0)

    monkeypatch.setattr("app.agent.runner.llm_chat", fake_chat)
    await execute_run(run_id, engine=engine, settings=object())

    run, steps = await load_run(engine, run_id)
    assert run.status == "done", run.error
    assert json.loads(run.citations_json) == []
    answer = next(step for step in steps if step.type == "answer")
    assert "没有足够证据" in answer.content
    await engine.dispose()


@pytest.mark.asyncio
async def test_notes_are_written_only_when_allowed(tmp_path, monkeypatch):
    db_path = await seed(tmp_path)
    engine, run_id = await make_run(db_path, allow_notes=True)
    replies = [
        reply(call("read_section", {"doc_id": "doc1", "node_id": "d:8.3"}, "call_1")),
        reply(
            call(
                "add_note",
                {
                    "body": "8.3 的持续时间以表 8-1 为准，别只看正文。",
                    "kind": "handoff",
                    "doc_id": "doc1",
                    "node_id": "d:8.3",
                },
                "call_2",
            )
        ),
        reply(
            call(
                "final_answer",
                {
                    "answer": "要求正弦振动[1]。",
                    "sources": [
                        {"ref": 1, "chunk_id": 1, "quote": "8.3 振动试验应采用正弦振动"}
                    ],
                },
                call_id="call_3",
            )
        ),
    ]

    def fake_chat(messages, settings=None, **kwargs):
        return replies.pop(0)

    monkeypatch.setattr("app.agent.runner.llm_chat", fake_chat)
    await execute_run(run_id, engine=engine, settings=object())

    run, steps = await load_run(engine, run_id)
    assert any(step.type == "note" for step in steps)
    async with session_factory(engine)() as db:
        notes = (
            (await db.execute(select(AgentNote))).scalars().all()
        )
        assert len(notes) == 1
        assert notes[0].session_id == run_id
        assert notes[0].kind == "handoff"
    await engine.dispose()


@pytest.mark.asyncio
async def test_add_note_is_refused_when_notes_are_off(tmp_path, monkeypatch):
    db_path = await seed(tmp_path)
    engine, run_id = await make_run(db_path, allow_notes=False)
    replies = [
        reply(call("read_section", {"doc_id": "doc1", "node_id": "d:8.3"}, "call_1")),
        reply(call("add_note", {"body": "不该写", "kind": "handoff"}, "call_2")),
        reply(
            call(
                "final_answer",
                {
                    "answer": "要求正弦振动[1]。",
                    "sources": [
                        {"ref": 1, "chunk_id": 1, "quote": "8.3 振动试验应采用正弦振动"}
                    ],
                },
                call_id="call_3",
            )
        ),
    ]

    def fake_chat(messages, settings=None, **kwargs):
        return replies.pop(0)

    monkeypatch.setattr("app.agent.runner.llm_chat", fake_chat)
    await execute_run(run_id, engine=engine, settings=object())

    async with session_factory(engine)() as db:
        notes = (
            (await db.execute(select(AgentNote))).scalars().all()
        )
        assert notes == []
    await engine.dispose()


def test_init_db_creates_agent_tables(tmp_path):
    import asyncio

    db_path = tmp_path / "tocmemex.db"
    engine = make_engine(db_path)

    async def run():
        await init_db(engine)
        await engine.dispose()

    asyncio.run(run())
    with sqlite3.connect(db_path) as conn:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "agent_runs" in names
    assert "agent_steps" in names


def test_post_run_completes_and_replays(tmp_path, monkeypatch):
    db_path = asyncio.run(seed(tmp_path))

    async def add_endpoint():
        engine = make_engine(db_path)
        async with session_factory(engine)() as db:
            db.add(
                ModelEndpoint(
                    name="fake",
                    base_url="http://127.0.0.1:9/v1",
                    api_key="test",
                    model="fake-model",
                    enabled=True,
                    is_default=True,
                )
            )
            bianxiu = ModelEndpoint(
                name="bianxiu-endpoint",
                base_url="http://127.0.0.1:9/v1",
                api_key="test",
                model="bianxiu-model",
                enabled=True,
                is_default=False,
            )
            db.add(bianxiu)
            await db.flush()
            # The console must use the 编修 agent's bound endpoint by default,
            # not the shared default endpoint.
            db.add(
                AgentSetting(
                    name="bianxiu",
                    config={"endpoint": bianxiu.id},
                    updated_at="2026-01-01T00:00:00Z",
                )
            )
            await db.commit()
        await engine.dispose()

    asyncio.run(add_endpoint())
    replies = [
        reply(call("read_section", {"doc_id": "doc1", "node_id": "d:8.3"})),
        reply(
            call(
                "final_answer",
                {
                    "answer": "要求正弦振动[1]。",
                    "sources": [
                        {"ref": 1, "chunk_id": 1, "quote": "8.3 振动试验应采用正弦振动"}
                    ],
                },
                call_id="call_2",
            )
        ),
    ]

    def fake_chat(messages, settings=None, **kwargs):
        return replies.pop(0)

    monkeypatch.setattr("app.agent.runner.llm_chat", fake_chat)
    app = create_app(db_path, tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/runs",
            json={
                "question": "8.3 振动要求？",
                "maxCalls": 4,
                "maxTokens": 2_000_000,
            },
        )
        assert response.status_code == 200, response.text
        created = response.json()["run"]
        assert created["maxCalls"] == 4
        assert created["maxTokens"] == 2_000_000
        assert created["endpoint"] == "bianxiu-endpoint"
        assert created["model"] == "bianxiu-model"
        run_id = created["id"]

        deadline = time.monotonic() + 15
        payload = None
        while time.monotonic() < deadline:
            payload = client.get(f"/api/runs/{run_id}").json()["run"]
            if payload["status"] in {"done", "failed"}:
                break
            time.sleep(0.1)
        assert payload is not None
        assert payload["status"] == "done", payload
        assert len(payload["citations"]) == 1
        assert payload["citations"][0]["doc_title"] == "振动试验规范"
        assert payload["steps"][-1]["type"] == "done"

        listed = client.get("/api/runs").json()["runs"]
        assert [row["id"] for row in listed] == [run_id]


def test_run_in_defaults_to_the_console_tool_budget():
    from app.web_api import RunIn

    assert RunIn(question="测试问题").maxCalls == 9999


def test_post_run_without_endpoint_returns_409(tmp_path, monkeypatch):
    db_path = tmp_path / "tocmemex.db"

    async def no_endpoint(session, name, base=None):
        return None, None, {}

    monkeypatch.setattr("app.llm.purpose.resolve_runtime", no_endpoint)
    app = create_app(db_path, tmp_path)
    with TestClient(app) as client:
        response = client.post("/api/runs", json={"question": "测试问题"})
        assert response.status_code == 409
        assert "模型端点" in response.json()["detail"]


# ── external MCP sessions ────────────────────────────────────────────


def _patch_mcp_session(
    monkeypatch,
    db_path: Path,
    session_id: str = "sess-test",
    client: str = "claude-code",
    data_dir: Path | None = None,
):
    from app import mcp_server
    from app.config import Settings

    monkeypatch.setattr(
        mcp_server,
        "get_settings",
        lambda: Settings(
            tocmemex_db_path=db_path,
            tocmemex_data_dir=data_dir or db_path.parent,
        ),
    )
    monkeypatch.setattr(mcp_server, "_session_id", lambda ctx: session_id)
    monkeypatch.setattr(mcp_server, "_client_name", lambda ctx: client)


@pytest.mark.asyncio
async def test_mcp_tool_call_opens_and_extends_external_run(tmp_path, monkeypatch):
    """A connection without run_start still shows up: the first retrieval
    call opens the run and every call appends the same two steps the
    built-in loop writes."""
    db_path = await seed(tmp_path)
    _patch_mcp_session(monkeypatch, db_path)
    from app import mcp_server

    first = await mcp_server.server.call_tool("search", {"query": "振动"})
    assert first.is_error is False
    second = await mcp_server.server.call_tool(
        "read_section", {"doc_id": "doc1", "node_id": "d:8.3"}
    )
    assert second.is_error is False

    engine = make_engine(db_path)
    async with session_factory(engine)() as db:
        runs = (await db.execute(select(AgentRun))).scalars().all()
        assert len(runs) == 1
        run = runs[0]
        assert run.source == "mcp"
        assert run.session_id == "sess-test"
        assert run.client == "claude-code"
        assert run.status == "running"
        assert run.question == "振动"
        steps = (
            (
                await db.execute(
                    select(AgentStep)
                    .where(AgentStep.run_id == run.id)
                    .order_by(AgentStep.seq)
                )
            )
            .scalars()
            .all()
        )
        assert [step.type for step in steps] == [
            "start",
            "tool_call",
            "tool_result",
            "tool_call",
            "tool_result",
        ]
        assert [step.name for step in steps[1:]] == [
            "search",
            "search",
            "read_section",
            "read_section",
        ]
        assert run.last_seen_at is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_mcp_overview_titles_the_auto_run_from_its_query(tmp_path, monkeypatch):
    """An external agent often opens with overview(); the run it creates
    should be named after the question, not the generic connection label."""
    db_path = await seed(tmp_path)
    _patch_mcp_session(monkeypatch, db_path)
    from app import mcp_server

    called = await mcp_server.server.call_tool("overview", {"query": "振动"})
    assert called.is_error is False

    engine = make_engine(db_path)
    async with session_factory(engine)() as db:
        runs = (await db.execute(select(AgentRun))).scalars().all()
        assert [run.question for run in runs] == ["振动"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_run_start_and_run_end_close_external_run(tmp_path, monkeypatch):
    db_path = await seed(tmp_path)
    _patch_mcp_session(monkeypatch, db_path)
    from app import mcp_server

    # An auto-run opened by a call before the agent declares its question.
    await mcp_server.server.call_tool("list_documents", {})
    started = await mcp_server.server.call_tool(
        "run_start", {"question": "8.3 振动要求是什么？"}
    )
    assert started.is_error is False
    await mcp_server.server.call_tool("search", {"query": "8.3 振动"})
    ended = await mcp_server.server.call_tool(
        "run_end", {"status": "done", "answer": "要求正弦振动。"}
    )
    assert ended.is_error is False

    engine = make_engine(db_path)
    async with session_factory(engine)() as db:
        runs = (
            (
                await db.execute(
                    select(AgentRun).order_by(AgentRun.created_at, AgentRun.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(runs) == 2
        # The earlier auto-run is closed when a named run starts.
        assert all(run.status == "done" for run in runs)
        named = next(run for run in runs if run.question == "8.3 振动要求是什么？")
        steps = (
            (
                await db.execute(
                    select(AgentStep)
                    .where(AgentStep.run_id == named.id)
                    .order_by(AgentStep.seq)
                )
            )
            .scalars()
            .all()
        )
        assert [step.type for step in steps] == [
            "start",
            "tool_call",
            "tool_result",
            "answer",
            "done",
        ]
        assert named.answer == "要求正弦振动。"
    await engine.dispose()


@pytest.mark.asyncio
async def test_mcp_read_is_tagged_with_connection_session(tmp_path, monkeypatch):
    db_path = await seed(tmp_path)
    _patch_mcp_session(monkeypatch, db_path)
    from app import mcp_server
    await mcp_server.server.call_tool(
        "read_section",
        {"doc_id": "doc1", "node_id": "d:8.3", "actor_agent": "claude-code"},
    )

    from sqlalchemy import text

    engine = make_engine(db_path)
    async with session_factory(engine)() as db:
        rows = (
            await db.execute(
                text("SELECT actor_agent, session_id FROM activities")
            )
        ).all()
        assert len(rows) == 1
        assert rows[0][0] == "claude-code"
        assert rows[0][1] == "sess-test"
    await engine.dispose()


@pytest.mark.asyncio
async def test_external_duplicate_call_replays_with_note(tmp_path, monkeypatch):
    """An external agent repeating itself gets the first result, not a rerun."""
    db_path = await seed(tmp_path)
    _patch_mcp_session(monkeypatch, db_path)
    from app import mcp_server

    calls = {"n": 0}
    original = mcp_server.scoped_search

    async def counting(*args, **kwargs):
        calls["n"] += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(mcp_server, "scoped_search", counting)

    first = await mcp_server.server.call_tool("search", {"query": "振动"})
    second = await mcp_server.server.call_tool("search", {"query": "振动"})
    assert first.is_error is False
    assert second.is_error is False
    assert calls["n"] == 1
    payload = json.loads(second.content[0].text)
    assert "note" in payload

    engine = make_engine(db_path)
    async with session_factory(engine)() as db:
        steps = (
            (
                await db.execute(
                    select(AgentStep).order_by(AgentStep.seq)
                )
            )
            .scalars()
            .all()
        )
        # The replay still shows up as a call in the console timeline.
        assert [step.type for step in steps] == [
            "start",
            "tool_call",
            "tool_result",
            "tool_call",
            "tool_result",
        ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_external_failed_call_can_be_retried(tmp_path, monkeypatch):
    db_path = await seed(tmp_path)
    _patch_mcp_session(monkeypatch, db_path)
    from app import mcp_server
    from mcp.server.mcpserver.exceptions import ToolError

    calls = {"n": 0}
    original = mcp_server._get_toc

    async def counting(*args, **kwargs):
        calls["n"] += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(mcp_server, "_get_toc", counting)

    for _ in range(2):
        with pytest.raises(ToolError):
            await mcp_server.server.call_tool("get_toc", {"doc_id": "missing"})
    # Failures are never cached: the real error reaches the agent twice.
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_external_read_resource_record_is_payload_dict(tmp_path, monkeypatch):
    from test_mcp_resources import _seed
    from app import mcp_server
    from app.agent.models import AgentStep
    from app.db import make_engine, session_factory
    from sqlalchemy import select

    engine, _ = await _seed(tmp_path)
    await engine.dispose()
    _patch_mcp_session(monkeypatch, tmp_path / "tocmemex.db", data_dir=tmp_path / "data")

    await mcp_server.server.call_tool(
        "read_resource", {"doc_id": "doc1", "resource_id": "tbl_1_0"}
    )
    await mcp_server.server.call_tool(
        "read_resource",
        {"doc_id": "doc1", "resource_id": "fig_1_0", "include_image": True},
    )

    engine = make_engine(tmp_path / "tocmemex.db")
    async with session_factory(engine)() as db:
        steps = (await db.execute(
            select(AgentStep).where(AgentStep.name == "read_resource")
            .order_by(AgentStep.seq)
        )).scalars().all()
    await engine.dispose()

    results = [json.loads(step.result_json) for step in steps if step.result_json]
    assert isinstance(results[0], dict) and results[0]["type"] == "table"
    assert isinstance(results[1], dict) and results[1]["type"] == "figure"
    assert results[1]["image_meta"]["mime"] == "image/jpeg"
    figure_step = [step for step in steps if step.type == "tool_result"][1]
    assert "base64" not in figure_step.result_json


@pytest.mark.asyncio
async def test_external_guard_resets_at_run_boundary(tmp_path, monkeypatch):
    db_path = await seed(tmp_path)
    _patch_mcp_session(monkeypatch, db_path)
    from app import mcp_server

    calls = {"n": 0}
    original = mcp_server.scoped_search

    async def counting(*args, **kwargs):
        calls["n"] += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(mcp_server, "scoped_search", counting)

    await mcp_server.server.call_tool("search", {"query": "振动"})
    await mcp_server.server.call_tool("run_start", {"question": "新任务"})
    await mcp_server.server.call_tool("search", {"query": "振动"})
    # A new run is new work: the same query must actually run again.
    assert calls["n"] == 2


def test_external_run_is_visible_through_web_api(tmp_path, monkeypatch):
    db_path = asyncio.run(seed(tmp_path))
    _patch_mcp_session(monkeypatch, db_path)
    from app import mcp_server
    asyncio.run(mcp_server.server.call_tool("search", {"query": "振动"}))
    asyncio.run(mcp_server.server.call_tool("run_start", {"question": "外部问题"}))
    asyncio.run(mcp_server.server.call_tool("run_end", {"answer": "外部答案"}))

    app = create_app(db_path, tmp_path)
    with TestClient(app) as client:
        runs = client.get("/api/runs").json()["runs"]
        # Auto-run closed by run_start, then the named run closed by run_end.
        assert len(runs) == 2
        assert {row["source"] for row in runs} == {"mcp"}
        assert {row["client"] for row in runs} == {"claude-code"}
        named = next(row for row in runs if row["question"] == "外部问题")
        detail = client.get(f"/api/runs/{named['id']}").json()["run"]
        assert detail["status"] == "done"
        assert detail["answer"] == "外部答案"
        assert [step["type"] for step in detail["steps"]] == ["start", "answer", "done"]


@pytest.mark.asyncio
async def test_runner_stops_after_cancel(tmp_path, monkeypatch):
    db_path = await seed(tmp_path)
    engine, run_id = await make_run(db_path)

    async def cancel_later():
        await asyncio.sleep(0.1)
        async with session_factory(engine)() as db:
            row = await db.get(AgentRun, run_id)
            row.status = "cancelled"
            row.error = "用户取消"
            await db.commit()

    task = asyncio.create_task(cancel_later())
    replies = [
        reply(call("read_section", {"doc_id": "doc1", "node_id": "d:8.3"})),
        reply(
            call(
                "final_answer",
                {
                    "answer": "要求正弦振动 [1]。",
                    "sources": [
                        {
                            "ref": 1,
                            "chunk_id": 1,
                            "quote": "8.3 振动试验应采用正弦振动",
                        }
                    ],
                },
                call_id="call_2",
            )
        ),
    ]

    def fake_chat(messages, settings=None, **kwargs):
        time.sleep(0.2)
        return replies.pop(0)

    monkeypatch.setattr("app.agent.runner.llm_chat", fake_chat)
    await execute_run(run_id, engine=engine, settings=object())
    await task

    run, _steps = await load_run(engine, run_id)
    assert run.status == "cancelled"
    assert run.answer is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_console_tools_include_mcp_retrieval_surface():
    from app.agent.surface import tool_schemas

    names = {s["function"]["name"] for s in await tool_schemas(True)}
    assert {"mark_used", "ask", "get_document", "list_collections",
            "memex_overview", "overview", "list_notes",
            "list_resources", "read_resource"} <= names
    assert "final_answer" in names


@pytest.mark.asyncio
async def test_dispatch_uses_mcp_call_tool(tmp_path, monkeypatch):
    from app import mcp_server
    from app.agent.tools import ToolContext, dispatch
    from app.db import make_engine, session_factory
    from agent_helpers import seed

    db_path = await seed(tmp_path)
    engine = make_engine(db_path)
    calls = []
    original = mcp_server.server.call_tool

    async def spy(name, arguments, context=None):
        calls.append(name)
        return await original(name, arguments, context)

    monkeypatch.setattr(mcp_server.server, "call_tool", spy)
    async with session_factory(engine)() as db:
        ctx = ToolContext(db=db, run_id="run", allow_notes=False)
        await dispatch(ctx, "search", {"query": "振动"})
    await engine.dispose()
    assert calls == ["search"]


@pytest.mark.asyncio
async def test_runner_prompt_uses_mcp_retrieval_instructions(tmp_path, monkeypatch):
    from agent_helpers import call, make_run, reply, seed
    from app.agent.instructions import RETRIEVAL_INSTRUCTIONS
    from app.agent.runner import execute_run

    db_path = await seed(tmp_path)
    engine, run_id = await make_run(db_path)
    captured = {}

    def fake_chat(messages, settings=None, **kwargs):
        captured["system"] = messages[0].content
        return reply(call("final_answer", {"answer": "无证据", "insufficient": True}))

    monkeypatch.setattr("app.agent.runner.llm_chat", fake_chat)
    await execute_run(run_id, engine=engine, settings=object())
    await engine.dispose()

    assert RETRIEVAL_INSTRUCTIONS in captured["system"]
    assert "mark_used(chunk_id=...)" in captured["system"]
    assert "final_answer" in captured["system"]


@pytest.mark.asyncio
async def test_console_tools_include_overview_and_shallow_toc():
    from app.agent.surface import tool_schemas

    schemas = {schema["function"]["name"]: schema for schema in await tool_schemas(True)}
    assert "overview" in schemas
    depth = schemas["get_toc"]["function"]["parameters"]["properties"]["depth"]
    assert depth["default"] == 2


def test_compaction_keeps_recent_tool_results_verbatim():
    from app.agent.runner import _compact_tool_messages
    from app.llm.client import ChatMessage

    payload = json.dumps(
        {
            "doc_id": "doc1",
            "doc_title": "振动试验规范",
            "node_id": "n1",
            "heading_path": "1 范围",
            "page_start": 0,
            "page_end": 1,
            "chunk_ids": [1, 2],
            "chunks": [{"chunk_id": 1, "text": "长文" * 800}],
        },
        ensure_ascii=False,
    )
    messages = [
        ChatMessage(
            role="tool",
            name="read_section",
            tool_call_id=f"c{index}",
            content=payload,
        )
        for index in range(6)
    ]
    changed = _compact_tool_messages(messages)
    assert changed == 2
    digest = json.loads(messages[0].content)
    assert digest["compacted"] is True
    assert digest["chunk_ids"] == [1, 2]
    assert messages[-1].content == payload


@pytest.mark.asyncio
async def test_overview_returns_documents_with_headings(tmp_path):
    from app.agent.tools import ToolContext, dispatch

    db_path = await seed(tmp_path)
    engine = make_engine(db_path)
    async with session_factory(engine)() as db:
        ctx = ToolContext(db=db, run_id="run", allow_notes=True)
        result = await dispatch(ctx, "overview", {})
    await engine.dispose()

    assert result["count"] == 1
    document = result["documents"][0]
    assert document["doc_id"] == "doc1"
    assert [heading["node_id"] for heading in document["headings"]] == ["d:8.3"]


@pytest.mark.asyncio
async def test_console_read_section_exposes_cursor_and_larger_cap():
    from app.agent.surface import tool_schemas

    read = next(
        schema
        for schema in await tool_schemas(False)
        if schema["function"]["name"] == "read_section"
    )
    props = read["function"]["parameters"]["properties"]
    assert props["cursor"]["type"] == "integer"
    assert props["max_chars"]["default"] == 8000


@pytest.mark.asyncio
async def test_console_read_section_pages_a_long_section(tmp_path):
    from app.agent.tools import ToolContext, dispatch

    db_path = await seed(tmp_path)
    engine = make_engine(db_path)
    long_text = "振动试验条件应逐条核对。" * 160
    async with session_factory(engine)() as db:
        chunk = (
            await db.execute(
                select(Chunk).where(
                    Chunk.doc_id == "doc1", Chunk.chunk_index == 0
                )
            )
        ).scalar_one()
        chunk.content = long_text
        await db.commit()

        ctx = ToolContext(db=db, run_id="cursor-test", allow_notes=False)
        first = await dispatch(
            ctx,
            "read_section",
            {"doc_id": "doc1", "node_id": "d:8.3", "max_chars": 500},
        )
        assert first["next_cursor"] == 500
        assert all(len(c["text"]) <= 500 for c in first["chunks"])

        second = await dispatch(
            ctx,
            "read_section",
            {
                "doc_id": "doc1",
                "node_id": "d:8.3",
                "max_chars": 500,
                "cursor": first["next_cursor"],
            },
        )
        assert second["next_cursor"] == 1000
        assert second["chunks"][0]["text"].startswith(long_text[500:520])

        last = await dispatch(
            ctx,
            "read_section",
            {
                "doc_id": "doc1",
                "node_id": "d:8.3",
                "max_chars": 500,
                "cursor": second["next_cursor"],
            },
        )
        assert last["next_cursor"] == 1500

        end = await dispatch(
            ctx,
            "read_section",
            {
                "doc_id": "doc1",
                "node_id": "d:8.3",
                "max_chars": 500,
                "cursor": last["next_cursor"],
            },
        )
        assert end["next_cursor"] is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_dispatch_replays_identical_successful_call(tmp_path):
    from app.agent.tools import ToolContext, dispatch

    db_path = await seed(tmp_path)
    engine = make_engine(db_path)
    args = {"doc_id": "doc1", "node_id": "d:8.3"}
    async with session_factory(engine)() as db:
        ctx = ToolContext(db=db, run_id="dup", allow_notes=False)
        first = await dispatch(ctx, "read_section", args)
        second = await dispatch(ctx, "read_section", args)
    await engine.dispose()

    assert "error" not in first
    assert "error" not in second
    assert second["chunk_ids"] == first["chunk_ids"]
    assert "note" in second  # says the tool was not run a second time


@pytest.mark.asyncio
async def test_dispatch_retries_a_failed_call(tmp_path):
    from app.agent.tools import ToolContext, dispatch

    db_path = await seed(tmp_path)
    engine = make_engine(db_path)
    args = {"doc_id": "doc1", "node_id": "d:missing"}
    async with session_factory(engine)() as db:
        ctx = ToolContext(db=db, run_id="retry", allow_notes=False)
        first = await dispatch(ctx, "read_section", args)
        second = await dispatch(ctx, "read_section", args)
    await engine.dispose()

    # The real error must reappear; the duplicate guard must not mask it.
    assert "error" in first
    assert "error" in second
    assert "重复" not in second["error"]


@pytest.mark.asyncio
async def test_pre_cancelled_run_is_not_started(tmp_path):
    db_path = await seed(tmp_path)
    engine, run_id = await make_run(db_path)
    async with session_factory(engine)() as db:
        run = await db.get(AgentRun, run_id)
        run.status = "cancelled"
        run.error = "用户取消"
        await db.commit()

    await execute_run(run_id, engine=engine, settings=object())

    run, _steps = await load_run(engine, run_id)
    assert run.status == "cancelled"
    assert run.answer is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_cancelled_run_is_not_marked_failed_on_late_error(tmp_path, monkeypatch):
    db_path = await seed(tmp_path)
    engine, run_id = await make_run(db_path)

    async def cancel_later():
        await asyncio.sleep(0.1)
        async with session_factory(engine)() as db:
            row = await db.get(AgentRun, run_id)
            row.status = "cancelled"
            row.error = "用户取消"
            await db.commit()

    task = asyncio.create_task(cancel_later())

    def fake_chat(messages, settings=None, **kwargs):
        time.sleep(0.2)
        return reply()

    monkeypatch.setattr("app.agent.runner.llm_chat", fake_chat)
    await execute_run(run_id, engine=engine, settings=object())
    await task

    run, _steps = await load_run(engine, run_id)
    assert run.status == "cancelled"
    await engine.dispose()


@pytest.mark.asyncio
async def test_cancelled_run_survives_a_late_exception(tmp_path, monkeypatch):
    db_path = await seed(tmp_path)
    engine, run_id = await make_run(db_path)

    async def cancel_later():
        await asyncio.sleep(0.1)
        async with session_factory(engine)() as db:
            row = await db.get(AgentRun, run_id)
            row.status = "cancelled"
            row.error = "用户取消"
            await db.commit()

    task = asyncio.create_task(cancel_later())

    def fake_chat(messages, settings=None, **kwargs):
        time.sleep(0.2)
        raise RuntimeError("boom")

    monkeypatch.setattr("app.agent.runner.llm_chat", fake_chat)
    await execute_run(run_id, engine=engine, settings=object())
    await task

    run, _steps = await load_run(engine, run_id)
    assert run.status == "cancelled"
    await engine.dispose()
