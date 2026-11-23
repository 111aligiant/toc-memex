from __future__ import annotations

import pytest

from app.db import init_db, make_engine, session_factory
from app.index.ask import ask_question
from app.index.fts import index_document, search_chunks
from app.index.sections import (
    is_pure_section_query,
    normalize_section_number,
    read_section,
    title_echo,
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
            title="振动标准",
            filename="vibration.pdf",
            file_path="vibration.pdf",
            content_hash="h",
            status="ready",
        )
        db.add(doc)
        await db.flush()

        db.add_all(
            [
                TocNode(
                    doc_id="d1",
                    node_id="d1:8",
                    parent_id=None,
                    level=1,
                    heading_num="第8章",
                    title="振动",
                    heading_path="第8章 振动",
                    page_start=10,
                    page_end=20,
                    chunk_start=0,
                    chunk_end=2,
                    source="toc_page",
                    confidence=0.9,
                ),
                TocNode(
                    doc_id="d1",
                    node_id="d1:8.3",
                    parent_id="d1:8",
                    level=2,
                    heading_num="8.3",
                    title="振动试验要求",
                    heading_path="第8章 振动 > 8.3 振动试验要求",
                    page_start=12,
                    page_end=13,
                    chunk_start=1,
                    chunk_end=2,
                    source="toc_page",
                    confidence=0.9,
                ),
            ]
        )
        await db.flush()

        chunks = [
            Chunk(
                doc_id="d1",
                node_id="d1:8",
                chunk_index=0,
                content="8.1 一般要求 振动试验应在设备安装状态下进行。",
                heading_path="第8章 振动 > 8.1 一般要求",
                page_start=10,
                page_end=10,
                token_count=12,
            ),
            Chunk(
                doc_id="d1",
                node_id="d1:8.3",
                chunk_index=1,
                content="8.3 振动试验要求 下列通用要求适用于所有振动试验：",
                heading_path="第8章 振动 > 8.3 振动试验要求",
                page_start=12,
                page_end=12,
                token_count=14,
            ),
            Chunk(
                doc_id="d1",
                node_id="d1:8.3",
                chunk_index=2,
                content="试验期间设备的安装应确保其输入点受到规定的振动。",
                heading_path="第8章 振动 > 8.3 振动试验要求",
                page_start=13,
                page_end=13,
                token_count=13,
            ),
        ]
        db.add_all(chunks)
        await db.flush()
        await index_document(db, doc, chunks)
        await db.commit()
        chunk_ids = [chunk.id for chunk in chunks]

    return engine, chunk_ids


def test_normalize_section_number():
    assert normalize_section_number("8.3") == "8.3"
    assert normalize_section_number(" 8 . 3 ") == "8.3"
    assert normalize_section_number("§26.6.4.7.1") == "26.6.4.7.1"
    assert normalize_section_number("第 8 章") == "第8章"
    assert normalize_section_number("附录 A") == "附录A"
    assert normalize_section_number("8.3 振动试验要求") == "8.3"


def test_title_echo_ignores_numbers_and_short_titles():
    assert title_echo("4.2 薄膜介电性能与压电响应的研究", "4.2 薄膜介电性能与压电响应的研究")
    assert title_echo("Thermodynamic considerations 给出了哪些结论？", "Thermodynamic considerations")
    # Short stand-bys are too common to name a section.
    assert not title_echo("这篇论文的实验说明了什么？", "4.2 实验")
    assert not title_echo("某一章讲了什么", "2.1 摘要")
    assert not title_echo("完全无关的问题", "4.2 薄膜介电性能与压电响应的研究")


def test_pure_section_query():
    assert is_pure_section_query("8.3")
    assert is_pure_section_query("§26.6.4.7.1")
    assert is_pure_section_query("第8章")
    assert not is_pure_section_query("8.3 振动试验要求")


@pytest.mark.asyncio
async def test_read_section_returns_leaf_text_and_citation(tmp_path):
    engine, chunk_ids = await _seed(tmp_path)
    Session = session_factory(engine)

    async with Session() as db:
        result = await read_section(db, "d1", node_id="d1:8.3")

    assert result["node_id"] == "d1:8.3"
    assert result["heading_num"] == "8.3"
    assert result["heading_path"].endswith("8.3 振动试验要求")
    assert result["page_start"] == 12
    assert result["page_end"] == 13
    assert result["chunk_ids"] == chunk_ids[1:]
    assert "下列通用要求" in result["content"]
    assert "试验期间设备" in result["content"]
    assert "8.1 一般要求" not in result["content"]

    await engine.dispose()


@pytest.mark.asyncio
async def test_read_section_includes_subtree(tmp_path):
    engine, chunk_ids = await _seed(tmp_path)
    Session = session_factory(engine)

    async with Session() as db:
        result = await read_section(db, "d1", node_id="d1:8")

    assert result["chunk_ids"] == chunk_ids
    assert "8.1 一般要求" in result["content"]
    assert "8.3 振动试验要求" in result["content"]
    assert result["page_start"] == 10
    assert result["page_end"] == 20

    await engine.dispose()


@pytest.mark.asyncio
async def test_read_section_falls_back_to_numbered_parent(tmp_path):
    """A 4.2.1 id the agent built by hand resolves to the 4.2 node.

    The paragraph heading lives inside 4.2's text and has no TOC node, so a
    guessed id used to dead-end the run.
    """
    engine, _ = await _seed(tmp_path)
    Session = session_factory(engine)

    async with Session() as db:
        result = await read_section(db, "d1", node_id="d1:h:8.3.1")

    assert result["node_id"] == "d1:8.3"
    assert result["resolved_from"] == "d1:h:8.3.1"
    assert "下列通用要求" in result["content"]

    await engine.dispose()


@pytest.mark.asyncio
async def test_read_section_falls_back_to_deduplicated_number(tmp_path):
    """The un-suffixed number still reaches a node stored as ``h:4.2.1@6``."""
    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    Session = session_factory(engine)

    async with Session() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(
            Document(
                id="d1",
                namespace_id="shared",
                title="振动标准",
                filename="v.pdf",
                file_path="v.pdf",
                content_hash="h",
                status="ready",
            )
        )
        await db.flush()
        db.add(
            TocNode(
                doc_id="d1",
                node_id="d1:h:4.2.1@6",
                parent_id=None,
                level=1,
                heading_num="4.2.1",
                title="4.2.1 概述",
                heading_path="4.2.1 概述",
                page_start=6,
                page_end=6,
                chunk_start=0,
                chunk_end=0,
                source="toc",
                confidence=0.9,
            )
        )
        chunk = Chunk(
            doc_id="d1",
            node_id="d1:h:4.2.1@6",
            chunk_index=0,
            content="4.2.1 概述 振动试验方法按表1选择。",
            heading_path="4.2.1 概述",
            page_start=6,
            page_end=6,
        )
        db.add(chunk)
        await db.flush()
        await db.commit()

        result = await read_section(db, "d1", node_id="d1:h:4.2.1")

    assert result["node_id"] == "d1:h:4.2.1@6"
    assert result["resolved_from"] == "d1:h:4.2.1"
    assert "按表1选择" in result["content"]

    await engine.dispose()


@pytest.mark.asyncio
async def test_pure_section_query_hits_exact_node_first(tmp_path):
    engine, chunk_ids = await _seed(tmp_path)
    Session = session_factory(engine)

    async with Session() as db:
        hits = await search_chunks(db, "8.3", top_k=5)

    assert hits
    assert hits[0]["match_type"] == "section"
    assert hits[0]["node_id"] == "d1:8.3"
    assert hits[0]["heading_path"].endswith("8.3 振动试验要求")
    assert hits[0]["page_start"] == 12
    assert hits[0]["chunk_ids"] == chunk_ids[1:]

    await engine.dispose()


@pytest.mark.asyncio
async def test_section_query_with_title_keeps_exact_hit_first(tmp_path):
    engine, _ = await _seed(tmp_path)
    Session = session_factory(engine)

    async with Session() as db:
        hits = await search_chunks(db, "8.3 振动试验要求", top_k=5)

    assert hits
    assert hits[0]["match_type"] == "section"
    assert hits[0]["node_id"] == "d1:8.3"

    await engine.dispose()


async def _seed_number_collision(tmp_path):
    """Two documents share section number ``4.2`` with different titles."""
    engine = make_engine(tmp_path / "collision.db")
    await init_db(engine)
    Session = session_factory(engine)

    async with Session() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        specs = [
            ("a", "论文甲", "a:4.2", "4.2 实验", "实验方法见下文，实验重复三次。"),
            ("b", "论文乙", "b:4.2", "4.2 薄膜介电性能与压电响应的研究",
             "本节研究薄膜的介电性能与压电响应，给出测试结果。"),
        ]
        for doc_id, doc_title, node_id, heading, body in specs:
            doc = Document(
                id=doc_id,
                namespace_id="shared",
                title=doc_title,
                filename=f"{doc_id}.pdf",
                file_path=f"{doc_id}.pdf",
                content_hash=doc_id,
                status="ready",
            )
            db.add(doc)
            await db.flush()
            db.add(
                TocNode(
                    doc_id=doc_id,
                    node_id=node_id,
                    parent_id=None,
                    level=1,
                    heading_num="4.2",
                    title=heading,
                    heading_path=heading,
                    page_start=45 if doc_id == "a" else 63,
                    page_end=45 if doc_id == "a" else 63,
                    chunk_start=0,
                    chunk_end=0,
                    source="toc_page",
                    confidence=0.9,
                )
            )
            chunk = Chunk(
                doc_id=doc_id,
                node_id=node_id,
                chunk_index=0,
                content=body,
                heading_path=heading,
                page_start=45 if doc_id == "a" else 63,
                page_end=45 if doc_id == "a" else 63,
                token_count=10,
            )
            db.add(chunk)
            await db.flush()
            await index_document(db, doc, [chunk])
        await db.commit()

    return engine


@pytest.mark.asyncio
async def test_section_number_collision_follows_the_title_in_the_query(tmp_path):
    """A query that spells out the title picks the document that owns it.

    ``4.2`` exists in both documents and the earlier pages belong to the
    other one; without disambiguation the wrong document ranks first.
    """
    engine = await _seed_number_collision(tmp_path)
    Session = session_factory(engine)

    async with Session() as db:
        hits = await search_chunks(db, "4.2 薄膜介电性能与压电响应的研究", top_k=5)
        assert hits[0]["doc_id"] == "b"
        assert hits[0]["node_id"] == "b:4.2"

        plain = await search_chunks(db, "4.2", top_k=5)
        assert {hit["doc_id"] for hit in plain} == {"a", "b"}

    await engine.dispose()


@pytest.mark.asyncio
async def test_quoted_heading_outranks_foreign_language_body_match(tmp_path):
    """A heading quoted in the query beats a body that repeats one word.

    The Chinese notes repeat ``热力学``, so BM25 prefers them; the English
    section title appears verbatim in the query and must win.
    """
    engine = make_engine(tmp_path / "mixed.db")
    await init_db(engine)
    Session = session_factory(engine)

    async with Session() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        specs = [
            ("en", "JECS paper", "en:h", "Thermodynamic considerations",
             "Thermodynamic considerations The free energy is negative."),
            ("cn", "笔记", "cn:h", "二、相图热力学",
             "相图热力学 热力学 结论 热力学 结论 热力学 结论 热力学 结论"),
        ]
        for doc_id, doc_title, node_id, heading, body in specs:
            doc = Document(
                id=doc_id,
                namespace_id="shared",
                title=doc_title,
                filename=f"{doc_id}.pdf",
                file_path=f"{doc_id}.pdf",
                content_hash=doc_id,
                status="ready",
            )
            db.add(doc)
            await db.flush()
            db.add(
                TocNode(
                    doc_id=doc_id,
                    node_id=node_id,
                    parent_id=None,
                    level=1,
                    heading_num=None,
                    title=heading,
                    heading_path=heading,
                    page_start=1,
                    page_end=1,
                    chunk_start=0,
                    chunk_end=0,
                    source="toc_page",
                    confidence=0.9,
                )
            )
            chunk = Chunk(
                doc_id=doc_id,
                node_id=node_id,
                chunk_index=0,
                content=body,
                heading_path=heading,
                page_start=1,
                page_end=1,
                token_count=12,
            )
            db.add(chunk)
            await db.flush()
            await index_document(db, doc, [chunk])
        await db.commit()

    async with Session() as db:
        hits = await search_chunks(
            db, "Thermodynamic considerations 给出了哪些热力学结论？", top_k=5
        )

    assert hits[0]["doc_id"] == "en"
    assert hits[0]["node_id"] == "en:h"

    await engine.dispose()


@pytest.mark.asyncio
async def test_bm25_hit_carries_section_citation(tmp_path):
    engine, chunk_ids = await _seed(tmp_path)
    Session = session_factory(engine)

    async with Session() as db:
        hits = await search_chunks(db, "输入点", top_k=5)

    assert hits
    hit = hits[0]
    assert hit["node_id"] == "d1:8.3"
    assert hit["heading_path"].endswith("8.3 振动试验要求")
    assert hit["page_start"] == 12
    assert hit["page_end"] == 13
    assert hit["chunk_id"] == chunk_ids[2]
    assert hit["chunk_ids"] == [chunk_ids[2]]
    assert hit["match_type"] == "bm25"

    await engine.dispose()


@pytest.mark.asyncio
async def test_title_query_prefers_exact_toc_title(tmp_path):
    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    Session = session_factory(engine)

    async with Session() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        doc = Document(
            id="d1",
            namespace_id="shared",
            title="温度标准",
            filename="temp.pdf",
            file_path="temp.pdf",
            content_hash="h",
            status="ready",
        )
        db.add(doc)
        await db.flush()
        db.add_all(
            [
                TocNode(
                    doc_id="d1",
                    node_id="d1:4.1",
                    parent_id=None,
                    level=1,
                    heading_num="4.1",
                    title="4.1 试验目的",
                    heading_path="第4章 温度—高度 > 4.1 试验目的",
                    page_start=13,
                    page_end=13,
                    chunk_start=0,
                    chunk_end=0,
                    source="toc_page",
                    confidence=0.9,
                ),
                TocNode(
                    doc_id="d1",
                    node_id="d1:4.5",
                    parent_id=None,
                    level=1,
                    heading_num="4.5",
                    title="4.5 温度试验",
                    heading_path="第4章 温度—高度 > 4.5 温度试验",
                    page_start=15,
                    page_end=15,
                    chunk_start=1,
                    chunk_end=1,
                    source="toc_page",
                    confidence=0.9,
                ),
            ]
        )
        await db.flush()
        chunks = [
            Chunk(
                doc_id="d1",
                node_id="d1:4.1",
                chunk_index=0,
                content="4.1 试验目的 本次试验用于验证设备性能。",
                heading_path="第4章 温度—高度 > 4.1 试验目的",
                page_start=13,
                page_end=13,
            ),
            Chunk(
                doc_id="d1",
                node_id="d1:4.5",
                chunk_index=1,
                content="4.5 温度试验 温度试验包括高温和低温试验。",
                heading_path="第4章 温度—高度 > 4.5 温度试验",
                page_start=15,
                page_end=15,
            ),
        ]
        db.add_all(chunks)
        await db.flush()
        await index_document(db, doc, chunks)
        await db.commit()

        hits = await search_chunks(db, "温度试验", top_k=3)

    assert hits
    assert hits[0]["node_id"] == "d1:4.5"
    assert hits[0]["match_type"] == "title"
    assert hits[0]["page_start"] == 15

    await engine.dispose()


@pytest.mark.asyncio
async def test_read_section_falls_back_to_page_range(tmp_path):
    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    Session = session_factory(engine)

    async with Session() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        doc = Document(
            id="d1",
            namespace_id="shared",
            title="爆炸标准",
            filename="explosion.pdf",
            file_path="explosion.pdf",
            content_hash="h",
            status="ready",
        )
        db.add(doc)
        await db.flush()
        db.add(
            TocNode(
                doc_id="d1",
                node_id="d1:9.6.1.3",
                parent_id=None,
                level=1,
                heading_num="9.6.1.3",
                title="9.6.1.3 失效判据",
                heading_path="第9章 爆炸大气 > 9.6.1.3 失效判据",
                page_start=66,
                page_end=66,
                chunk_start=None,
                chunk_end=None,
                source="toc_page",
                confidence=0.9,
            )
        )
        await db.flush()
        chunk = Chunk(
            doc_id="d1",
            node_id="d1:9.6.1.2",
            chunk_index=0,
            content="9.6.1.3 失效判据 试验样品不应出现火花。",
            heading_path="第9章 爆炸大气 > 9.6.1.2 试验实施",
            page_start=66,
            page_end=66,
        )
        db.add(chunk)
        await db.flush()
        await index_document(db, doc, [chunk])
        await db.commit()

        result = await read_section(db, "d1", node_id="d1:9.6.1.3")
        chunk_id = chunk.id

    assert result["heading_num"] == "9.6.1.3"
    assert result["page_start"] == 66
    assert result["chunk_ids"] == [chunk_id]
    assert "失效判据" in result["content"]
    assert result["fallback"] == "page_range"
    assert result["citation_confidence"] == "low"
    assert result["citation_note"]
    assert result["pdf_page_start"] == 67
    assert result["pdf_page_end"] == 67
    assert result["printed_page_start"] is None
    assert result["printed_page_end"] is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_ask_returns_section_answer_with_citation(tmp_path):
    engine, _ = await _seed(tmp_path)
    Session = session_factory(engine)

    async with Session() as db:
        result = await ask_question(db, "8.3 振动试验要求是什么", top_k=3)

    assert result["answer"]
    assert "下列通用要求" in result["answer"]
    assert result["citations"]
    citation = result["citations"][0]
    assert citation["doc_title"] == "振动标准"
    assert citation["node_id"] == "d1:8.3"
    assert citation["heading_path"].endswith("8.3 振动试验要求")
    assert citation["page_start"] == 12
    node_ids = [item["node_id"] for item in result["citations"]]
    assert len(node_ids) == len(set(node_ids))

    await engine.dispose()


@pytest.mark.asyncio
async def test_ask_no_hit_returns_hint(tmp_path):
    engine, _ = await _seed(tmp_path)
    Session = session_factory(engine)

    async with Session() as db:
        result = await ask_question(db, "完全不存在的术语xyz", top_k=3)

    assert result["answer"] == ""
    assert result["citations"] == []
    assert result["hint"]

    await engine.dispose()


async def _seed_custom(tmp_path, nodes, chunks):
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
            status="ready",
        )
        db.add(doc)
        await db.flush()
        db.add_all([TocNode(doc_id="d1", **node) for node in nodes])
        await db.flush()
        chunk_models = [Chunk(doc_id="d1", **chunk) for chunk in chunks]
        db.add_all(chunk_models)
        await db.flush()
        await index_document(db, doc, chunk_models)
        await db.commit()

    return engine


async def test_read_section_page_range_keeps_preceding_sibling(tmp_path):
    """A real preceding sibling's same-page chunk still feeds the section."""
    engine = await _seed_custom(
        tmp_path,
        nodes=[
            {
                "node_id": "d1:9.2",
                "level": 1,
                "heading_num": "9.2",
                "title": "9.2 试验实施",
                "heading_path": "第9章 爆炸大气 > 9.2 试验实施",
                "page_start": 65,
                "page_end": 65,
                "chunk_start": None,
                "chunk_end": None,
            },
            {
                "node_id": "d1:9.3",
                "level": 1,
                "heading_num": "9.3",
                "title": "9.3 失效判据",
                "heading_path": "第9章 爆炸大气 > 9.3 失效判据",
                "page_start": 66,
                "page_end": 66,
                "chunk_start": None,
                "chunk_end": None,
            },
        ],
        chunks=[
            {
                "node_id": "d1:9.2",
                "chunk_index": 0,
                "content": "9.3 失效判据 试验样品不应出现火花。",
                "heading_path": "第9章 爆炸大气 > 9.2 试验实施",
                "page_start": 66,
                "page_end": 66,
            },
        ],
    )
    Session = session_factory(engine)

    async with Session() as db:
        result = await read_section(db, "d1", node_id="d1:9.3")

    assert result["fallback"] == "page_range"
    assert result["citation_confidence"] == "low"
    assert len(result["chunk_ids"]) == 1
    assert "失效判据" in result["content"]

    await engine.dispose()


async def test_read_section_page_range_excludes_following_sibling(tmp_path):
    engine = await _seed_custom(
        tmp_path,
        nodes=[
            {
                "node_id": "d1:9.3",
                "level": 1,
                "heading_num": "9.3",
                "title": "9.3 失效判据",
                "heading_path": "第9章 爆炸大气 > 9.3 失效判据",
                "page_start": 66,
                "page_end": 66,
                "chunk_start": None,
                "chunk_end": None,
            },
            {
                "node_id": "d1:9.4",
                "level": 1,
                "heading_num": "9.4",
                "title": "9.4 后续要求",
                "heading_path": "第9章 爆炸大气 > 9.4 后续要求",
                "page_start": 66,
                "page_end": 66,
                "chunk_start": None,
                "chunk_end": None,
            },
        ],
        chunks=[
            {
                "node_id": None,
                "chunk_index": 0,
                "content": "本节前文：试验样品不应出现火花。",
                "heading_path": "第9章 爆炸大气",
                "page_start": 66,
                "page_end": 66,
            },
            {
                "node_id": "d1:9.4",
                "chunk_index": 1,
                "content": "后续章节：这是下一节的正文，不属于本节。",
                "heading_path": "第9章 爆炸大气 > 9.4 后续要求",
                "page_start": 66,
                "page_end": 66,
            },
        ],
    )
    Session = session_factory(engine)

    async with Session() as db:
        result = await read_section(db, "d1", node_id="d1:9.3")

    assert result["fallback"] == "page_range"
    assert result["citation_confidence"] == "low"
    assert "本节前文" in result["content"]
    assert "后续章节" not in result["content"]
    assert len(result["chunk_ids"]) == 1

    await engine.dispose()


async def test_read_section_page_range_excludes_next_chapter(tmp_path):
    """A different-branch chapter overlapping the page range is excluded."""
    engine = await _seed_custom(
        tmp_path,
        nodes=[
            {
                "node_id": "d1:9",
                "level": 1,
                "heading_num": "第9章",
                "title": "第9章 爆炸大气",
                "heading_path": "第9章 爆炸大气",
                "page_start": 60,
                "page_end": 70,
                "chunk_start": None,
                "chunk_end": None,
            },
            {
                "node_id": "d1:9.3",
                "parent_id": "d1:9",
                "level": 2,
                "heading_num": "9.3",
                "title": "9.3 失效判据",
                "heading_path": "第9章 爆炸大气 > 9.3 失效判据",
                "page_start": 66,
                "page_end": 66,
                "chunk_start": None,
                "chunk_end": None,
            },
            {
                "node_id": "d1:10",
                "level": 1,
                "heading_num": "第10章",
                "title": "第10章 其他",
                "heading_path": "第10章 其他",
                "page_start": 65,
                "page_end": 70,
                "chunk_start": None,
                "chunk_end": None,
            },
        ],
        chunks=[
            {
                "node_id": None,
                "chunk_index": 0,
                "content": "本节前文：试验样品不应出现火花。",
                "heading_path": "第9章 爆炸大气",
                "page_start": 66,
                "page_end": 66,
            },
            {
                "node_id": "d1:10",
                "chunk_index": 1,
                "content": "邻章正文：其他章节的内容，不属于本节。",
                "heading_path": "第10章 其他",
                "page_start": 66,
                "page_end": 66,
            },
        ],
    )
    Session = session_factory(engine)

    async with Session() as db:
        result = await read_section(db, "d1", node_id="d1:9.3")

    assert result["fallback"] == "page_range"
    assert result["citation_confidence"] == "low"
    assert "本节前文" in result["content"]
    assert "邻章正文" not in result["content"]
    assert len(result["chunk_ids"]) == 1

    await engine.dispose()


async def test_read_section_chunk_range_is_high_confidence(tmp_path):
    engine, _ = await _seed(tmp_path)
    Session = session_factory(engine)

    async with Session() as db:
        result = await read_section(db, "d1", node_id="d1:8.3")

    assert result["fallback"] == "chunk_range"
    assert result["citation_confidence"] == "high"
    assert result["citation_note"] is None
    assert result["pdf_page_start"] == 13
    assert result["pdf_page_end"] == 14

    await engine.dispose()


async def test_read_section_citation_pages_do_not_widen(tmp_path):
    """A fallback chunk that spills onto the next page must not widen the
    section's cited page range."""
    engine = await _seed_custom(
        tmp_path,
        nodes=[
            {
                "node_id": "d1:9.3",
                "level": 1,
                "heading_num": "9.3",
                "title": "9.3 失效判据",
                "heading_path": "第9章 爆炸大气 > 9.3 失效判据",
                "page_start": 66,
                "page_end": 66,
                "chunk_start": None,
                "chunk_end": None,
            },
        ],
        chunks=[
            {
                "node_id": None,
                "chunk_index": 0,
                "content": "失效判据：试验样品不应出现火花，下一行延续到下一页。",
                "heading_path": "第9章 爆炸大气",
                "page_start": 66,
                "page_end": 67,
            },
        ],
    )
    Session = session_factory(engine)

    async with Session() as db:
        result = await read_section(db, "d1", node_id="d1:9.3")

    assert result["page_start"] == 66
    assert result["page_end"] == 66
    assert result["pdf_page_start"] == 67
    assert result["pdf_page_end"] == 67
    assert result["chunks"][0]["page_end"] == 67

    await engine.dispose()


async def test_read_section_returns_chunk_node_ids(tmp_path):
    """The per-chunk node ids feeding 05 must not include later sections."""
    engine = await _seed_custom(
        tmp_path,
        nodes=[
            {
                "node_id": "d1:9.3",
                "level": 1,
                "heading_num": "9.3",
                "title": "9.3 失效判据",
                "heading_path": "第9章 爆炸大气 > 9.3 失效判据",
                "page_start": 66,
                "page_end": 66,
                "chunk_start": None,
                "chunk_end": None,
            },
            {
                "node_id": "d1:9.4",
                "level": 1,
                "heading_num": "9.4",
                "title": "9.4 后续要求",
                "heading_path": "第9章 爆炸大气 > 9.4 后续要求",
                "page_start": 66,
                "page_end": 66,
                "chunk_start": None,
                "chunk_end": None,
            },
        ],
        chunks=[
            {
                "node_id": None,
                "chunk_index": 0,
                "content": "本节前文：试验样品不应出现火花。",
                "heading_path": "第9章 爆炸大气",
                "page_start": 66,
                "page_end": 66,
            },
            {
                "node_id": "d1:9.4",
                "chunk_index": 1,
                "content": "后续章节：这是下一节的正文。",
                "heading_path": "第9章 爆炸大气 > 9.4 后续要求",
                "page_start": 66,
                "page_end": 66,
            },
        ],
    )
    Session = session_factory(engine)

    async with Session() as db:
        result = await read_section(db, "d1", node_id="d1:9.3")

    node_ids = {chunk["node_id"] for chunk in result["chunks"]}
    assert "d1:9.4" not in node_ids
    assert node_ids == {None}

    await engine.dispose()


async def test_read_section_pdf_pages_are_1based(tmp_path):
    engine = await _seed_custom(
        tmp_path,
        nodes=[
            {
                "node_id": "d1:9.3",
                "level": 1,
                "heading_num": "9.3",
                "title": "9.3 失效判据",
                "heading_path": "第9章 爆炸大气 > 9.3 失效判据",
                "page_start": None,
                "page_end": None,
                "chunk_start": None,
                "chunk_end": None,
            },
        ],
        chunks=[],
    )
    Session = session_factory(engine)

    async with Session() as db:
        result = await read_section(db, "d1", node_id="d1:9.3")

    assert result["page_start"] is None
    assert result["page_end"] is None
    assert result["pdf_page_start"] is None
    assert result["pdf_page_end"] is None
    assert result["fallback"] is None
    assert result["citation_confidence"] is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_question_words_are_ignored_for_title_channel(tmp_path):
    engine = await _seed_custom(
        tmp_path,
        nodes=[
            {
                "node_id": "d1:4.1",
                "level": 1,
                "heading_num": "4.1",
                "title": "4.1 试验目的",
                "heading_path": "第4章 温度—高度 > 4.1 试验目的",
                "page_start": 13,
                "page_end": 13,
                "chunk_start": 0,
                "chunk_end": 0,
            },
            {
                "node_id": "d1:4.5",
                "level": 1,
                "heading_num": "4.5",
                "title": "4.5 温度试验",
                "heading_path": "第4章 温度—高度 > 4.5 温度试验",
                "page_start": 15,
                "page_end": 15,
                "chunk_start": 1,
                "chunk_end": 1,
            },
        ],
        chunks=[
            {
                "node_id": "d1:4.1",
                "chunk_index": 0,
                "content": "4.1 试验目的 本次试验用于验证设备性能。",
                "heading_path": "第4章 温度—高度 > 4.1 试验目的",
                "page_start": 13,
                "page_end": 13,
            },
            {
                "node_id": "d1:4.5",
                "chunk_index": 1,
                "content": "4.5 温度试验 温度试验包括高温和低温试验。",
                "heading_path": "第4章 温度—高度 > 4.5 温度试验",
                "page_start": 15,
                "page_end": 15,
            },
        ],
    )
    Session = session_factory(engine)

    async with Session() as db:
        hits = await search_chunks(db, "温度试验包括哪些试验", top_k=3)

    assert hits[0]["node_id"] == "d1:4.5"
    assert hits[0]["match_type"] == "title"

    await engine.dispose()


@pytest.mark.asyncio
async def test_prefix_title_beats_sibling(tmp_path):
    engine = await _seed_custom(
        tmp_path,
        nodes=[
            {
                "node_id": "d1:4.6.2",
                "level": 2,
                "heading_num": "4.6.2",
                "title": "4.6.2 减压试验",
                "heading_path": "第4章 温度—高度 > 4.6 高度、减压和过压试验 > 4.6.2 减压试验",
                "page_start": 16,
                "page_end": 16,
                "chunk_start": 0,
                "chunk_end": 0,
            },
            {
                "node_id": "d1:4.6.3",
                "level": 2,
                "heading_num": "4.6.3",
                "title": "4.6.3 过压试验",
                "heading_path": "第4章 温度—高度 > 4.6 高度、减压和过压试验 > 4.6.3 过压试验",
                "page_start": 17,
                "page_end": 17,
                "chunk_start": 1,
                "chunk_end": 1,
            },
        ],
        chunks=[
            {
                "node_id": "d1:4.6.2",
                "chunk_index": 0,
                "content": "4.6.2 减压试验 将试验箱压力降低到规定值。",
                "heading_path": "第4章 温度—高度 > 4.6 高度、减压和过压试验 > 4.6.2 减压试验",
                "page_start": 16,
                "page_end": 16,
            },
            {
                "node_id": "d1:4.6.3",
                "chunk_index": 1,
                "content": "4.6.3 过压试验 过压试验的压力为规定值。",
                "heading_path": "第4章 温度—高度 > 4.6 高度、减压和过压试验 > 4.6.3 过压试验",
                "page_start": 17,
                "page_end": 17,
            },
        ],
    )
    Session = session_factory(engine)

    async with Session() as db:
        hits = await search_chunks(db, "过压试验的压力是多少", top_k=3)

    assert hits[0]["node_id"] == "d1:4.6.3"
    assert hits[0]["match_type"] == "title"

    await engine.dispose()


@pytest.mark.asyncio
async def test_matching_ancestor_suppresses_child(tmp_path):
    engine = await _seed_custom(
        tmp_path,
        nodes=[
            {
                "node_id": "d1:6.3",
                "level": 2,
                "heading_num": "6.3",
                "title": "6.3 试验程序",
                "heading_path": "第6章 湿热 > 6.3 试验程序",
                "page_start": 31,
                "page_end": 36,
                "chunk_start": 0,
                "chunk_end": 1,
            },
            {
                "node_id": "d1:6.3.4",
                "parent_id": "d1:6.3",
                "level": 3,
                "heading_num": "6.3.4",
                "title": "6.3.4 抽检",
                "heading_path": "第6章 湿热 > 6.3 试验程序 > 6.3.4 抽检",
                "page_start": 32,
                "page_end": 32,
                "chunk_start": 1,
                "chunk_end": 1,
            },
        ],
        chunks=[
            {
                "node_id": "d1:6.3",
                "chunk_index": 0,
                "content": "6.3 试验程序 湿热试验分为若干类别。",
                "heading_path": "第6章 湿热 > 6.3 试验程序",
                "page_start": 31,
                "page_end": 31,
            },
            {
                "node_id": "d1:6.3.4",
                "chunk_index": 1,
                "content": "6.3.4 抽检 每批抽检一件。",
                "heading_path": "第6章 湿热 > 6.3 试验程序 > 6.3.4 抽检",
                "page_start": 32,
                "page_end": 32,
            },
        ],
    )
    Session = session_factory(engine)

    async with Session() as db:
        hits = await search_chunks(db, "湿热试验程序", top_k=3)

    assert hits[0]["node_id"] == "d1:6.3"
    assert hits[0]["match_type"] == "title"

    await engine.dispose()


@pytest.mark.asyncio
async def test_ambiguous_prefix_defers_to_bm25(tmp_path):
    engine = await _seed_custom(
        tmp_path,
        nodes=[
            {
                "node_id": "d1:26.6.4.1.4",
                "level": 3,
                "heading_num": "26.6.4.1.4",
                "title": "26.6.4.1.4 燃烧长度",
                "heading_path": "第26章 防火，可燃性 > 26.6.4.1 定义 > 26.6.4.1.4 燃烧长度",
                "page_start": 299,
                "page_end": 299,
                "chunk_start": 0,
                "chunk_end": 0,
            },
            {
                "node_id": "d1:26.6.4.7.3",
                "level": 3,
                "heading_num": "26.6.4.7.3",
                "title": "26.6.4.7.3 燃烧长度",
                "heading_path": "第26章 防火，可燃性 > 26.6.4.7 达标要求 > 26.6.4.7.3 燃烧长度",
                "page_start": 302,
                "page_end": 302,
                "chunk_start": 1,
                "chunk_end": 1,
            },
        ],
        chunks=[
            {
                "node_id": "d1:26.6.4.1.4",
                "chunk_index": 0,
                "content": "燃烧长度是指试验样品燃烧后剩余的长度。",
                "heading_path": "第26章 防火，可燃性 > 26.6.4.1 定义 > 26.6.4.1.4 燃烧长度",
                "page_start": 299,
                "page_end": 299,
            },
            {
                "node_id": "d1:26.6.4.7.3",
                "chunk_index": 1,
                "content": "燃烧长度不应超过 3in（76mm）。",
                "heading_path": "第26章 防火，可燃性 > 26.6.4.7 达标要求 > 26.6.4.7.3 燃烧长度",
                "page_start": 302,
                "page_end": 302,
            },
        ],
    )
    Session = session_factory(engine)

    async with Session() as db:
        hits = await search_chunks(db, "燃烧长度不应超过多少", top_k=3)

    assert hits[0]["node_id"] == "d1:26.6.4.7.3"
    assert hits[0]["match_type"] == "bm25"

    await engine.dispose()

async def _seed_segments(tmp_path):
    engine = make_engine(tmp_path / "segments.db")
    await init_db(engine)
    Session = session_factory(engine)
    async with Session() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(
            Document(
                id="doc1",
                namespace_id="shared",
                title="GJB 150.16A",
                filename="gjb.pdf",
                file_path="gjb.pdf",
                content_hash="segments",
                page_count=20,
                status="ready",
            )
        )
        await db.flush()
        db.add(
            TocNode(
                doc_id="doc1",
                node_id="doc1:h:4",
                parent_id=None,
                level=1,
                heading_num="4",
                title="4 剪裁指南",
                heading_path="4 剪裁指南",
                page_start=6,
                page_end=6,
                chunk_start=0,
                chunk_end=1,
                source="bookmark",
                confidence=1.0,
            )
        )
        await db.flush()
        db.add_all(
            [
                Chunk(
                    doc_id="doc1",
                    node_id="doc1:h:4",
                    chunk_index=0,
                    content="4.1 选择试验方法\n\n分析有关技术文件的要求。",
                    heading_path="4 剪裁指南 > 4.1 选择试验方法",
                    page_start=6,
                    page_end=6,
                    token_count=10,
                ),
                Chunk(
                    doc_id="doc1",
                    node_id="doc1:h:4",
                    chunk_index=1,
                    content="4.2 选择振动试验方法\n\n试验方法根据表1选择振动环境类别。",
                    heading_path="4 剪裁指南 > 4.2 选择振动试验方法",
                    page_start=6,
                    page_end=6,
                    token_count=10,
                ),
            ]
        )
        await db.commit()
    return engine, Session


async def test_read_section_returns_chunk_segments_with_own_pages(tmp_path):
    engine, Session = await _seed_segments(tmp_path)
    async with Session() as db:
        result = await read_section(
            db, "doc1", node_id="doc1:h:4", max_chars=6000
        )

    assert [c["chunk_id"] for c in result["chunks"]] == [1, 2]
    assert result["chunks"][1]["heading_path"].endswith("4.2 选择振动试验方法")
    assert "试验方法根据表1" in result["chunks"][1]["text"]
    assert "试验方法根据表1" not in result["chunks"][0]["text"]
    assert "试验方法根据表1" in result["content"]
    assert result["chunks"][0]["page_start"] == 6
    assert result["chunks"][0]["node_id"] == "doc1:h:4"
    await engine.dispose()


async def test_read_section_chunk_segments_respect_cursor_and_max_chars(tmp_path):
    engine, Session = await _seed_segments(tmp_path)
    async with Session() as db:
        result = await read_section(
            db, "doc1", node_id="doc1:h:4", max_chars=20
        )

    assert len(result["content"]) <= 20
    assert all(len(c["text"]) <= 20 for c in result["chunks"])
    assert result["chunks"][0]["chunk_id"] == 1
    assert result["next_cursor"] is not None
    await engine.dispose()
