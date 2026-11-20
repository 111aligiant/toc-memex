from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.db import init_db, make_engine, session_factory
from app.index.documents import (
    memex_overview,
    get_document,
    get_toc,
    list_documents,
)
from app.models import Chunk, Document, Namespace, TocNode


async def _seed(tmp_path):
    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    Session = session_factory(engine)

    async with Session() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        doc = Document(
            id="d1",
            namespace_id="shared",
            title="测试标准",
            filename="test.pdf",
            file_path="test.pdf",
            content_hash="h",
            doc_type="standard",
            status="ready",
        )
        db.add(doc)
        await db.flush()
        db.add_all(
            [
                TocNode(
                    doc_id="d1",
                    node_id="d1:1",
                    parent_id=None,
                    level=1,
                    heading_num="第1章",
                    title="第1章 范围",
                    heading_path="第1章 范围",
                    page_start=1,
                    page_end=2,
                    chunk_start=0,
                    chunk_end=0,
                    source="toc_page",
                    confidence=0.9,
                ),
                TocNode(
                    doc_id="d1",
                    node_id="d1:1.1",
                    parent_id="d1:1",
                    level=2,
                    heading_num="1.1",
                    title="1.1 目的",
                    heading_path="第1章 范围 > 1.1 目的",
                    page_start=1,
                    page_end=1,
                    chunk_start=0,
                    chunk_end=0,
                    source="toc_page",
                    confidence=0.9,
                ),
            ]
        )
        chunk = Chunk(
            doc_id="d1",
            node_id="d1:1.1",
            chunk_index=0,
            content="本文件规定了范围。",
            heading_path="第1章 范围 > 1.1 目的",
            page_start=1,
            page_end=1,
        )
        db.add(chunk)
        await db.flush()
        # search goes through FTS, so the seed must index or the MCP search
        # test would only ever see an empty result set.
        from app.index.fts import index_document

        await index_document(db, doc, [chunk])
        await db.commit()

    return engine


@pytest.mark.asyncio
async def test_memex_overview(tmp_path):
    engine = await _seed(tmp_path)
    Session = session_factory(engine)

    async with Session() as db:
        overview = await memex_overview(db)

    assert overview["documents"] == 1
    assert overview["toc_nodes"] == 2
    assert overview["chunks"] == 1
    assert overview["doc_types"]["standard"] == 1
    assert overview["recent"][0]["doc_id"] == "d1"

    await engine.dispose()


@pytest.mark.asyncio
async def test_list_and_get_document(tmp_path):
    engine = await _seed(tmp_path)
    Session = session_factory(engine)

    async with Session() as db:
        documents = await list_documents(db, query="测试")
        doc = await get_document(db, "d1")

    assert documents[0]["doc_id"] == "d1"
    assert doc["doc_id"] == "d1"
    assert doc["toc_nodes"] == 2
    assert doc["chunks"] == 1
    assert doc["toc_roots"][0]["node_id"] == "d1:1"

    await engine.dispose()


@pytest.mark.asyncio
async def test_list_documents_matches_keywords_and_title(tmp_path):
    """A term in the keywords still finds its document.

    「筛选文档」 is document-level search, so it must cover what ingest
    writes into doc_fts — otherwise a document about 盐雾 whose title never
    says 盐雾 is invisible to the filter.
    """
    engine = await _seed(tmp_path)
    Session = session_factory(engine)
    from app.index.fts import index_document

    async with Session() as db:
        doc = await db.get(Document, "d1")
        doc.summary = "本文件规定了盐雾试验的严酷度等级。"
        doc.keywords = "盐雾, 腐蚀, 试验"
        chunks = (
            await db.execute(select(Chunk).where(Chunk.doc_id == "d1"))
        ).scalars().all()
        await index_document(db, doc, chunks)
        await db.commit()

        by_summary = await list_documents(db, query="盐雾")
        by_keyword = await list_documents(db, query="腐蚀")
        by_title = await list_documents(db, query="测试")
        miss = await list_documents(db, query="不存在的词")

    assert [item["doc_id"] for item in by_summary] == ["d1"]
    assert [item["doc_id"] for item in by_keyword] == ["d1"]
    assert [item["doc_id"] for item in by_title] == ["d1"]
    assert miss == []

    await engine.dispose()


@pytest.mark.asyncio
async def test_get_toc_depth(tmp_path):
    engine = await _seed(tmp_path)
    Session = session_factory(engine)

    async with Session() as db:
        shallow = await get_toc(db, "d1", depth=1)
        deep = await get_toc(db, "d1", depth=2)

    assert shallow["roots"][0]["node_id"] == "d1:1"
    assert shallow["roots"][0]["children"] == []
    assert deep["roots"][0]["children"][0]["node_id"] == "d1:1.1"

    await engine.dispose()


def test_mcp_server_registers_tools():
    from app.mcp_server import server

    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    assert {
        "memex_overview",
        "list_documents",
        "get_document",
        "get_toc",
        "read_section",
        "search",
        "ask",
        "list_collections",
    } <= names


@pytest.mark.asyncio
async def test_mcp_search_tool(tmp_path, monkeypatch):
    from app import mcp_server
    from app.config import Settings

    engine = await _seed(tmp_path)
    settings = Settings(
        tocmemex_db_path=tmp_path / "tocmemex.db",
        tocmemex_data_dir=tmp_path / "data",
    )
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)

    result = await mcp_server.server.call_tool(
        "search", {"query": "范围", "top_k": 2}
    )

    # search returns a dict, which has no output model, so the payload
    # arrives as JSON in the text block (what the agent actually reads).
    import json

    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["results"]
    assert payload["scope"] == {"collection": None, "unassigned": False}
    await engine.dispose()