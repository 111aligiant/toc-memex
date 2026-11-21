from __future__ import annotations

import json

import httpx
import pytest

from app.config import Settings
from app.llm.agent import run_tool_loop
from app.llm.client import ChatMessage, llm_chat


def _settings(tmp_path) -> Settings:
    return Settings(
        tocmemex_llm_api_key="k",
        tocmemex_llm_model="m",
        tocmemex_llm_base_url="http://x/v1",
        tocmemex_data_dir=tmp_path / "data",
        tocmemex_llm_log="full",
    )


def _reply(message: dict, usage: dict | None = None):
    body = {"choices": [{"message": message}]}
    if usage:
        body["usage"] = usage

    class R:
        status_code = 200

        def json(self):
            return body

    return R()


def test_llm_chat_returns_structured_tool_calls(monkeypatch, tmp_path):
    captured = {}

    def fake_post(self, url, json=None, headers=None, timeout=None):
        captured["body"] = json
        return _reply(
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_quality", "arguments": "{}"},
                    }
                ],
            },
            usage={"prompt_tokens": 30, "completion_tokens": 5, "total_tokens": 35},
        )

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    settings = _settings(tmp_path)
    reply = llm_chat(
        [ChatMessage(role="user", content="hi")],
        settings,
        tools=[{"type": "function", "function": {"name": "get_quality"}}],
    )
    assert reply.tool_calls[0]["function"]["name"] == "get_quality"
    assert reply.tokens["source"] == "api_usage"
    assert captured["body"]["tools"][0]["function"]["name"] == "get_quality"
    # Logged to the JSONL with tool names and token source.
    log = (tmp_path / "data" / "llm" / "calls.jsonl").read_text(encoding="utf-8")
    record = json.loads(log.strip().splitlines()[-1])
    assert record["tokens"]["source"] == "api_usage"
    assert record["request"]["tools"] == ["get_quality"]


def test_chat_message_wire_shape_omits_empty_fields():
    wire = ChatMessage(role="tool", tool_call_id="c1", name="get_chunks", content="{}").to_wire()
    assert wire == {
        "role": "tool",
        "content": "{}",
        "tool_call_id": "c1",
        "name": "get_chunks",
    }
    assert "tool_calls" not in ChatMessage(role="user", content="x").to_wire()


@pytest.mark.asyncio
async def test_tool_loop_dispatches_and_stops(tmp_path, monkeypatch):
    """A scripted model: read quality, propose an op, then stop."""
    from app.curation.dispatch import Dispatcher
    from app.db import init_db, make_engine, session_factory
    from app.models import Document, Namespace

    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(
            Document(
                id="d1", namespace_id="shared", title="Demo", filename="a.pdf",
                file_path="a.pdf", content_hash="h", page_count=3, status="ready",
            )
        )
        await db.commit()

        script = [
            _reply({"content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "get_quality", "arguments": "{}"}}]}),
            _reply({"content": None, "tool_calls": [
                {"id": "c2", "type": "function",
                 "function": {"name": "propose_ops",
                              "arguments": json.dumps({"ops": [{"op": "set_summary", "summary": "新摘要"}], "rationale": "t"})}}]}),
            _reply({"content": "完成", "tool_calls": []}),
        ]
        calls = {"n": 0}

        def fake_chat(messages, settings=None, **kwargs):
            from app.llm.client import ChatReply

            reply = script[min(calls["n"], len(script) - 1)]
            calls["n"] += 1
            body = reply.json()
            message = body["choices"][0]["message"]
            return ChatReply(
                content=message.get("content"),
                tool_calls=message.get("tool_calls") or [],
                tokens={"prompt": 1, "completion": 1, "total": 2, "source": "api_usage"},
            )

        monkeypatch.setattr("app.llm.agent.llm_chat", fake_chat)
        dispatcher = Dispatcher(db, tmp_path, "d1")
        doc = await db.get(Document, "d1")
        result = await run_tool_loop(dispatcher, doc, max_calls=10, run_id="run_x")
        await db.commit()

        assert result["steps"] == 3
        assert result["applied"] == 1
        assert result["proposals"]
        assert (await db.get(Document, "d1")).summary == "新摘要"
    await engine.dispose()


@pytest.mark.asyncio
async def test_tool_loop_stops_on_budget(tmp_path, monkeypatch):
    from app.curation.dispatch import Budget, Dispatcher
    from app.db import init_db, make_engine, session_factory
    from app.llm.client import ChatReply
    from app.models import Document, Namespace

    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(
            Document(
                id="d1", namespace_id="shared", title="Demo", filename="a.pdf",
                file_path="a.pdf", content_hash="h", page_count=3, status="ready",
            )
        )
        await db.commit()

        def fake_chat(messages, settings=None, **kwargs):
            return ChatReply(
                content=None,
                tool_calls=[{"id": "c", "type": "function",
                             "function": {"name": "get_chunks", "arguments": "{}"}}],
                tokens={"prompt": 1, "completion": 1, "total": 2, "source": "api_usage"},
            )

        monkeypatch.setattr("app.llm.agent.llm_chat", fake_chat)
        dispatcher = Dispatcher(db, tmp_path, "d1", budget=Budget(calls_left=2, tokens_left=1000))
        doc = await db.get(Document, "d1")
        result = await run_tool_loop(dispatcher, doc, max_calls=10)
        assert result["calls_left"] == 0
        assert result["steps"] == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_curator_agent_resolves_the_db_default_endpoint(tmp_path, monkeypatch):
    """A library configured through the Web must actually run the agent.

    ``run_agent`` used ``llm_available()`` (env only). With the endpoint in
    ``model_endpoints`` — the normal case — every curator run silently fell
    back to rules and ``curator.tool_loop`` never appeared in the LLM log.
    """
    from app.curation import runner
    from app.db import init_db, make_engine, session_factory
    from app.models import CurationJob, Document, ModelEndpoint, Namespace

    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(
            Document(
                id="d1", namespace_id="shared", title="Demo", filename="a.pdf",
                file_path="a.pdf", content_hash="h", page_count=3, status="ready",
            )
        )
        await db.flush()
        db.add(
            ModelEndpoint(
                name="magpie", base_url="http://127.0.0.1:3425/v1",
                api_key="k", model="m", enabled=1, is_default=1,
            )
        )
        job = CurationJob(
            id="j1", issue_id="d1:short-chunks", doc_id="d1",
            kind="short-chunks", fingerprint="f",
        )
        db.add(job)
        await db.commit()
        doc = await db.get(Document, "d1")

        captured = {}

        async def fake_loop(dispatcher, document, **kwargs):
            captured.update(kwargs)
            return {
                "steps": 0, "applied": 0, "pending": 0,
                "proposals": [], "error": None, "calls_left": 20,
            }

        monkeypatch.setattr("app.llm.agent.run_tool_loop", fake_loop)
        result = await runner.run_agent(db, doc, tmp_path, job, "run_x")

        assert captured["settings"].tocmemex_llm_api_key == "k"
        assert captured["settings"].tocmemex_llm_model == "m"
        assert result["endpoint"] == "magpie"
        assert "context" in captured
    await engine.dispose()


# ── B8: message order + non-blocking LLM call ────────────────────────────


@pytest.mark.asyncio
async def test_tool_loop_orders_assistant_before_tool_messages(tmp_path, monkeypatch):
    """B8: the assistant(tool_calls) message must precede its tool results.

    The loop used to append each tool result first and the assistant turn only
    afterwards; OpenAI-style APIs reject that ordering (tool message without a
    preceding assistant tool_call).
    """
    from app.curation.dispatch import Dispatcher
    from app.db import init_db, make_engine, session_factory
    from app.llm.client import ChatReply
    from app.models import Document, Namespace

    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(
            Document(
                id="d1", namespace_id="shared", title="Demo", filename="a.pdf",
                file_path="a.pdf", content_hash="h", page_count=3, status="ready",
            )
        )
        await db.commit()

        seen_histories = []

        def fake_chat(messages, settings=None, **kwargs):
            # Snapshot the wire order the loop is about to send.
            seen_histories.append([(m.role, bool(m.tool_calls)) for m in messages])
            if len(seen_histories) == 1:
                return ChatReply(
                    content=None,
                    tool_calls=[{"id": "c1", "type": "function",
                                 "function": {"name": "get_quality", "arguments": "{}"}}],
                    tokens={"prompt": 1, "completion": 1, "total": 2},
                )
            return ChatReply(content="done", tool_calls=[], tokens={"total": 1})

        monkeypatch.setattr("app.llm.agent.llm_chat", fake_chat)
        dispatcher = Dispatcher(db, tmp_path, "d1")
        doc = await db.get(Document, "d1")
        await run_tool_loop(dispatcher, doc, max_calls=5)
        await db.commit()

        # Second request carries the appended turn: assistant(with tool_calls)
        # must come immediately before the tool result.
        second = seen_histories[1]
        roles = [r for r, _ in second]
        idx_assistant = next(
            i for i, (r, has_tc) in enumerate(second) if r == "assistant" and has_tc
        )
        idx_tool = roles.index("tool")
        assert idx_assistant < idx_tool, f"assistant must precede tool: {second}"
    await engine.dispose()


@pytest.mark.asyncio
async def test_tool_loop_runs_llm_chat_off_the_event_loop(tmp_path, monkeypatch):
    """B8: llm_chat is blocking; it must run in a worker thread, not on-loop."""
    import threading

    from app.curation.dispatch import Dispatcher
    from app.db import init_db, make_engine, session_factory
    from app.llm.client import ChatReply
    from app.models import Document, Namespace

    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(
            Document(
                id="d1", namespace_id="shared", title="Demo", filename="a.pdf",
                file_path="a.pdf", content_hash="h", page_count=3, status="ready",
            )
        )
        await db.commit()

        main_thread = threading.get_ident()
        seen = {}

        def fake_chat(messages, settings=None, **kwargs):
            seen["thread"] = threading.get_ident()
            return ChatReply(content="done", tool_calls=[], tokens={"total": 1})

        monkeypatch.setattr("app.llm.agent.llm_chat", fake_chat)
        dispatcher = Dispatcher(db, tmp_path, "d1")
        doc = await db.get(Document, "d1")
        await run_tool_loop(dispatcher, doc, max_calls=5)
        await db.commit()

        assert seen["thread"] != main_thread, "llm_chat must not block the event loop"
    await engine.dispose()
