from __future__ import annotations

import pytest

from app.db import init_db, make_engine, session_factory
from app.index.fts import index_document, search_chunks, seg_index
from app.models import Chunk, Document, Namespace, TocNode


@pytest.mark.asyncio
async def test_chinese_bm25_matches_continuous_text(tmp_path):
    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    Session = session_factory(engine)
    async with Session() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        doc = Document(
            id="d1",
            namespace_id="shared",
            title="方法大纲",
            filename="a.pdf",
            file_path="a.pdf",
            content_hash="h",
            status="ready",
        )
        db.add(doc)
        await db.flush()
        chunks = [
            Chunk(
                doc_id="d1",
                chunk_index=0,
                content="流程说明包括步骤和记录",
                heading_path="5 方法",
            ),
            Chunk(doc_id="d1", chunk_index=1, content="补充说明", heading_path="6 补充"),
        ]
        db.add_all(chunks)
        await db.flush()
        await index_document(db, doc, chunks)
        await db.commit()

        hits = await search_chunks(db, "流程说明", top_k=5)
        assert hits
        assert hits[0]["chunk_id"] == chunks[0].id
    await engine.dispose()


def test_seg_index_inserts_spaces():
    assert " " in seg_index("流程说明程序")


@pytest.mark.asyncio
async def test_identifier_only_query_returns_doc_hits(tmp_path):
    """A bare document number routes to its documents even without body tokens."""
    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    Session = session_factory(engine)
    async with Session() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        doc = Document(
            id="d1",
            namespace_id="shared",
            title="ACME-100.1-2014",
            filename="a.pdf",
            file_path="a.pdf",
            content_hash="h",
            status="ready",
        )
        db.add(doc)
        await db.flush()
        chunks = [
            Chunk(doc_id="d1", chunk_index=0, content="范围", heading_path="1 范围"),
            Chunk(doc_id="d1", chunk_index=1, content="方法说明", heading_path="5 方法"),
        ]
        db.add_all(chunks)
        await db.flush()
        await index_document(db, doc, chunks)
        await db.commit()

        hits = await search_chunks(db, "ACME 100", top_k=5)
        assert len(hits) == 1
        assert hits[0]["match_type"] == "doc"
        assert hits[0]["doc_id"] == "d1"
        assert hits[0]["title"] == "ACME-100.1-2014"
        assert "范围" in hits[0]["snippet"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_identifier_query_does_not_crash(tmp_path):
    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    Session = session_factory(engine)
    async with Session() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        doc = Document(
            id="d1",
            namespace_id="shared",
            title="文档",
            filename="a.pdf",
            file_path="a.pdf",
            content_hash="h",
            status="ready",
        )
        db.add(doc)
        await db.flush()
        chunk = Chunk(
            doc_id="d1", chunk_index=0, content="见 5.1 方法说明", heading_path="5.1 方法说明"
        )
        db.add(chunk)
        await db.flush()
        await index_document(db, doc, [chunk])
        await db.commit()

        hits = await search_chunks(db, "5.1", top_k=5)
        assert isinstance(hits, list)
    await engine.dispose()

async def _seed_front_matter(tmp_path):
    engine = make_engine(tmp_path / "front.db")
    await init_db(engine)
    Session = session_factory(engine)
    async with Session() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        doc = Document(
            id="d1",
            namespace_id="shared",
            title="方法汇编",
            filename="v.pdf",
            file_path="v.pdf",
            content_hash="front",
            status="ready",
        )
        db.add(doc)
        await db.flush()
        db.add_all(
            [
                TocNode(
                    doc_id="d1",
                    node_id="n_body",
                    parent_id=None,
                    level=1,
                    heading_num="8",
                    title="8 方法",
                    heading_path="8 方法",
                    page_start=0,
                    page_end=0,
                    chunk_start=0,
                    chunk_end=0,
                    source="bookmark",
                    confidence=1.0,
                ),
                TocNode(
                    doc_id="d1",
                    node_id="n_ref",
                    parent_id=None,
                    level=1,
                    heading_num=None,
                    title="参考文献",
                    heading_path="参考文献",
                    page_start=1,
                    page_end=1,
                    chunk_start=1,
                    chunk_end=1,
                    source="bookmark",
                    confidence=1.0,
                ),
            ]
        )
        await db.flush()
        chunks = [
            Chunk(
                doc_id="d1",
                node_id="n_body",
                chunk_index=0,
                content="本章给出方法的结论。",
                heading_path="8 方法",
            ),
            Chunk(
                doc_id="d1",
                node_id="n_ref",
                chunk_index=1,
                content="方法 方法 方法 参考文献",
                heading_path="参考文献",
            ),
        ]
        db.add_all(chunks)
        await db.flush()
        await index_document(db, doc, chunks)
        await db.commit()
    return engine, Session


async def test_reference_chunk_is_demoted_below_body(tmp_path):
    engine, Session = await _seed_front_matter(tmp_path)
    async with Session() as db:
        hits = await search_chunks(db, "方法 结论", top_k=5)
    assert hits[0]["heading_path"] == "8 方法"
    await engine.dispose()


async def test_front_matter_query_keeps_reference_hits(tmp_path):
    engine, Session = await _seed_front_matter(tmp_path)
    async with Session() as db:
        hits = await search_chunks(db, "参考文献 方法", top_k=5)
    assert any(hit["heading_path"] == "参考文献" for hit in hits)
    await engine.dispose()


def test_configured_synonyms_expand_terms(monkeypatch):
    from app.config import get_settings
    from app.index.fts import _expand_synonyms

    monkeypatch.setattr(
        get_settings(), "tocmemex_search_synonyms", {"alpha": ["beta"]}
    )
    assert _expand_synonyms(["alpha"]) == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_common_document_words_are_not_identifiers(tmp_path):
    from app.index.sections import doc_ids_from_query

    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    async with session_factory(engine)() as db:
        assert await doc_ids_from_query(db, "Chapter 12") is None
        assert await doc_ids_from_query(db, "ACME 100") is not None
    await engine.dispose()
