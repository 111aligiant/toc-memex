"""Search contract: mode, filters, empty_reason, hints/next, pdf pages.

These tests exercise ``scoped_search`` directly (the core contract) and the
MCP ``search`` wrapper for the ToolError validation it owns.
"""

from __future__ import annotations

import pytest

from app.db import init_db, make_engine, session_factory
from app.index.collections import create_collection
from app.index.fts import (
    doc_ids_for_filters,
    index_document,
    scoped_search,
)
from app.models import Chunk, Document, Namespace, TocNode


async def _seed(tmp_path):
    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    Session = session_factory(engine)

    specs = [
        {
            "id": "a",
            "title": "Paper Alpha",
            "doc_type": "paper",
            "source": "upload",
            "doc_date": "2021-05-01",
            "node_id": "a:5.1",
            "heading_num": "5.1",
            "heading": "5.1 试验方法",
            "body": "振动试验方法包括校准步骤",
            "page_start": 9,
            "page_end": 11,
        },
        {
            "id": "b",
            "title": "Manual Beta",
            "doc_type": "manual",
            "source": "scan",
            "doc_date": "2019-01-01",
            "node_id": "b:3",
            "heading_num": "3",
            "heading": "3 校准",
            "body": "校准记录需要归档",
            "page_start": 1,
            "page_end": 2,
        },
        {
            "id": "c",
            "title": "Paper Gamma",
            "doc_type": "paper",
            "source": "upload",
            "doc_date": None,
            "node_id": "c:2",
            "heading_num": "2",
            "heading": "2 概述",
            "body": "概述与范围",
            "page_start": 0,
            "page_end": 0,
        },
    ]

    async with Session() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        for spec in specs:
            doc = Document(
                id=spec["id"],
                namespace_id="shared",
                title=spec["title"],
                filename=f"{spec['id']}.pdf",
                file_path=f"{spec['id']}.pdf",
                content_hash=spec["id"],
                status="ready",
                doc_type=spec["doc_type"],
                source=spec["source"],
                doc_date=spec["doc_date"],
            )
            db.add(doc)
            await db.flush()
            db.add(
                TocNode(
                    doc_id=spec["id"],
                    node_id=spec["node_id"],
                    parent_id=None,
                    level=1,
                    heading_num=spec["heading_num"],
                    title=spec["heading"],
                    heading_path=spec["heading"],
                    page_start=spec["page_start"],
                    page_end=spec["page_end"],
                    chunk_start=0,
                    chunk_end=0,
                    source="bookmark",
                    confidence=1.0,
                )
            )
            chunk = Chunk(
                doc_id=spec["id"],
                node_id=spec["node_id"],
                chunk_index=0,
                content=spec["body"],
                heading_path=spec["heading"],
                page_start=spec["page_start"],
                page_end=spec["page_end"],
            )
            db.add(chunk)
            await db.flush()
            await index_document(db, doc, [chunk])
        await db.commit()
    return engine, Session


# ── mode ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invalid_mode_raises_tool_error(tmp_path):
    from mcp.server.mcpserver.exceptions import ToolError

    from app import mcp_server

    # Validation happens before any DB access, so no fixture is needed.
    with pytest.raises(ToolError, match="auto\\|lexical\\|semantic\\|hybrid"):
        await mcp_server.search(query="校准", mode="vector")
    with pytest.raises(ValueError):
        await scoped_search(object(), "校准", mode="vector")


@pytest.mark.asyncio
async def test_mode_is_case_insensitive_and_lexical(tmp_path):
    engine, Session = await _seed(tmp_path)
    async with Session() as db:
        upper = await scoped_search(db, "校准", top_k=5, mode="LEXICAL")
        auto = await scoped_search(db, "校准", top_k=5, mode="auto")
    assert upper["mode_requested"] == "lexical"
    assert upper["mode_effective"] == "lexical"
    assert upper["degraded"] is False
    assert upper["mode_note"] is None
    assert auto["mode_requested"] == "auto"
    assert auto["mode_effective"] == "lexical"
    assert auto["degraded"] is False
    assert [h["doc_id"] for h in upper["results"]] == [
        h["doc_id"] for h in auto["results"]
    ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_semantic_and_hybrid_degrade_to_lexical(tmp_path):
    engine, Session = await _seed(tmp_path)
    async with Session() as db:
        lexical = await scoped_search(db, "校准", top_k=5, mode="lexical")
        for requested in ("semantic", "hybrid"):
            degraded = await scoped_search(db, "校准", top_k=5, mode=requested)
            assert degraded["results"], "degraded search must still return results"
            assert degraded["mode_requested"] == requested
            assert degraded["mode_effective"] == "lexical"
            assert degraded["degraded"] is True
            assert degraded["mode_note"]
            assert [h["doc_id"] for h in degraded["results"]] == [
                h["doc_id"] for h in lexical["results"]
            ]
    await engine.dispose()


# ── filters ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_filter_doc_type_restricts_results(tmp_path):
    engine, Session = await _seed(tmp_path)
    async with Session() as db:
        only_manual = await scoped_search(
            db, "校准", top_k=5, filters={"doc_type": "manual"}
        )
        only_paper = await scoped_search(
            db, "校准", top_k=5, filters={"doc_type": "paper"}
        )
    assert {h["doc_id"] for h in only_manual["results"]} == {"b"}
    assert {h["doc_id"] for h in only_paper["results"]} == {"a"}
    assert only_manual["filters"] == {"doc_type": "manual"}
    await engine.dispose()


@pytest.mark.asyncio
async def test_filter_date_from_drops_older_and_undated(tmp_path):
    engine, Session = await _seed(tmp_path)
    async with Session() as db:
        filtered = await scoped_search(
            db, "校准", top_k=5, filters={"date_from": "2020"}
        )
    doc_ids = {h["doc_id"] for h in filtered["results"]}
    assert doc_ids == {"a"}  # b is 2019, c has no date
    await engine.dispose()


@pytest.mark.asyncio
async def test_filter_doc_id_singular_alias(tmp_path):
    engine, Session = await _seed(tmp_path)
    async with Session() as db:
        filtered = await scoped_search(
            db, "校准", top_k=5, filters={"doc_id": "b"}
        )
    assert {h["doc_id"] for h in filtered["results"]} == {"b"}
    await engine.dispose()


@pytest.mark.asyncio
async def test_filter_and_explicit_doc_ids_intersect_to_empty(tmp_path):
    engine, Session = await _seed(tmp_path)
    async with Session() as db:
        result = await scoped_search(
            db,
            "校准",
            top_k=5,
            doc_ids=["b"],
            filters={"doc_ids": ["a"]},
        )
    assert result["results"] == []
    assert result["empty_reason"] == "filtered_out"
    await engine.dispose()


@pytest.mark.asyncio
async def test_unknown_filter_key_and_bad_filters_are_tool_errors(tmp_path):
    from mcp.server.mcpserver.exceptions import ToolError

    from app import mcp_server

    engine, Session = await _seed(tmp_path)
    async with Session() as db:
        with pytest.raises(ValueError, match="未知 filter 键"):
            await doc_ids_for_filters(db, {"nope": 1})
        with mcp_server.bridge(db):
            with pytest.raises(ToolError, match="未知 filter 键"):
                await mcp_server.search(query="校准", filters={"nope": 1})
    with pytest.raises(ToolError, match="filters"):
        await mcp_server.search(query="校准", filters=["not", "a", "dict"])
    await engine.dispose()


# ── empty_reason ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_reason_no_terms(tmp_path):
    engine, Session = await _seed(tmp_path)
    async with Session() as db:
        result = await scoped_search(db, "的", top_k=5)
    assert result["results"] == []
    assert result["empty_reason"] == "no_terms"
    assert isinstance(result["hints"], list) and result["hints"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_empty_reason_collection_empty(tmp_path):
    engine, Session = await _seed(tmp_path)
    async with Session() as db:
        empty = await create_collection(db, "空分区")
        await db.commit()
        result = await scoped_search(db, "校准", top_k=5, collection=empty.id)
    assert result["results"] == []
    assert result["empty_reason"] == "collection_empty"
    assert "0 hits in collection" in result["hints"][0]
    await engine.dispose()


@pytest.mark.asyncio
async def test_empty_reason_filtered_out(tmp_path):
    engine, Session = await _seed(tmp_path)
    async with Session() as db:
        result = await scoped_search(
            db, "校准", top_k=5, filters={"doc_type": "不存在类型"}
        )
    assert result["results"] == []
    assert result["empty_reason"] == "filtered_out"
    await engine.dispose()


@pytest.mark.asyncio
async def test_empty_reason_no_match(tmp_path):
    engine, Session = await _seed(tmp_path)
    async with Session() as db:
        result = await scoped_search(db, "完全不存在术语xyz", top_k=5)
    assert result["results"] == []
    assert result["empty_reason"] == "no_match"
    await engine.dispose()


@pytest.mark.asyncio
async def test_empty_reason_scoped_miss_only_when_elsewhere_has_hits(tmp_path):
    engine, Session = await _seed(tmp_path)
    async with Session() as db:
        coll = await create_collection(db, "甲区")
        doc = await db.get(Document, "a")
        doc.collection_id = coll.id
        await db.commit()
        result = await scoped_search(db, "归档", top_k=5, collection=coll.id)
    assert result["results"] == []
    assert result["empty_reason"] == "scoped_miss"
    assert "Other collections with hits" in result["hints"][0]
    await engine.dispose()


@pytest.mark.asyncio
async def test_empty_reason_scope_with_no_hits_anywhere_is_no_match(tmp_path):
    engine, Session = await _seed(tmp_path)
    async with Session() as db:
        coll = await create_collection(db, "甲区")
        doc = await db.get(Document, "a")
        doc.collection_id = coll.id
        await db.commit()
        result = await scoped_search(
            db, "完全不存在术语xyz", top_k=5, collection=coll.id
        )
    assert result["results"] == []
    assert result["empty_reason"] == "no_match"
    assert "No other collection has hits" in result["hints"][0]
    await engine.dispose()


@pytest.mark.asyncio
async def test_blank_date_bounds_are_ignored(tmp_path):
    engine, Session = await _seed(tmp_path)
    async with Session() as db:
        result = await scoped_search(
            db, "校准", top_k=5, filters={"date_from": "", "date_to": "  "}
        )
    assert result["results"]
    assert result["empty_reason"] is None
    await engine.dispose()


# ── hints / next / channels ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_next_hints_and_channels_shape(tmp_path):
    engine, Session = await _seed(tmp_path)
    async with Session() as db:
        hit_result = await scoped_search(db, "校准", top_k=5)
        miss_result = await scoped_search(db, "完全不存在术语xyz", top_k=5)

    assert hit_result["results"]
    steps = hit_result["next"]
    assert 0 < len(steps) <= 3
    for step in steps:
        assert set(step) == {"tool", "args"}
        assert step["tool"] in {"read_section", "get_toc"}
    assert all(isinstance(hint, str) for hint in hit_result["hints"])
    assert hit_result["channels_used"] == list(
        dict.fromkeys(h["match_type"] for h in hit_result["results"])
    )

    # Backward compat: on a miss, hint is hints[0]; on a hit, hint is null.
    assert isinstance(miss_result["hints"], list) and miss_result["hints"]
    assert miss_result["hint"] == miss_result["hints"][0]
    assert hit_result["hint"] is None
    await engine.dispose()


# ── pdf page decoration (R18) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_pdf_pages_are_one_based_and_printed_is_null(tmp_path):
    engine, Session = await _seed(tmp_path)
    async with Session() as db:
        result = await scoped_search(
            db, "振动试验", top_k=5, filters={"doc_id": "a"}
        )
    assert result["results"]
    hit = result["results"][0]
    # Internal fields keep their 0-based meaning.
    assert hit["page_start"] == 9
    assert hit["page_end"] == 11
    # Agent-facing fields are 1-based.
    assert hit["pdf_page_start"] == 10
    assert hit["pdf_page_end"] == 12
    assert hit["printed_page_start"] is None
    assert hit["printed_page_end"] is None
    await engine.dispose()
