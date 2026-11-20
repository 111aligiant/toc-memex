from __future__ import annotations

import pytest
from sqlalchemy import select

from app.curation import strategies
from app.db import init_db, make_engine, session_factory
from app.models import Chunk, Document, Namespace, TocNode


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
                title="GB/T 2423.3-2006 试验 Cab 恒定湿热",
                filename="a.pdf",
                file_path="a.pdf",
                content_hash="h",
                page_count=12,
                status="ready",
            )
        )
        await db.flush()
        db.add_all(
            [
                TocNode(
                    doc_id="d1", node_id="d1:h:1", level=1, heading_num="1",
                    title="1 范围", heading_path="1 范围", page_start=0, page_end=0,
                ),
                TocNode(
                    doc_id="d1", node_id="d1:h:2", level=1, heading_num="2",
                    title="2 规范性引用文件", heading_path="2 规范性引用文件",
                    page_start=1, page_end=3,
                ),
                TocNode(
                    doc_id="d1", node_id="d1:h:2.1", parent_id="d1:h:2", level=2,
                    heading_num="2.1", title="2.1 引用标准", heading_path="2 > 2.1",
                    page_start=1, page_end=1,
                ),
                TocNode(
                    doc_id="d1", node_id="d1:h:2.2", parent_id="d1:h:2", level=2,
                    heading_num="2.2", title="2.2 引用说明", heading_path="2 > 2.2",
                    page_start=2, page_end=2,
                ),
            ]
        )
        await db.flush()
        db.add_all(
            [
                Chunk(
                    doc_id="d1", node_id="d1:h:1", chunk_index=0,
                    content="1 范围\n\n本文件规定了恒定湿热试验的方法。", heading_path="1 范围",
                    page_start=0, page_end=0,
                ),
                # A parent heading the chunker split off, with no body.
                Chunk(
                    doc_id="d1", node_id="d1:h:2", chunk_index=1,
                    content="2 规范性引用文件", heading_path="2 规范性引用文件",
                    page_start=1, page_end=1,
                ),
                Chunk(
                    doc_id="d1", node_id="d1:h:2.1", chunk_index=2,
                    content="2.1 引用标准\n\n下列文件中的条款通过本文件的引用而成为本文件的条款。",
                    heading_path="2 > 2.1", page_start=1, page_end=1,
                ),
                Chunk(
                    doc_id="d1", node_id="d1:h:2.2", chunk_index=3,
                    content="2.2 引用说明", heading_path="2 > 2.2", page_start=2, page_end=2,
                ),
            ]
        )
        await db.commit()
    return engine


@pytest.mark.asyncio
async def test_heading_only_parent_slice_is_dropped(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        chunks = (await db.execute(select(Chunk).where(Chunk.doc_id == "d1"))).scalars().all()
        nodes = {node.node_id: node for node in (await db.execute(select(TocNode))).scalars()}
        ops = strategies.plan_short_chunk_merges(chunks, nodes, 1200)
    dropped = [op for op in ops if op["op"] == "drop_chunk"]
    assert len(dropped) == 1
    # Only the parent heading, not the leaf heading (2.2 has a childless node
    # but the slice is the node's whole body too — both are heading-only).
    assert dropped[0]["evidence_in_chunk"] is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_short_run_packs_into_one_slice(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        doc = await db.get(Document, "d1")
        db.add(
            TocNode(
                doc_id="d1", node_id="d1:h:9", level=1, heading_num="9",
                title="9 附录", heading_path="9 附录", page_start=5, page_end=8,
            )
        )
        await db.flush()
        for index in range(6):
            db.add(
                Chunk(
                    doc_id="d1", node_id="d1:h:9", chunk_index=10 + index,
                    content=f"第 {index} 段很短的附录文字",
                    heading_path="9 附录", page_start=5 + index, page_end=5 + index,
                )
            )
        await db.commit()
        chunks = (await db.execute(select(Chunk).where(Chunk.doc_id == "d1"))).scalars().all()
        nodes = {node.node_id: node for node in (await db.execute(select(TocNode))).scalars()}
        ops = strategies.plan_short_chunk_merges(chunks, nodes, 1200)
    runs = [op for op in ops if op["evidence"] == "short run"]
    assert runs
    # Six short slices of one node become one merge op.
    assert any(len(op["chunk_ids"]) == 6 for op in runs)
    await engine.dispose()


def test_heading_only_detection():
    from types import SimpleNamespace

    node = SimpleNamespace(title="2.3 定义", heading_num="2.3")
    assert strategies.is_heading_only("2.3 定义", node)
    assert strategies.is_heading_only("2.3 定义\n\n", node)
    assert not strategies.is_heading_only("2.3 定义\n\n本文件规定了…" + "内容" * 20, node)
    assert not strategies.is_heading_only("无关文字", node)


def test_page_range_fix_widens_parent_only():
    from types import SimpleNamespace

    nodes = [
        SimpleNamespace(node_id="p", parent_id=None, heading_num="1", title="1 章",
                        page_start=3, page_end=3),
        SimpleNamespace(node_id="c1", parent_id="p", heading_num="1.1", title="1.1",
                        page_start=3, page_end=5),
        SimpleNamespace(node_id="c2", parent_id="p", heading_num="1.2", title="1.2",
                        page_start=6, page_end=8),
        SimpleNamespace(node_id="leaf", parent_id=None, heading_num="2", title="2 章",
                        page_start=9, page_end=9),
    ]
    ops = strategies.plan_page_range_fixes(nodes)
    assert len(ops) == 1
    assert ops[0]["node_id"] == "p"
    assert ops[0]["page_start"] == 3
    assert ops[0]["page_end"] == 8
