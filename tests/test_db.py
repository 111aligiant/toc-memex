from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db import init_db, make_engine


@pytest.mark.asyncio
async def test_init_db_creates_tables_and_fts(tmp_path):
    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    async with engine.connect() as conn:
        names = {
            r[0]
            for r in (
                await conn.execute(
                    text("select name from sqlite_master where type in ('table','view')")
                )
            ).fetchall()
        }
        assert {"namespaces", "documents", "toc_nodes", "chunks", "resources"} <= names
        assert "chunk_fts" in names
        assert "doc_fts" in names
    await engine.dispose()

@pytest.mark.asyncio
async def test_init_db_backfills_doc_type_from_filename(tmp_path):
    """Old rows had doc_type NULL; the type filter and overview need it."""
    from app.db import session_factory
    from app.models import Document, Namespace

    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    async with session_factory(engine)() as session:
        session.add(Namespace(id="shared", kind="shared"))
        await session.flush()
        session.add(
            Document(
                id="d1", namespace_id="shared", title="t", filename="a.csv",
                file_path="a.csv", content_hash="h", status="ready",
            )
        )
        await session.commit()
    async with engine.connect() as conn:
        assert (
            await conn.execute(text("select doc_type from documents where id='d1'"))
        ).scalar_one() is None
    await engine.dispose()

    # A fresh startup runs the migration against the existing row.
    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    async with engine.connect() as conn:
        doc_type = (
            await conn.execute(text("select doc_type from documents where id='d1'"))
        ).scalar_one()
    assert doc_type == "csv"
    await engine.dispose()
