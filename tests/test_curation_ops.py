from __future__ import annotations

import pytest
from sqlalchemy import select

from app.curation import ops as ops_mod
from app.curation.dispatch import Dispatcher, DispatchError
from app.db import init_db, make_engine, session_factory
from app.models import Chunk, CurationProposal, Document, Namespace, TocNode


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
                title="Demo",
                filename="demo.pdf",
                file_path="demo.pdf",
                content_hash="h",
                page_count=10,
                status="ready",
                summary="old",
            )
        )
        await db.flush()
        db.add_all(
            [
                TocNode(
                    doc_id="d1", node_id="d1:h:1", level=1, heading_num="1",
                    title="1 范围", heading_path="1 范围", page_start=0, page_end=3,
                ),
                TocNode(
                    doc_id="d1", node_id="d1:h:1.1", parent_id="d1:h:1", level=2,
                    heading_num="1.1", title="1.1 目的", heading_path="1 > 1.1",
                    page_start=1, page_end=2,
                ),
            ]
        )
        await db.flush()
        db.add_all(
            [
                Chunk(
                    doc_id="d1", node_id="d1:h:1", chunk_index=0,
                    content="1 范围\n\n本文件规定了试验方法。", heading_path="1 范围",
                    page_start=0, page_end=1,
                ),
                Chunk(
                    doc_id="d1", node_id="d1:h:1", chunk_index=1,
                    content="适用于所有电工电子产品。", heading_path="1 范围",
                    page_start=1, page_end=1,
                ),
                Chunk(
                    doc_id="d1", node_id="d1:h:1.1", chunk_index=2,
                    content="1.1 目的\n\n说明试验的目的和适用范围。", heading_path="1 > 1.1",
                    page_start=1, page_end=2,
                ),
            ]
        )
        await db.commit()
    return engine


@pytest.mark.asyncio
async def test_merge_chunks_applies_and_renumbers(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        doc = await db.get(Document, "d1")
        ctx = await ops_mod.build_context(db, doc, tmp_path)
        chunks = await ctx.chunks()
        outcome = await ops_mod.apply_ops(
            ctx, [{"op": "merge_chunks", "chunk_ids": [chunks[0].id, chunks[1].id]}], "p1"
        )
        await db.commit()
        assert outcome["applied_ops"] == 1
        rows = (await db.execute(select(Chunk).where(Chunk.doc_id == "d1").order_by(Chunk.chunk_index))).scalars().all()
        assert [row.chunk_index for row in rows] == [0, 1]
        assert "本文件规定了试验方法" in rows[0].content
        assert "适用于所有电工电子产品" in rows[0].content
    await engine.dispose()


@pytest.mark.asyncio
async def test_merge_across_nodes_is_dropped(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        doc = await db.get(Document, "d1")
        ctx = await ops_mod.build_context(db, doc, tmp_path)
        chunks = await ctx.chunks()
        outcome = await ops_mod.apply_ops(
            ctx, [{"op": "merge_chunks", "chunk_ids": [chunks[0].id, chunks[2].id]}], "p1"
        )
        assert outcome["applied_ops"] == 0
        assert outcome["results"][0]["status"] == "dropped"
        assert "cross TOC nodes" in outcome["results"][0]["reason"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_set_page_without_evidence_is_dropped(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        doc = await db.get(Document, "d1")
        ctx = await ops_mod.build_context(db, doc, tmp_path)
        outcome = await ops_mod.apply_ops(
            ctx,
            [{"op": "set_page", "node_id": "d1:h:1.1", "page_start": 1, "page_end": 2,
              "evidence": "不存在的证据文本"}],
            "p1",
        )
        assert outcome["results"][0]["status"] == "dropped"
        assert "evidence" in outcome["results"][0]["reason"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_set_page_outside_parent_is_dropped(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        doc = await db.get(Document, "d1")
        ctx = await ops_mod.build_context(db, doc, tmp_path)
        outcome = await ops_mod.apply_ops(
            ctx,
            [{"op": "set_page", "node_id": "d1:h:1.1", "page_start": 1, "page_end": 9,
              "evidence": "1.1 目的"}],
            "p1",
        )
        assert outcome["results"][0]["status"] == "dropped"
        assert "outside parent" in outcome["results"][0]["reason"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_set_page_without_ocr_cache_cannot_pass_grounding(tmp_path):
    """No OCR cache means no page text, so grounding must fail rather than
    trust the claim. This is the honest-unknown path."""
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        doc = await db.get(Document, "d1")
        ctx = await ops_mod.build_context(db, doc, tmp_path)
        assert ctx.pages == {}
        outcome = await ops_mod.apply_ops(
            ctx,
            [{"op": "set_page", "node_id": "d1:h:1.1", "page_start": 1, "page_end": 2,
              "evidence": "1.1 目的"}],
            "p1",
        )
        assert outcome["results"][0]["status"] == "dropped"
    await engine.dispose()


@pytest.mark.asyncio
async def test_unknown_op_is_refused(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        doc = await db.get(Document, "d1")
        ctx = await ops_mod.build_context(db, doc, tmp_path)
        outcome = await ops_mod.apply_ops(
            ctx, [{"op": "rewrite_original_file"}], "p1"
        )
        assert outcome["results"][0]["status"] == "dropped"
        assert "not allowed" in outcome["results"][0]["reason"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_set_summary_applies_and_is_l1(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        doc = await db.get(Document, "d1")
        ctx = await ops_mod.build_context(db, doc, tmp_path)
        outcome = await ops_mod.apply_ops(
            ctx, [{"op": "set_summary", "summary": "新摘要"}], "p1"
        )
        await db.commit()
        assert outcome["applied_ops"] == 1
        assert (await db.get(Document, "d1")).summary == "新摘要"
    await engine.dispose()


@pytest.mark.asyncio
async def test_dispatcher_refuses_cross_document_calls(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        dispatcher = Dispatcher(db, tmp_path, "d1")
        with pytest.raises(DispatchError):
            await dispatcher.dispatch("get_chunks", {"doc_id": "other"})
    await engine.dispose()


@pytest.mark.asyncio
async def test_dispatcher_budget_exhausts(tmp_path):
    from app.curation.dispatch import Budget

    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        dispatcher = Dispatcher(db, tmp_path, "d1", budget=Budget(calls_left=1, tokens_left=1000))
        first = await dispatcher.dispatch("get_chunks", {})
        assert first["budget"]["calls_left"] == 0
        with pytest.raises(DispatchError, match="budget_exhausted"):
            await dispatcher.dispatch("get_chunks", {})
    await engine.dispose()


@pytest.mark.asyncio
async def test_propose_ops_records_a_proposal(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        dispatcher = Dispatcher(db, tmp_path, "d1")
        result = await dispatcher.dispatch(
            "propose_ops",
            {"ops": [{"op": "set_summary", "summary": "来自 agent"}], "rationale": "test"},
        )
        await db.commit()
        assert result["applied_ops"] == 1
        rows = (await db.execute(select(CurationProposal))).scalars().all()
        assert len(rows) == 1
        assert rows[0].rationale == "test"
        assert rows[0].verdict in ("accepted", "partial")
    await engine.dispose()


@pytest.mark.asyncio
async def test_get_page_blocks_carries_labels(tmp_path):
    from app.curation import tools
    from app.ocr.cache import save_pages
    from app.ocr.models import OcrBlock, OcrPage, ParsedDocument

    engine = await _seed(tmp_path)
    ocr_dir = tmp_path / "ocr" / "d1"
    ocr_dir.mkdir(parents=True)

    save_pages(
        ocr_dir,
        ParsedDocument(
            pages=[
                OcrPage(
                    page_index=0,
                    width=100,
                    height=200,
                    markdown="",
                    blocks=[
                        OcrBlock("paragraph_title", "1 范围", (0, 0, 10, 10), 0),
                        OcrBlock("text", "正文", (0, 20, 10, 30), 1),
                    ],
                )
            ]
        ),
    )
    async with session_factory(engine)() as db:
        result = await tools.get_page_blocks(db, tmp_path, "d1", [0])
    assert result["pages"][0]["blocks"][0]["label"] == "paragraph_title"
    assert "bbox" in result["pages"][0]["blocks"][0]
    assert "markdown" not in result["pages"][0]
    assert "note" in result
    await engine.dispose()
