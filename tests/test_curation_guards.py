from __future__ import annotations

import pytest
from sqlalchemy import select

from app.curation import llm_log
from app.curation import ops as ops_mod
from app.curation.dispatch import Dispatcher
from app.db import init_db, make_engine, session_factory
from app.models import AgentNote, Chunk, Document, Namespace, TocNode


# ── §10.4: since filter ─────────────────────────────────────────────────


def _call(data_dir, ts: str, doc_id: str = "d1"):
    llm_log.append_call(
        data_dir,
        call_site="test",
        endpoint="e",
        model="m",
        prompt="p",
        content="c",
        tokens={"prompt": 1, "completion": 1, "total": 2, "source": "api_usage"},
        elapsed_ms=1,
        doc_id=doc_id,
        log_mode="full",
    )
    # append_call stamps its own ts; rewrite the last line so the test can
    # control time without sleeping.
    path = llm_log.log_path(data_dir)
    lines = path.read_text(encoding="utf-8").splitlines()
    record = __import__("json").loads(lines[-1])
    record["ts"] = ts
    lines[-1] = __import__("json").dumps(record, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_since_filters_by_timestamp(tmp_path):
    data_dir = tmp_path / "data"
    _call(data_dir, "2026-11-17T07:00:00+00:00")
    _call(data_dir, "2026-11-17T09:00:00+00:00")
    _call(data_dir, "2026-11-17T11:00:00+00:00")

    all_calls = llm_log.read_calls(data_dir)
    assert len(all_calls) == 3

    recent = llm_log.read_calls(data_dir, since="2026-11-17T10:00:00+00:00")
    assert len(recent) == 1

    none = llm_log.read_calls(data_dir, since="2026-11-18T00:00:00+00:00")
    assert none == []


def test_since_respects_timezone_offsets(tmp_path):
    """07:00+08:00 is 23:00 the previous day UTC; a naive since means UTC."""
    data_dir = tmp_path / "data"
    _call(data_dir, "2026-11-16T23:00:00+00:00")
    # 2026-11-17T07:00+08:00 == 2026-11-16T23:00Z, so both forms select it.
    assert len(llm_log.read_calls(data_dir, since="2026-11-16T22:00:00+00:00")) == 1
    assert len(llm_log.read_calls(data_dir, since="2026-11-17T00:00:00+00:00")) == 0
    assert len(llm_log.read_calls(data_dir, since="2026-11-17T07:00:00+08:00")) == 1


def test_malformed_since_is_ignored_by_the_log_reader(tmp_path):
    """The API rejects a bad `since` with 400; the reader itself stays lenient."""
    data_dir = tmp_path / "data"
    _call(data_dir, "2026-11-17T07:00:00+00:00")
    assert len(llm_log.read_calls(data_dir, since="not-a-date")) == 1


# ── §11.4: correct_text needs usage feedback ────────────────────────────


async def _seed(tmp_path):
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
            TocNode(
                doc_id="d1", node_id="d1:h:1", level=1, heading_num="1",
                title="1 范围", heading_path="1 范围", page_start=0, page_end=0,
            )
        )
        await db.flush()
        db.add(
            Chunk(
                doc_id="d1", node_id="d1:h:1", chunk_index=0,
                content="温度应为 0.2 g/m3，注：O 与 0 混淆。",
                heading_path="1 范围", page_start=0, page_end=0,
            )
        )
        await db.commit()
    return engine


@pytest.mark.asyncio
async def test_correct_text_without_feedback_is_dropped(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        doc = await db.get(Document, "d1")
        ctx = await ops_mod.build_context(db, doc, tmp_path)
        chunk = (await ctx.chunks())[0]
        outcome = await ops_mod.apply_ops(
            ctx,
            [{"op": "correct_text", "chunk_id": chunk.id, "old": "O", "new": "0"}],
            "p1",
        )
        assert outcome["results"][0]["status"] == "dropped"
        assert "usage feedback" in outcome["results"][0]["reason"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_correct_text_with_an_erratum_is_allowed(tmp_path):
    from app.index.activity import Erratum

    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        chunk = (await db.execute(select(Chunk).where(Chunk.doc_id == "d1"))).scalars().one()
        db.add(
            Erratum(
                doc_id="d1", chunk_id=chunk.id, body="O 应为 0",
                proposed_by="agent-a", status="proposed",
            )
        )
        await db.commit()

        doc = await db.get(Document, "d1")
        ctx = await ops_mod.build_context(db, doc, tmp_path)
        outcome = await ops_mod.apply_ops(
            ctx,
            [{"op": "correct_text", "chunk_id": chunk.id, "old": "O", "new": "0"}],
            "p1",
        )
        assert outcome["applied_ops"] == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_correct_text_with_a_pitfall_note_is_allowed(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        db.add(
            AgentNote(
                author="agent-b", kind="pitfall", doc_id="d1",
                body="这里 OCR 把 0 认成了 O",
            )
        )
        await db.commit()
        doc = await db.get(Document, "d1")
        ctx = await ops_mod.build_context(db, doc, tmp_path)
        chunk = (await ctx.chunks())[0]
        outcome = await ops_mod.apply_ops(
            ctx,
            [{"op": "correct_text", "chunk_id": chunk.id, "old": "O", "new": "0"}],
            "p1",
        )
        assert outcome["applied_ops"] == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_rejected_erratum_does_not_unlock_correct_text(tmp_path):
    from app.index.activity import Erratum

    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        chunk = (await db.execute(select(Chunk).where(Chunk.doc_id == "d1"))).scalars().one()
        db.add(
            Erratum(
                doc_id="d1", chunk_id=chunk.id, body="误报",
                proposed_by="agent-a", status="rejected",
            )
        )
        await db.commit()
        doc = await db.get(Document, "d1")
        ctx = await ops_mod.build_context(db, doc, tmp_path)
        outcome = await ops_mod.apply_ops(
            ctx,
            [{"op": "correct_text", "chunk_id": chunk.id, "old": "O", "new": "0"}],
            "p1",
        )
        assert outcome["results"][0]["status"] == "dropped"
    await engine.dispose()


# ── §5.2: printed-TOC LLM as a tool ─────────────────────────────────────


@pytest.mark.asyncio
async def test_printed_toc_tool_reports_unavailable_without_llm(tmp_path, monkeypatch):
    monkeypatch.setenv("TOCMEMEX_LLM_API_KEY", "")
    monkeypatch.setenv("TOCMEMEX_LLM_MODEL", "")
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        dispatcher = Dispatcher(db, tmp_path, "d1")
        result = await dispatcher.dispatch("get_printed_toc_llm", {})
        assert result["available"] is False
        assert result["entries"] == []
        assert "budget" in result
    await engine.dispose()


@pytest.mark.asyncio
async def test_printed_toc_tool_is_registered_for_mcp_and_agent(tmp_path):
    from app.curation.dispatch import tool_schemas

    names = [schema["function"]["name"] for schema in tool_schemas()]
    assert "get_printed_toc_llm" in names
