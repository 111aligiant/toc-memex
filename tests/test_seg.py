from __future__ import annotations

import pytest

from app.db import init_db, make_engine, session_factory
from app.index.fts import index_document, search_chunks
from app.index.seg import char_chunks, cjk_n_grams, contains_cjk, is_cjk
from app.models import Chunk, Document, Namespace


# --- app.index.seg: CJK character n-gram tokenizer (public capability) ---


def test_cjk_n_grams_unigram_bigram():
    # A contiguous Chinese run yields its characters and adjacent bigrams.
    assert cjk_n_grams("湿度") == ["湿", "度", "湿度"]
    assert cjk_n_grams("数据分析")[:4] == ["数", "据", "分", "析"]
    assert "数据" in cjk_n_grams("数据分析")
    assert "析分" not in cjk_n_grams("数据分析")  # order, no reversed bigram


def test_cjk_n_grams_keeps_latin_and_digits_whole():
    # English words and numbers stay word-sized tokens, not single chars.
    toks = [t for t in cjk_n_grams("reactor design 2026") if t != " "]
    assert "reactor" in toks
    assert "2026" in toks
    assert not any(t in {"2", "0", "2", "6"} for t in toks)


def test_cjk_n_grams_mixed_span():
    toks = cjk_n_grams("湿度 humidity")
    assert "湿度" in toks
    assert "humidity" in toks


def test_is_cjk_and_contains():
    assert is_cjk("湿")
    assert not is_cjk("s")
    assert not is_cjk("湿度")  # one char only
    assert contains_cjk("a 湿度 b")
    assert not contains_cjk("reactor design")


def test_char_chunks_single_characters():
    assert char_chunks("湿度") == ["湿", "度"]


# --- End-to-end: cross-lingual via query-side synonym dictionary ---

SEED_CHUNK = (
    "Humidity sensors respond to changes in the water vapour content of "
    "the surrounding air."
)


@pytest.mark.asyncio
async def test_chinese_query_hits_english_chunk_via_synonym(tmp_path, monkeypatch):
    """0.2 acceptance: a Chinese query matches an English document section
    through the configured query-side bilingual dictionary.
    """
    from app.config import get_settings

    engine = make_engine(tmp_path / "bi.db")
    await init_db(engine)
    Session = session_factory(engine)
    async with Session() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        doc = Document(
            id="d1",
            namespace_id="shared",
            title="Humidity sensor",
            filename="sensor.pdf",
            file_path="sensor.pdf",
            content_hash="h",
            status="ready",
        )
        db.add(doc)
        await db.flush()
        chunks = [
            Chunk(
                doc_id="d1",
                chunk_index=0,
                content=SEED_CHUNK,
                heading_path="1 Introduction",
            )
        ]
        db.add_all(chunks)
        await db.flush()
        await index_document(db, doc, chunks)
        await db.commit()

    # Without the dictionary the Chinese query cannot reach the English text.
    async with Session() as db:
        hits = await search_chunks(db, "湿度", top_k=5)
    assert not hits

    monkeypatch.setattr(
        get_settings(), "tocmemex_search_synonyms", {"湿度": ["humidity"]}
    )
    async with Session() as db:
        hits = await search_chunks(db, "湿度", top_k=5)
    assert hits, "Chinese query with configured synonym must hit English chunk"
    assert hits[0]["doc_id"] == "d1"
    await engine.dispose()


@pytest.mark.asyncio
async def test_meaningless_query_stays_miss_with_synonyms(tmp_path, monkeypatch):
    """The no-hit contract holds even when a dictionary is configured:
    a random query does not suddenly match prose through a common word.
    """
    from app.config import get_settings

    engine = make_engine(tmp_path / "miss.db")
    await init_db(engine)
    Session = session_factory(engine)
    async with Session() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        doc = Document(
            id="d1",
            namespace_id="shared",
            title="Humidity sensor",
            filename="sensor.pdf",
            file_path="sensor.pdf",
            content_hash="h",
            status="ready",
        )
        db.add(doc)
        await db.flush()
        chunk = Chunk(
            doc_id="d1",
            chunk_index=0,
            content=SEED_CHUNK,
            heading_path="1 Introduction",
        )
        db.add(chunk)
        await db.flush()
        await index_document(db, doc, [chunk])
        await db.commit()

    monkeypatch.setattr(
        get_settings(), "tocmemex_search_synonyms", {"湿度": ["humidity"]}
    )
    async with Session() as db:
        hits = await search_chunks(db, "完全不存在的术语xyz", top_k=5)
    assert not hits
    await engine.dispose()