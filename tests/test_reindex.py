from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy import text

from app.config import Settings
from app.db import init_db, make_engine, session_factory
from app.index.fts import index_document, search_chunks
from app.ingest.pipeline import (
    load_bookmarks,
    save_bookmarks,
    set_engine,
)
from app.ingest.reindex import reindex_document
from app.models import Chunk, Document, Namespace, TocNode
from app.ocr.models import Bookmark

FIXTURE = Path(__file__).parent / "fixtures" / "paddleocr_sample.jsonl"
pytestmark = pytest.mark.skipif(
    not FIXTURE.exists(),
    reason="OCR fixtures are not tracked; see tests/fixtures/README.md",
)


def _settings(tmp_path) -> Settings:
    return Settings(
        tocmemex_db_path=tmp_path / "tocmemex.db",
        tocmemex_data_dir=tmp_path / "data",
    )


def _doc(tmp_path) -> Document:
    return Document(
        id="d1",
        namespace_id="shared",
        title="试验大纲",
        filename="a.pdf",
        file_path=str(tmp_path / "a.pdf"),
        content_hash="h",
        status="ready",
    )


@pytest.mark.asyncio
async def test_reindex_rebuilds_toc_chunks_and_fts_without_ocr(tmp_path):
    settings = _settings(tmp_path)
    ocr_dir = settings.ocr_dir / "d1"
    ocr_dir.mkdir(parents=True)
    shutil.copy(FIXTURE, ocr_dir / "raw.jsonl")

    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        doc = _doc(tmp_path)
        db.add(doc)
        await db.flush()
        stale = Chunk(doc_id="d1", chunk_index=0, content="旧内容", heading_path="old")
        db.add(stale)
        db.add(TocNode(doc_id="d1", node_id="d1:old", level=1, title="old", heading_path="old"))
        await db.flush()
        await index_document(db, doc, [stale])
        await db.commit()

    await reindex_document("d1", settings)

    async with session_factory(engine)() as db:
        assert (await db.execute(text("select count(*) from toc_nodes where doc_id='d1'"))).scalar() > 0
        assert (await db.execute(text("select count(*) from chunks where doc_id='d1'"))).scalar() > 0
        assert (await db.execute(text("select count(*) from chunk_fts where doc_id='d1'"))).scalar() > 0
        assert (await db.execute(text("select count(*) from chunks where content='旧内容'"))).scalar() == 0
        hits = await search_chunks(db, "范围", top_k=5)
        assert hits
        status = (await db.execute(text("select status from documents where id='d1'"))).scalar()
        assert status == "ready"
    await engine.dispose()


@pytest.mark.asyncio
async def test_reindex_twice_is_idempotent(tmp_path):
    settings = _settings(tmp_path)
    ocr_dir = settings.ocr_dir / "d1"
    ocr_dir.mkdir(parents=True)
    shutil.copy(FIXTURE, ocr_dir / "raw.jsonl")

    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(_doc(tmp_path))
        await db.commit()

    await reindex_document("d1", settings)
    async with session_factory(engine)() as db:
        first = (await db.execute(text("select count(*) from chunks where doc_id='d1'"))).scalar()
    await reindex_document("d1", settings)
    async with session_factory(engine)() as db:
        second = (await db.execute(text("select count(*) from chunks where doc_id='d1'"))).scalar()
    assert first == second
    await engine.dispose()


def test_bookmarks_roundtrip(tmp_path):
    settings = _settings(tmp_path)
    save_bookmarks(settings, "d1", [Bookmark(1, "1 范围", 0, "1")])
    loaded = load_bookmarks(settings, "d1")
    assert [(b.level, b.title, b.page_index) for b in loaded] == [(1, "1 范围", 0)]


@pytest.mark.asyncio
async def test_reindex_missing_cache_raises(tmp_path):
    settings = _settings(tmp_path)
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(_doc(tmp_path))
        await db.commit()

    with pytest.raises(FileNotFoundError):
        await reindex_document("d1", settings)
    await engine.dispose()

@pytest.mark.asyncio
async def test_reindex_prefers_pages_json_cache(tmp_path):
    """A provider-neutral pages.json is used before legacy raw.jsonl."""
    from app.ocr.cache import save_pages
    from app.ocr.models import OcrBlock, OcrPage, ParsedDocument

    settings = _settings(tmp_path)
    ocr_dir = settings.ocr_dir / "d1"
    ocr_dir.mkdir(parents=True)
    save_pages(
        ocr_dir,
        ParsedDocument(
            pages=[
                OcrPage(
                    0,
                    100,
                    200,
                    "",
                    [
                        OcrBlock("paragraph_title", "1 范围", (0, 0, 100, 10), 1),
                        OcrBlock("text", "缓存正文", (0, 20, 100, 30), 2),
                    ],
                )
            ]
        ),
    )
    # A stale legacy file must be ignored when pages.json exists.
    (ocr_dir / "raw.jsonl").write_text("{not json", encoding="utf-8")

    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(_doc(tmp_path))
        await db.commit()

    await reindex_document("d1", settings)
    async with session_factory(engine)() as db:
        content = (
            await db.execute(text("select content from chunks where doc_id='d1'"))
        ).scalars().all()
    assert any("缓存正文" in value for value in content)
    await engine.dispose()
