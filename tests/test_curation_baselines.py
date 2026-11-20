from __future__ import annotations

import pytest
from sqlalchemy import select

from app.curation import baselines as baselines_mod
from app.curation.identity import record_aliases, resolve_node_id
from app.db import init_db, make_engine, session_factory
from app.models import Chunk, Document, Namespace, NodeIdAlias, TocNode


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
                title="试验大纲",
                filename="a.pdf",
                file_path="a.pdf",
                content_hash="h1",
                page_count=3,
                status="ready",
                summary="old summary",
            )
        )
        await db.flush()
        db.add_all(
            [
                TocNode(
                    doc_id="d1",
                    node_id="d1:h:1",
                    level=1,
                    heading_num="1",
                    title="1 范围",
                    heading_path="1 范围",
                    page_start=0,
                    page_end=1,
                ),
                TocNode(
                    doc_id="d1",
                    node_id="d1:h:2",
                    parent_id="d1:h:1",
                    level=2,
                    heading_num="2",
                    title="2 引用",
                    heading_path="1 范围 > 2 引用",
                    page_start=1,
                    page_end=2,
                ),
            ]
        )
        await db.flush()
        db.add_all(
            [
                Chunk(
                    doc_id="d1",
                    node_id="d1:h:1",
                    chunk_index=0,
                    content="范围正文",
                    heading_path="1 范围",
                    page_start=0,
                    page_end=1,
                ),
                Chunk(
                    doc_id="d1",
                    node_id="d1:h:2",
                    chunk_index=1,
                    content="引用正文",
                    heading_path="1 范围 > 2 引用",
                    page_start=1,
                    page_end=2,
                ),
            ]
        )
        await db.commit()
    return engine


@pytest.mark.asyncio
async def test_create_baseline_captures_rows_and_is_idempotent(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        row = await baselines_mod.create_baseline(db, "d1", "pre_curator_write")
        await db.commit()
        assert row is not None
        assert row.node_count == 2
        assert row.chunk_count == 2
        assert baselines_mod.verify_baseline(row)

        again = await baselines_mod.create_baseline(db, "d1", "pre_curator_write")
        assert again is None, "same (doc, hash, reason) must not duplicate"
        await db.commit()
    await engine.dispose()


@pytest.mark.asyncio
async def test_restore_baseline_reverts_curator_edits(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        created = await baselines_mod.create_baseline(db, "d1", "pre_curator_write")
        await db.commit()

        # A curator write: rename a node, change summary, add a chunk.
        node = (
            await db.execute(select(TocNode).where(TocNode.node_id == "d1:h:1"))
        ).scalar_one()
        node.title = "1 范围（agent 改过）"
        doc = await db.get(Document, "d1")
        doc.summary = "new summary"
        db.add(Chunk(doc_id="d1", node_id="d1:h:1", chunk_index=9, content="额外切片"))
        await db.commit()

        result = await baselines_mod.restore_baseline(db, "d1", created.id)
        await db.commit()

    async with session_factory(engine)() as db:
        titles = (
            (await db.execute(select(TocNode.title).where(TocNode.doc_id == "d1")))
            .scalars()
            .all()
        )
        assert "1 范围" in titles
        assert "1 范围（agent 改过）" not in titles
        doc = await db.get(Document, "d1")
        assert doc.summary == "old summary"
        chunks = (
            (await db.execute(select(Chunk).where(Chunk.doc_id == "d1"))).scalars().all()
        )
        assert len(chunks) == 2
        assert doc.status == "ready"
    assert result["restored_nodes"] == 2
    assert result["restored_chunks"] == 2
    assert result["forced"] is False
    await engine.dispose()


@pytest.mark.asyncio
async def test_restore_writes_a_pre_restore_snapshot(tmp_path):
    from app.curation.snapshots import list_snapshots

    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        created = await baselines_mod.create_baseline(db, "d1", "pre_curator_write")
        await db.commit()
        await baselines_mod.restore_baseline(db, "d1", created.id)
        await db.commit()
        snapshots = await list_snapshots(db, "d1")
    assert len(snapshots) == 1
    assert snapshots[0]["proposal_id"] == f"restore:{created.id}"
    await engine.dispose()


@pytest.mark.asyncio
async def test_restore_refuses_content_hash_mismatch_without_force(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        created = await baselines_mod.create_baseline(db, "d1", "pre_curator_write")
        doc = await db.get(Document, "d1")
        doc.content_hash = "h2"
        await db.commit()

        with pytest.raises(ValueError, match="content hash mismatch"):
            await baselines_mod.restore_baseline(db, "d1", created.id)
        await db.rollback()

        result = await baselines_mod.restore_baseline(db, "d1", created.id, force=True)
        await db.commit()
    assert result["forced"] is True
    assert result["hash_mismatch"] is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_restore_without_baseline_raises_lookup_error(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        with pytest.raises(LookupError):
            await baselines_mod.restore_baseline(db, "d1")
    await engine.dispose()


@pytest.mark.asyncio
async def test_restore_marks_open_jobs_rolled_back(tmp_path):
    """Restore must not be followed by the curator re-applying the same fix."""
    from app.models import CurationJob

    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        created = await baselines_mod.create_baseline(db, "d1", "pre_curator_write")
        db.add(
            CurationJob(
                id="j1", issue_id="d1:short-chunks", doc_id="d1",
                kind="short-chunks", fingerprint="fp", status="applied",
            )
        )
        await db.commit()
        await baselines_mod.restore_baseline(db, "d1", created.id)
        await db.commit()
        job = await db.get(CurationJob, "j1")
        assert job.status == "rolled_back"
        assert job.last_error == "restored_by_human"
    await engine.dispose()


@pytest.mark.asyncio
async def test_alias_resolves_old_node_id(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        await record_aliases(db, "d1", {"d1:0000": "d1:h:1"})
        await db.commit()
        assert await resolve_node_id(db, "d1", "d1:0000") == "d1:h:1"
        assert await resolve_node_id(db, "d1", "d1:h:2") == "d1:h:2"
        aliases = (await db.execute(select(NodeIdAlias))).scalars().all()
        assert len(aliases) == 1
    await engine.dispose()
