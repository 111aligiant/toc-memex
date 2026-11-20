from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db import init_db, make_engine, session_factory
from app.index.resources import (
    attach_resources,
    get_resource,
    image_file,
    link_resources,
    list_resources,
    resource_owner,
)
from app.models import Chunk, Document, Namespace, Resource, TocNode

TABLE_HTML = (
    '<table><tr><td rowspan="2">外观</td><td>a</td></tr>'
    "<tr><td>b</td></tr></table>"
)


async def _seed(tmp_path: Path):
    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    Session = session_factory(engine)
    image_path = tmp_path / "data" / "resources" / "doc1" / "fig_1_0.jpg"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"jpeg")

    async with Session() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(
            Document(
                id="doc1",
                namespace_id="shared",
                title="演示文档",
                filename="demo.pdf",
                file_path="demo.pdf",
                content_hash="h",
                page_count=6,
                status="ready",
            )
        )
        await db.flush()
        db.add_all(
            [
                TocNode(
                    doc_id="doc1",
                    node_id="n1",
                    parent_id=None,
                    level=1,
                    heading_num="1",
                    title="1 范围",
                    heading_path="1 范围",
                    page_start=0,
                    page_end=3,
                    chunk_start=0,
                    chunk_end=1,
                    source="bookmark",
                    confidence=1.0,
                ),
                TocNode(
                    doc_id="doc1",
                    node_id="n2",
                    parent_id="n1",
                    level=2,
                    heading_num="1.1",
                    title="1.1 目的",
                    heading_path="1 范围 > 1.1 目的",
                    page_start=1,
                    page_end=1,
                    chunk_start=1,
                    chunk_end=1,
                    source="bookmark",
                    confidence=1.0,
                ),
            ]
        )
        db.add_all(
            [
                Chunk(
                    doc_id="doc1",
                    node_id="n1",
                    chunk_index=0,
                    content="范围正文",
                    heading_path="1 范围",
                    page_start=0,
                    page_end=3,
                    token_count=4,
                ),
                Chunk(
                    doc_id="doc1",
                    node_id="n2",
                    chunk_index=1,
                    content="目的正文",
                    heading_path="1 范围 > 1.1 目的",
                    page_start=1,
                    page_end=1,
                    token_count=4,
                ),
            ]
        )
        db.add_all(
            [
                Resource(
                    doc_id="doc1",
                    resource_id="tbl_1_0",
                    type="table",
                    title="表1 试验条件",
                    content=TABLE_HTML,
                    page_idx=1,
                ),
                Resource(
                    doc_id="doc1",
                    resource_id="fig_1_0",
                    type="figure",
                    title="图1 布置",
                    image_path=str(image_path),
                    page_idx=1,
                ),
                Resource(
                    doc_id="doc1",
                    resource_id="tbl_5_0",
                    type="table",
                    title="表5 结果",
                    content="<table><tr><td>x</td></tr></table>",
                    page_idx=5,
                ),
            ]
        )
        await db.commit()
    return engine, Session


@pytest.mark.asyncio
async def test_list_resources_filters_by_type_page_and_title(tmp_path):
    engine, Session = await _seed(tmp_path)
    async with Session() as db:
        payload = await list_resources(
            db, "doc1", type_="table", page_start=0, page_end=2
        )
        assert [r["resource_id"] for r in payload["resources"]] == ["tbl_1_0"]

        filtered = await list_resources(db, "doc1", query="结果")
        assert [r["resource_id"] for r in filtered["resources"]] == ["tbl_5_0"]

        limited = await list_resources(db, "doc1", limit=1)
        assert limited["count"] == 3
        assert limited["truncated"] is True
        assert len(limited["resources"]) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_get_resource_renders_merged_cells(tmp_path):
    engine, Session = await _seed(tmp_path)
    async with Session() as db:
        payload, image_path = await get_resource(db, "doc1", "tbl_1_0")
    assert "| 外观 | a |" in payload["content"]
    assert "| 外观 | b |" in payload["content"]
    assert payload["content"].count("| --- | --- |") == 1
    assert payload["heading_path"] is None
    assert image_path is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_get_resource_returns_image_path_outside_payload(tmp_path):
    engine, Session = await _seed(tmp_path)
    async with Session() as db:
        payload, image_path = await get_resource(db, "doc1", "fig_1_0")
    assert payload["content"] is None
    assert payload["image_available"] is True
    assert image_path == str(
        tmp_path / "data" / "resources" / "doc1" / "fig_1_0.jpg"
    )
    assert "image_path" not in payload
    await engine.dispose()


@pytest.mark.asyncio
async def test_get_resource_missing_raises(tmp_path):
    engine, Session = await _seed(tmp_path)
    async with Session() as db:
        with pytest.raises(ValueError, match="resource not found"):
            await get_resource(db, "doc1", "nope")
    await engine.dispose()


@pytest.mark.asyncio
async def test_attach_resources_dedupes_and_matches_page_or_node(tmp_path):
    engine, Session = await _seed(tmp_path)
    anchors = [
        {"doc_id": "doc1", "node_id": "n1", "page_start": 0, "page_end": 3},
        {"doc_id": "doc1", "node_id": "n2", "page_start": 1, "page_end": 1},
    ]
    async with Session() as db:
        await attach_resources(db, anchors)
    assert {r["resource_id"] for r in anchors[0]["resources"]} == {
        "tbl_1_0",
        "fig_1_0",
    }
    assert anchors[1]["resources"] == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_link_resources_sets_owner_node(tmp_path):
    engine, Session = await _seed(tmp_path)
    async with Session() as db:
        updated = await link_resources(db, "doc1")
        await db.commit()
    assert updated == 2

    async with Session() as db:
        rows = {
            resource.resource_id: resource.node_id
            for resource in (
                await db.execute(
                    select(Resource).where(Resource.doc_id == "doc1")
                )
            ).scalars()
        }
    # Page 1 is covered by n1 (0–3) and n2 (page 1); the tighter chunk wins.
    assert rows["tbl_1_0"] == "n2"
    assert rows["fig_1_0"] == "n2"
    # Page 5 has no covering chunk, so the resource stays unlinked.
    assert rows["tbl_5_0"] is None
    await engine.dispose()


def test_resource_owner_prefers_page_covering_chunk_in_same_node():
    chunks = [
        SimpleNamespace(node_id="n1", page_start=0, page_end=3, chunk_index=0),
        SimpleNamespace(node_id="n1", page_start=4, page_end=4, chunk_index=1),
    ]
    owner = resource_owner(SimpleNamespace(page_idx=2, node_id="n1"), chunks)
    assert owner.chunk_index == 0


def test_resource_owner_reads_dict_rows_like_the_web_reading_stream():
    chunks = [
        {"node_id": "n1", "page_start": 0, "page_end": 3, "chunk_index": 0},
        {"node_id": "n1", "page_start": 4, "page_end": 4, "chunk_index": 1},
    ]
    owner = resource_owner({"page_idx": 2, "node_id": "n1"}, chunks)
    assert owner["chunk_index"] == 0


def test_image_file_rejects_paths_outside_root(tmp_path):
    root = tmp_path / "resources"
    inside = root / "doc1" / "a.jpg"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"jpeg")
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(b"x")

    assert image_file(None, root) is None
    assert image_file(str(inside), root) == inside.resolve()
    assert image_file(str(outside), root) is None
    assert image_file(str(root / "missing.jpg"), root) is None
