from __future__ import annotations

import pytest

from app.db import init_db, make_engine, session_factory
from app.index.documents import get_document
from app.index.fts import scoped_search
from app.models import Chunk, Document, Namespace, TocNode


async def _seed_reading_order(tmp_path) -> tuple:
    """Three roots that a page-first sort must order reading-correctly.

    page_start is the PDF page a section begins on; chunk_start is the cut
    order. Two sections may share a page_start but differ in chunk_start —
    the chunk span must win the tie so the displayed TOC matches the on-page
    sequence (mcp-feedback §4.1).
    """
    engine = make_engine(tmp_path / "order.db")
    await init_db(engine)
    Session = session_factory(engine)
    async with Session() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        doc = Document(
            id="d1",
            namespace_id="shared",
            title="顺序文档",
            filename="a.pdf",
            file_path="a.pdf",
            content_hash="h",
            status="ready",
        )
        db.add(doc)
        await db.flush()
        db.add_all(
            [
                TocNode(
                    doc_id="d1", node_id="n2", parent_id=None, level=1,
                    heading_num="2", title="2 中间", heading_path="2 中间",
                    page_start=1, chunk_start=1, source="bookmark", confidence=1.0,
                ),
                TocNode(
                    doc_id="d1", node_id="n1", parent_id=None, level=1,
                    heading_num="1", title="1 开头", heading_path="1 开头",
                    page_start=0, chunk_start=0, source="bookmark", confidence=1.0,
                ),
                # Same page as n2 but cut later: chunk_start breaks the tie.
                TocNode(
                    doc_id="d1", node_id="n3", parent_id=None, level=1,
                    heading_num="3", title="3 结尾", heading_path="3 结尾",
                    page_start=1, chunk_start=3, source="bookmark", confidence=1.0,
                ),
            ]
        )
        await db.flush()
        await db.commit()
    return engine, Session


@pytest.mark.asyncio
async def test_get_document_roots_in_reading_order(tmp_path):
    engine, Session = await _seed_reading_order(tmp_path)
    async with Session() as db:
        doc = await get_document(db, "d1")
        titles = [root["title"] for root in doc["toc_roots"]]
    assert titles == ["1 开头", "2 中间", "3 结尾"]
    await engine.dispose()


# --- 0.4.3 retrieval: one document must not flood the results page ---


async def _seed_limit_docs(tmp_path) -> tuple:
    engine = make_engine(tmp_path / "limit.db")
    await init_db(engine)
    Session = session_factory(engine)
    async with Session() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        d1 = Document(
            id="d1", namespace_id="shared", title="甲", filename="a.pdf",
            file_path="a.pdf", content_hash="h1", status="ready",
        )
        d2 = Document(
            id="d2", namespace_id="shared", title="乙", filename="b.pdf",
            file_path="b.pdf", content_hash="h2", status="ready",
        )
        db.add_all([d1, d2])
        await db.flush()
        chunks = [
            Chunk(doc_id="d1", chunk_index=0, content="数据 分析 方法 结果",
                  heading_path="1"),
            Chunk(doc_id="d1", chunk_index=1, content="数据 分析 讨论 综述",
                  heading_path="2"),
            Chunk(doc_id="d1", chunk_index=2, content="数据 分析 结论 展望",
                  heading_path="3"),
            Chunk(doc_id="d2", chunk_index=0, content="数据 分析 简介 摘要",
                  heading_path="1"),
        ]
        db.add_all(chunks)
        await db.flush()
        from app.index.fts import index_document
        await index_document(db, d1, chunks[:3])
        await index_document(db, d2, chunks[3:])
        await db.commit()
    return engine, Session


@pytest.mark.asyncio
async def test_scoped_search_limits_docs_distinct_docs(tmp_path):
    engine, _ = await _seed_limit_docs(tmp_path)
    async with session_factory(engine)() as db:
        result = await scoped_search(db, "数据 分析", top_k=8, distinct_docs=2)
        docs = [hit["doc_id"] for hit in result["results"]]
    assert docs.count("d1") <= 2
    assert "d2" in docs  # diverse documents still represented
    await engine.dispose()


@pytest.mark.asyncio
async def test_scoped_search_distinct_docs_none_keeps_all(tmp_path):
    engine, _ = await _seed_limit_docs(tmp_path)
    async with session_factory(engine)() as db:
        result = await scoped_search(db, "数据 分析", top_k=8, distinct_docs=None)
        docs = [hit["doc_id"] for hit in result["results"]]
    assert docs.count("d1") == 3
    await engine.dispose()


# --- 0.4.4 absolute file_path at ingest ---


def test_ingest_path_normalizes_relative_path():
    """ingest_path resolves its input so ``file_path`` is stored absolute.

    mcp-feedback §4.4: old rows stored relative ``data\\uploads\\...`` paths,
    new rows absolute, and an agent cannot reliably parse either. The fix is
    one line in ``ingest_path`` — ``path = Path(path).resolve()`` — and this
    test pins it so a future refactor cannot silently re-relative the stored
    path.
    """
    import inspect

    from app.ingest import pipeline

    src = inspect.getsource(pipeline.ingest_path)
    assert "Path(path).resolve()" in src
    # The normalization happens before the DB row is written.
    assert src.index("Path(path).resolve()") < src.index("_open_ingest_row")