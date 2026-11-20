from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app.config import Settings
from app.db import init_db, make_engine, session_factory
from app.index.activity import task_create
from app.index.fts import index_document
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
                    content="范围正文，涵盖温度试验要求。",
                    heading_path="1 范围",
                    page_start=0,
                    page_end=3,
                    token_count=8,
                ),
                Chunk(
                    doc_id="doc1",
                    node_id="n2",
                    chunk_index=1,
                    content="目的正文。",
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

    async with Session() as db:
        doc = await db.get(Document, "doc1")
        chunks = (
            await db.execute(select(Chunk).where(Chunk.doc_id == "doc1"))
        ).scalars().all()
        await index_document(db, doc, list(chunks))
        await db.commit()
    return engine, Session


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        tocmemex_db_path=tmp_path / "tocmemex.db",
        tocmemex_data_dir=tmp_path / "data",
    )


async def _call(monkeypatch, tmp_path, name: str, args: dict):
    from app import mcp_server

    monkeypatch.setattr(mcp_server, "get_settings", lambda: _settings(tmp_path))
    return await mcp_server.server.call_tool(name, args)


@pytest.mark.asyncio
async def test_resource_tools_registered():
    from app.mcp_server import server

    names = {tool.name for tool in await server.list_tools()}
    assert {"list_resources", "read_resource"} <= names


@pytest.mark.asyncio
async def test_list_resources_tool_filters(tmp_path, monkeypatch):
    engine, _ = await _seed(tmp_path)
    result = await _call(
        monkeypatch, tmp_path, "list_resources", {"doc_id": "doc1", "type": "figure"}
    )
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert [r["resource_id"] for r in payload["resources"]] == ["fig_1_0"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_read_resource_table_returns_markdown(tmp_path, monkeypatch):
    engine, _ = await _seed(tmp_path)
    result = await _call(
        monkeypatch,
        tmp_path,
        "read_resource",
        {"doc_id": "doc1", "resource_id": "tbl_1_0"},
    )
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["type"] == "table"
    assert "| 外观 | a |" in payload["content"]
    assert payload["node_id"] is None
    assert len(result.content) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_read_resource_figure_returns_image_block(tmp_path, monkeypatch):
    engine, _ = await _seed(tmp_path)
    result = await _call(
        monkeypatch,
        tmp_path,
        "read_resource",
        {"doc_id": "doc1", "resource_id": "fig_1_0", "include_image": True},
    )
    assert result.is_error is False
    assert len(result.content) == 2
    assert result.content[0].type == "text"
    assert result.content[1].type == "image"
    assert result.content[1].mime_type == "image/jpeg"
    payload = json.loads(result.content[0].text)
    assert payload["image_meta"]["mime"] == "image/jpeg"
    assert payload["image_meta"]["bytes"] > 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_read_resource_missing_is_tool_error(tmp_path, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    engine, _ = await _seed(tmp_path)
    # In-process call_tool raises ToolError; the wire handler turns the same
    # error into an is_error result (see test_collections.py).
    with pytest.raises(ToolError, match="resource not found"):
        await _call(
            monkeypatch,
            tmp_path,
            "read_resource",
            {"doc_id": "doc1", "resource_id": "nope"},
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_read_resource_logs_read_activity(tmp_path, monkeypatch):
    engine, Session = await _seed(tmp_path)
    async with Session() as db:
        task = await task_create(db, "资源读取")
        await db.commit()
        task_id = task.id

    result = await _call(
        monkeypatch,
        tmp_path,
        "read_resource",
        {
            "doc_id": "doc1",
            "resource_id": "fig_1_0",
            "actor_agent": "agent-a",
            "task_id": task_id,
        },
    )
    assert result.is_error is False

    async with Session() as db:
        rows = (
            await db.execute(
                text(
                    "select doc_id, node_id, task_id from activities "
                    "where kind = 'read' and actor_agent = 'agent-a'"
                )
            )
        ).mappings().all()
    assert len(rows) == 1
    assert rows[0]["doc_id"] == "doc1"
    assert rows[0]["task_id"] == task_id
    await engine.dispose()


@pytest.mark.asyncio
async def test_search_attaches_resources(tmp_path, monkeypatch):
    engine, _ = await _seed(tmp_path)
    result = await _call(
        monkeypatch, tmp_path, "search", {"query": "范围", "top_k": 3}
    )
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    refs = payload["results"][0]["resources"]
    assert {r["resource_id"] for r in refs} == {"tbl_1_0", "fig_1_0"}
    await engine.dispose()


@pytest.mark.asyncio
async def test_read_section_attaches_resources(tmp_path, monkeypatch):
    engine, _ = await _seed(tmp_path)
    result = await _call(
        monkeypatch,
        tmp_path,
        "read_section",
        {"doc_id": "doc1", "node_id": "n1"},
    )
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert {r["resource_id"] for r in payload["resources"]} == {
        "tbl_1_0",
        "fig_1_0",
    }
    assert payload["chunks"]
    assert payload["chunks"][0]["chunk_id"] == payload["chunk_ids"][0]
    assert payload["chunks"][0]["heading_path"] == "1 范围"
    await engine.dispose()


@pytest.mark.asyncio
async def test_read_section_falls_back_for_hand_built_node_id(tmp_path, monkeypatch):
    """The MCP chain gets the same number fallback as the console agent."""
    engine, _ = await _seed(tmp_path)
    result = await _call(
        monkeypatch,
        tmp_path,
        "read_section",
        {"doc_id": "doc1", "node_id": "doc1:h:1.1.1"},
    )
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["node_id"] == "n2"
    assert payload["resolved_from"] == "doc1:h:1.1.1"
    assert "目的正文" in payload["content"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_ask_attaches_resources_to_citations(tmp_path, monkeypatch):
    engine, _ = await _seed(tmp_path)
    result = await _call(monkeypatch, tmp_path, "ask", {"question": "范围"})
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["citations"][0]["resources"]

    refs = [
        resource
        for group in (payload["citations"], payload["hits"])
        for anchor in group
        for resource in anchor.get("resources", [])
    ]
    ids = [resource["resource_id"] for resource in refs]
    assert len(ids) == len(set(ids))
    await engine.dispose()


async def _set_image_path(session, resource_id: str, value: str | None) -> None:
    resource = (
        await session.execute(
            select(Resource).where(
                Resource.doc_id == "doc1", Resource.resource_id == resource_id
            )
        )
    ).scalar_one()
    resource.image_path = value
    await session.commit()


@pytest.mark.asyncio
async def test_read_resource_missing_image_returns_metadata(tmp_path, monkeypatch):
    engine, Session = await _seed(tmp_path)
    async with Session() as db:
        await _set_image_path(db, "fig_1_0", None)

    result = await _call(
        monkeypatch,
        tmp_path,
        "read_resource",
        {"doc_id": "doc1", "resource_id": "fig_1_0", "include_image": True},
    )
    assert result.is_error is False
    assert len(result.content) == 1
    payload = json.loads(result.content[0].text)
    assert payload["image_available"] is False
    assert payload["image_include_error"] == "图片文件不可用或不在资源目录中"
    assert "image_path" not in payload
    await engine.dispose()


@pytest.mark.asyncio
async def test_read_resource_deleted_image_returns_metadata(tmp_path, monkeypatch):
    engine, _ = await _seed(tmp_path)
    (tmp_path / "data" / "resources" / "doc1" / "fig_1_0.jpg").unlink()

    result = await _call(
        monkeypatch,
        tmp_path,
        "read_resource",
        {"doc_id": "doc1", "resource_id": "fig_1_0", "include_image": True},
    )
    assert result.is_error is False
    assert len(result.content) == 1
    payload = json.loads(result.content[0].text)
    assert payload["image_include_error"] == "图片文件不可用或不在资源目录中"
    await engine.dispose()


@pytest.mark.asyncio
async def test_read_resource_image_outside_root_is_not_served(tmp_path, monkeypatch):
    engine, Session = await _seed(tmp_path)
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(b"jpeg")
    async with Session() as db:
        await _set_image_path(db, "fig_1_0", str(outside))

    result = await _call(
        monkeypatch,
        tmp_path,
        "read_resource",
        {"doc_id": "doc1", "resource_id": "fig_1_0", "include_image": True},
    )
    assert result.is_error is False
    assert len(result.content) == 1
    payload = json.loads(result.content[0].text)
    assert payload["image_include_error"] == "图片文件不可用或不在资源目录中"
    assert "secret.jpg" not in result.content[0].text
    await engine.dispose()


@pytest.mark.asyncio
async def test_read_section_caps_resource_refs_at_twelve(tmp_path, monkeypatch):
    engine, Session = await _seed(tmp_path)
    async with Session() as db:
        for index in range(11):
            db.add(
                Resource(
                    doc_id="doc1",
                    resource_id=f"tbl_1_extra_{index}",
                    type="table",
                    title=f"表1 补充{index}",
                    content="<table><tr><td>x</td></tr></table>",
                    page_idx=1,
                )
            )
        await db.commit()

    result = await _call(
        monkeypatch,
        tmp_path,
        "read_section",
        {"doc_id": "doc1", "node_id": "n1"},
    )
    payload = json.loads(result.content[0].text)
    assert len(payload["resources"]) == 12
    await engine.dispose()


@pytest.mark.asyncio
async def test_search_dedupes_resources_across_hits(tmp_path, monkeypatch):
    engine, _ = await _seed(tmp_path)
    result = await _call(
        monkeypatch, tmp_path, "search", {"query": "正文", "top_k": 5}
    )
    payload = json.loads(result.content[0].text)
    assert len(payload["results"]) >= 2
    ids = [
        resource["resource_id"]
        for hit in payload["results"]
        for resource in hit["resources"]
    ]
    assert len(ids) == len(set(ids))
    assert {"tbl_1_0", "fig_1_0"} <= set(ids)
    await engine.dispose()


@pytest.mark.asyncio
async def test_list_resources_rejects_unknown_doc_and_bad_type(tmp_path, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    engine, _ = await _seed(tmp_path)
    with pytest.raises(ToolError, match="document not found"):
        await _call(
            monkeypatch, tmp_path, "list_resources", {"doc_id": "nope"}
        )
    with pytest.raises(ToolError, match="invalid resource type"):
        await _call(
            monkeypatch,
            tmp_path,
            "list_resources",
            {"doc_id": "doc1", "type": "chart"},
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_search_and_read_section_carry_chunk_figures(tmp_path, monkeypatch):
    engine, _ = await _seed(tmp_path)
    search = await _call(
        monkeypatch, tmp_path, "search", {"query": "范围", "top_k": 3}
    )
    assert search.is_error is False
    payload = json.loads(search.content[0].text)
    assert payload["results"][0]["figures"][0]["resource_id"] == "fig_1_0"

    read = await _call(
        monkeypatch, tmp_path, "read_section", {"doc_id": "doc1", "node_id": "n2"}
    )
    assert read.is_error is False
    section = json.loads(read.content[0].text)
    assert section["chunks"][0]["figures"][0]["resource_id"] == "fig_1_0"
    await engine.dispose()


@pytest.mark.asyncio
async def test_read_resource_oversized_image_keeps_text_only(tmp_path, monkeypatch):
    engine, _ = await _seed(tmp_path)
    image_path = tmp_path / "data" / "resources" / "doc1" / "fig_1_0.jpg"
    image_path.write_bytes(b"x" * (4 * 1024 * 1024 + 1))

    result = await _call(
        monkeypatch,
        tmp_path,
        "read_resource",
        {"doc_id": "doc1", "resource_id": "fig_1_0", "include_image": True},
    )
    assert result.is_error is False
    assert len(result.content) == 1
    payload = json.loads(result.content[0].text)
    assert payload["image_include_error"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_mcp_overview_tool_returns_documents_with_headings(tmp_path, monkeypatch):
    engine, _ = await _seed(tmp_path)
    result = await _call(monkeypatch, tmp_path, "overview", {})
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["count"] == 1
    assert payload["documents"][0]["doc_id"] == "doc1"
    assert [h["node_id"] for h in payload["documents"][0]["headings"]] == ["n1"]
    await engine.dispose()


def test_mcp_instructions_split_keeps_retrieval_and_coordination():
    from app.agent.instructions import (
        COORDINATION_INSTRUCTIONS,
        FULL_INSTRUCTIONS,
        RETRIEVAL_INSTRUCTIONS,
    )
    from app.mcp_server import server

    assert "Search hits, ask citations and read_section results carry a 'resources' list" in RETRIEVAL_INSTRUCTIONS
    assert "task_create()" in COORDINATION_INSTRUCTIONS
    assert "task_create()" not in RETRIEVAL_INSTRUCTIONS
    assert server.instructions == FULL_INSTRUCTIONS
