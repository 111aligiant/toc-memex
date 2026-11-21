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


# ── B2: L2 approval of a high-risk op must actually apply it ──────────────


def _seed_ocr_pages(tmp_path):
    """Write page text so set_page grounding can succeed for doc d1."""
    from app.ocr.cache import save_pages
    from app.ocr.models import OcrBlock, OcrPage, ParsedDocument

    ocr_dir = tmp_path / "ocr" / "d1"
    ocr_dir.mkdir(parents=True, exist_ok=True)
    pages = []
    # Page texts must contain the strings the seeded chunks use, so evidence
    # grounding for set_page / split_chunk can actually succeed.
    page_body = {
        0: (
            "1 范围 本文件规定了试验方法。"
            "第一条 本文件规定了适用于所有电工电子产品的通用试验方法与一般要求。"
            "第二条 试验条件、仪器设备、试样制备与数据处理均应遵循本标准的规定执行。"
        ),
        1: "适用于所有电工电子产品。",
        2: "1.1 目的 说明试验的目的和适用范围。",
    }
    for index in range(10):
        blocks = [
            OcrBlock("paragraph_title", f"第 {index} 页标题", (0, 0, 10, 10), 0),
            OcrBlock(
                "text",
                page_body.get(index, f"第 {index} 页正文 内容编号 {index}"),
                (0, 20, 10, 30),
                1,
            ),
        ]
        if index == 1:
            # A heading the set_page evidence below will point at.
            blocks.append(OcrBlock("paragraph_title", "1.1 目的", (0, 40, 10, 50), 2))
        pages.append(
            OcrPage(
                page_index=index, width=100, height=200, markdown="", blocks=blocks
            )
        )
    save_pages(ocr_dir, ParsedDocument(pages=pages))


@pytest.mark.asyncio
async def test_set_page_stays_pending_on_first_proposal(tmp_path):
    """First pass (propose_ops) leaves a valid high-risk op pending."""
    engine = await _seed(tmp_path)
    _seed_ocr_pages(tmp_path)
    async with session_factory(engine)() as db:
        doc = await db.get(Document, "d1")
        ctx = await ops_mod.build_context(db, doc, tmp_path)
        outcome = await ops_mod.apply_ops(
            ctx,
            [{"op": "set_page", "node_id": "d1:h:1.1", "page_start": 0, "page_end": 3,
              "evidence": "1.1 目的"}],
            "p1",
        )
        assert outcome["results"][0]["status"] == "pending"
        assert outcome["applied_ops"] == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_apply_proposal_applies_an_approved_set_page(tmp_path):
    """B2: approving the pending set_page must move the node, not dead-end.

    Prior to the fix, apply_ops re-validated the pending op as 'pending'
    again and _apply_one was never reached, so L2 approval did nothing.
    """
    engine = await _seed(tmp_path)
    _seed_ocr_pages(tmp_path)

    async with session_factory(engine)() as db:
        # allow_approval=True models the human approval surface; the curator
        # agent loop is refused (it could otherwise self-approve a canary).
        dispatcher = Dispatcher(db, tmp_path, "d1", allow_approval=True)
        proposed = await dispatcher.dispatch(
            "propose_ops",
            {
                "ops": [
                    {"op": "set_page", "node_id": "d1:h:1.1",
                     "page_start": 0, "page_end": 3, "evidence": "1.1 目的"}
                ],
                "rationale": "fix page range",
            },
        )
        pending = [e for e in proposed["results"] if e["status"] == "pending"]
        assert len(pending) == 1
        # Node still at its old range before approval.
        node = (
            await db.execute(select(TocNode).where(TocNode.node_id == "d1:h:1.1"))
        ).scalar_one()
        assert (node.page_start, node.page_end) != (0, 3)

        applied = await dispatcher.dispatch(
            "apply_proposal", {"proposal_id": proposed["proposal_id"]}
        )
        await db.commit()
        assert applied["applied"] == 1
        node = (
            await db.execute(select(TocNode).where(TocNode.node_id == "d1:h:1.1"))
        ).scalar_one()
        assert (node.page_start, node.page_end) == (0, 3)
    await engine.dispose()


@pytest.mark.asyncio
async def test_merge_nodes_is_rejected_not_pending(tmp_path):
    """B2: unimplemented high-risk ops must be dropped, not left pending."""
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        doc = await db.get(Document, "d1")
        ctx = await ops_mod.build_context(db, doc, tmp_path)
        outcome = await ops_mod.apply_ops(
            ctx,
            [{"op": "merge_nodes", "node_ids": ["d1:h:1", "d1:h:1.1"]}],
            "p1",
        )
        assert outcome["results"][0]["status"] == "dropped"
        assert "not implemented" in outcome["results"][0]["reason"]
    await engine.dispose()


# ── B5: split_chunk must not collide on UNIQUE(doc_id, chunk_index) ───────


@pytest.mark.asyncio
async def test_split_chunk_shifts_later_chunks_and_keeps_order(tmp_path):
    """B5: splitting a chunk when a later chunk exists used to collide."""
    engine = await _seed(tmp_path)
    _seed_ocr_pages(tmp_path)
    long_head = "第一条 本文件规定了适用于所有电工电子产品的通用试验方法与一般要求。"
    long_tail = "第二条 试验条件、仪器设备、试样制备与数据处理均应遵循本标准的规定执行。"
    long_body = long_head + long_tail
    async with session_factory(engine)() as db:
        # Replace chunk 0 with a long two-sentence body so the split keeps both
        # halves substantial (verify must not roll it back as degenerate).
        chunk0 = (
            await db.execute(
                select(Chunk).where(Chunk.doc_id == "d1", Chunk.chunk_index == 0)
            )
        ).scalar_one()
        chunk0.content = long_body
        await db.commit()

        doc = await db.get(Document, "d1")
        ctx = await ops_mod.build_context(db, doc, tmp_path)
        target = (
            await db.execute(
                select(Chunk).where(Chunk.doc_id == "d1", Chunk.chunk_index == 0)
            )
        ).scalar_one()
        outcome = await ops_mod.apply_ops(
            ctx,
            [{"op": "split_chunk", "chunk_id": target.id, "at": long_tail}],
            "p1",
            verify=False,
        )
        await db.commit()
        assert outcome["applied_ops"] == 1
        rows = (
            await db.execute(
                select(Chunk).where(Chunk.doc_id == "d1").order_by(Chunk.chunk_index)
            )
        ).scalars().all()
        # Indices stay contiguous and unique; count grew by one.
        assert [row.chunk_index for row in rows] == list(range(len(rows)))
        assert len(rows) == 4
        # The tail content landed in the immediately following slot.
        assert rows[1].content.startswith(long_tail)
    await engine.dispose()


# ── B6: stale-job recovery must actually see ISO timestamps ───────────────


@pytest.mark.asyncio
async def test_recover_stale_jobs_requeues_an_old_running_job(tmp_path):
    """B6: a job whose ISO updated_at is 30 min old must be requeued.

    The old datetime('now','-15 minutes') produced a space-separated string
    that compared lexically AFTER every ISO 'T' timestamp, so nothing was
    ever recovered.
    """
    from datetime import datetime, timedelta, timezone

    from app.curation.runner import recover_stale_jobs
    from app.models import CurationJob

    engine = await _seed(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    async with session_factory(engine)() as db:
        db.add_all(
            [
                CurationJob(
                    id="job_old", issue_id="x:1", doc_id="d1", kind="rule",
                    fingerprint="f1", status="running", updated_at=old,
                ),
                CurationJob(
                    id="job_new", issue_id="x:2", doc_id="d1", kind="rule",
                    fingerprint="f2", status="running", updated_at=fresh,
                ),
            ]
        )
        await db.commit()

    async with session_factory(engine)() as db:
        requeued = await recover_stale_jobs(db)
        assert requeued == 1
        old_job = await db.get(CurationJob, "job_old")
        new_job = await db.get(CurationJob, "job_new")
        assert old_job.status == "queued"
        assert new_job.status == "running"
    await engine.dispose()
