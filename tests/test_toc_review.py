"""纲目复审 (docs/m3-toc-review.md)."""

from __future__ import annotations

import pytest

from app.db import init_db, make_engine, session_factory
from app.ingest.toc_review import review_document
from app.models import Document, Namespace, TocNode

PAGES = {
    1: "1 范围 本文件规定了恒定湿热试验的条件和步骤",
    2: "4 附录 试验设备清单",
    3: "2 试验设备 试验箱应能维持规定的温湿度",
    4: "4 附录 补充说明与记录表",
}


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
                title="试验方法",
                filename="a.pdf",
                file_path="a.pdf",
                content_hash="h",
                page_count=5,
                status="ready",
            )
        )
        await db.flush()
        db.add_all(
            [
                TocNode(
                    doc_id="d1", node_id="d1:h:1", level=1, heading_num="1",
                    title="1 范围", heading_path="1 范围", page_start=1, page_end=1,
                ),
                TocNode(
                    doc_id="d1", node_id="d1:h:2", level=1, heading_num="2",
                    title="2 试验设备", heading_path="2 试验设备",
                    page_start=1, page_end=1,
                ),
                TocNode(
                    doc_id="d1", node_id="d1:h:3", level=1, heading_num="3",
                    title="3 其他要求", heading_path="3 其他要求",
                    page_start=1, page_end=1,
                ),
                TocNode(
                    doc_id="d1", node_id="d1:h:4", level=1, heading_num="4",
                    title="4 附录", heading_path="4 附录", page_start=1, page_end=1,
                ),
                TocNode(
                    doc_id="d1", node_id="d1:h:0", level=1,
                    title="前言", heading_path="前言", page_start=None, page_end=None,
                ),
            ]
        )
        await db.commit()
    return engine


@pytest.mark.asyncio
async def test_review_locates_a_misplaced_node(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        doc = await db.get(Document, "d1")
        report = await review_document(db, doc, PAGES)
    assert report["checked"] == 4
    assert report["ok"] == 1, "node 1 is on its page"
    proposed = {entry["node_id"]: entry for entry in report["proposed"]}
    assert set(proposed) == {"d1:h:2"}
    entry = proposed["d1:h:2"]
    assert entry["found"] == 3
    assert entry["op"]["page_start"] == 3
    assert entry["op"]["evidence"] == "2 试验设备"
    unlocatable = {entry["node_id"] for entry in report["unlocatable"]}
    assert "d1:h:3" in unlocatable
    ambiguous = {entry["node_id"]: entry["hits"] for entry in report["ambiguous"]}
    assert ambiguous == {"d1:h:4": [2, 4]}
    assert report["skipped"] == 1, "the node without a page range is skipped"
    await engine.dispose()


@pytest.mark.asyncio
async def test_review_without_page_cache_checks_nothing(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        doc = await db.get(Document, "d1")
        report = await review_document(db, doc, {})
    assert report["checked"] == 0
    assert report["proposed"] == []
    assert report["skipped"] == 5
    await engine.dispose()


@pytest.mark.asyncio
async def test_review_respects_the_window(tmp_path):
    """A title far outside the window is not relocated — reported instead."""
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        doc = await db.get(Document, "d1")
        report = await review_document(db, doc, PAGES, window=1)
    # node 2 lives on page 3, two pages away — outside the window, so it is
    # reported rather than guessed. Node 4's second occurrence (page 4) is
    # outside the window too, so the page-2 hit becomes unique and is
    # proposed; narrowing the window can therefore surface new fixes.
    assert [entry["node_id"] for entry in report["proposed"]] == ["d1:h:4"]
    assert any(entry["node_id"] == "d1:h:2" for entry in report["unlocatable"])
    await engine.dispose()


@pytest.mark.asyncio
async def test_proposed_op_validates_as_pending(tmp_path):
    """The op the review produces passes the existing gate as pending_human."""
    from app.curation.ops import ProposalContext, validate_op

    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        doc = await db.get(Document, "d1")
        report = await review_document(db, doc, PAGES)
        op = report["proposed"][0]["op"]
        ctx = ProposalContext(db=db, doc=doc, pages=PAGES, data_dir=None)
        status, reason = await validate_op(ctx, op)
    assert status == "pending"
    assert reason == ""
    await engine.dispose()

@pytest.mark.asyncio
async def test_catalog_page_is_not_a_relocation_target(tmp_path):
    """The printed TOC page contains every title; it must never be the fix."""
    engine = await _seed(tmp_path)
    pages = {
        0: "目录 1 范围 2 试验设备 3 其他要求 4 附录",
        **PAGES,
    }
    async with session_factory(engine)() as db:
        doc = await db.get(Document, "d1")
        report = await review_document(db, doc, pages)
    assert report["catalog_pages"] == [0]
    proposed = {entry["node_id"]: entry for entry in report["proposed"]}
    assert proposed["d1:h:2"]["found"] == 3, "must land on the body page, not the TOC"
    assert all(not entry["op"]["page_start"] == 0 for entry in report["proposed"])
    await engine.dispose()
