from __future__ import annotations

import json

import pytest
from agent_helpers import call, execute_with_fake_llm, reply
from sqlalchemy import select

from app.db import init_db, make_engine, session_factory
from app.index.fts import index_document
from app.models import Chunk, Document, Namespace, Resource, TocNode

TABLE_HTML = (
    "<table>"
    "<tr><td>寿命阶段</td><td>平台</td><td>类别</td><td>装备描述</td>"
    "<td>试验量值和持续时间</td></tr>"
    "<tr><td>运输</td><td>轮式</td><td>1</td><td>设备</td><td>按表2</td></tr>"
    "</table>"
)


async def _seed(tmp_path):
    db_path = tmp_path / "resources.db"
    image_dir = tmp_path / "data" / "resources" / "doc1"
    image_dir.mkdir(parents=True)
    image_path = image_dir / "fig_6_0.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    engine = make_engine(db_path)
    await init_db(engine)
    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(
            Document(
                id="doc1",
                namespace_id="shared",
                title="振动环境试验",
                filename="vibration.pdf",
                file_path="vibration.pdf",
                content_hash="resources",
                page_count=10,
                status="ready",
            )
        )
        await db.flush()
        db.add(
            TocNode(
                doc_id="doc1",
                node_id="n1",
                parent_id=None,
                level=1,
                heading_num="5",
                title="5 试验条件",
                heading_path="5 试验条件",
                page_start=5,
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
                    node_id="n1",
                    chunk_index=0,
                    content="5.1 试验范围 本章规定振动试验的适用范围。",
                    heading_path="5 试验条件 > 5.1 试验范围",
                    page_start=5,
                    page_end=5,
                    token_count=12,
                ),
                Chunk(
                    doc_id="doc1",
                    node_id="n1",
                    chunk_index=1,
                    content="5.2 试验条件 振动试验条件按表1选择，测点布置见图6。",
                    heading_path="5 试验条件 > 5.2 试验条件",
                    page_start=6,
                    page_end=6,
                    token_count=14,
                ),
            ]
        )
        db.add_all(
            [
                Resource(
                    doc_id="doc1",
                    resource_id="tbl_5_0",
                    type="table",
                    title="表1 振动环境类别",
                    content=TABLE_HTML,
                    page_idx=5,
                ),
                Resource(
                    doc_id="doc1",
                    resource_id="fig_6_0",
                    type="figure",
                    title="图6 夹具示意",
                    image_path=str(image_path),
                    page_idx=6,
                ),
            ]
        )
        await db.commit()

    async with session_factory(engine)() as db:
        doc = await db.get(Document, "doc1")
        chunks = (
            (await db.execute(select(Chunk).where(Chunk.doc_id == "doc1")))
            .scalars()
            .all()
        )
        await index_document(db, doc, list(chunks))
        await db.commit()
    await engine.dispose()
    return db_path


def _tool_result(run, name: str, occurrence: int = 0) -> dict:
    results = [
        json.loads(step.result_json)
        for step in run.steps
        if step.name == name and step.type == "tool_result"
    ]
    return results[occurrence]


async def test_list_and_read_table_resource(tmp_path, monkeypatch):
    db_path = await _seed(tmp_path)
    replies = [
        reply(
            call(
                "list_resources",
                {"doc_id": "doc1", "query": "表1"},
                call_id="call_1",
            )
        ),
        reply(
            call(
                "read_resource",
                {"doc_id": "doc1", "resource_id": "tbl_5_0"},
                call_id="call_2",
            )
        ),
        reply(
            call(
                "final_answer",
                {
                    "answer": "档案中没有足够证据。",
                    "sources": [],
                    "insufficient": True,
                },
                call_id="call_3",
            )
        ),
    ]
    run = await execute_with_fake_llm(db_path, monkeypatch, replies)
    listed = _tool_result(run, "list_resources")
    assert "表1 振动环境类别" in listed["resources"][0]["title"]
    read = _tool_result(run, "read_resource")
    assert "寿命阶段" in read["content"]


async def test_read_resource_image_on_table_reports_meta_and_error(
    tmp_path, monkeypatch
):
    db_path = await _seed(tmp_path)
    replies = [
        reply(
            call(
                "read_resource",
                {"doc_id": "doc1", "resource_id": "tbl_5_0", "include_image": True},
                call_id="call_1",
            )
        ),
        reply(
            call(
                "final_answer",
                {
                    "answer": "档案中没有足够证据。",
                    "sources": [],
                    "insufficient": True,
                },
                call_id="call_2",
            )
        ),
    ]
    run = await execute_with_fake_llm(db_path, monkeypatch, replies)
    read = _tool_result(run, "read_resource")
    assert read["image_meta"]["url"].endswith("/tbl_5_0/image")
    assert "figure" in read["image_include_error"]
    assert "_image" not in read


async def test_search_hit_carries_figure_handles(tmp_path, monkeypatch):
    db_path = await _seed(tmp_path)
    replies = [
        reply(
            call(
                "search",
                {"query": "试验条件", "top_k": 3},
                call_id="call_1",
            )
        ),
        reply(
            call(
                "final_answer",
                {
                    "answer": "档案中没有足够证据。",
                    "sources": [],
                    "insufficient": True,
                },
                call_id="call_2",
            )
        ),
    ]
    run = await execute_with_fake_llm(db_path, monkeypatch, replies)
    hits = _tool_result(run, "search")["results"]
    hit = hits[0]
    assert hit["figures"][0]["resource_id"] == "fig_6_0"
    assert hit["figures"][0]["has_image"] is True


async def test_read_section_chunks_carry_their_own_figures(tmp_path, monkeypatch):
    db_path = await _seed(tmp_path)
    replies = [
        reply(
            call(
                "read_section",
                {"doc_id": "doc1", "node_id": "n1"},
                call_id="call_1",
            )
        ),
        reply(
            call(
                "final_answer",
                {
                    "answer": "档案中没有足够证据。",
                    "sources": [],
                    "insufficient": True,
                },
                call_id="call_2",
            )
        ),
    ]
    run = await execute_with_fake_llm(db_path, monkeypatch, replies)
    result = _tool_result(run, "read_section")
    assert result["chunks"][0]["figures"] == []
    assert [f["resource_id"] for f in result["chunks"][1]["figures"]] == [
        "fig_6_0"
    ]


async def test_final_answer_accepts_table_resource_citation(tmp_path, monkeypatch):
    db_path = await _seed(tmp_path)
    replies = [
        reply(
            call(
                "read_resource",
                {"doc_id": "doc1", "resource_id": "tbl_5_0"},
                call_id="call_1",
            )
        ),
        reply(
            call(
                "final_answer",
                {
                    "answer": "按表1按寿命阶段选择类别 [1]。",
                    "sources": [
                        {
                            "ref": 1,
                            "resource_id": "tbl_5_0",
                            "quote": "寿命阶段 | 平台 | 类别 | 装备描述 | 试验量值和持续时间",
                        }
                    ],
                },
                call_id="call_2",
            )
        ),
    ]
    run = await execute_with_fake_llm(db_path, monkeypatch, replies)
    assert run.status == "done", run.error
    citation = json.loads(run.citations_json)[0]
    assert citation["resource_id"] == "tbl_5_0"
    assert citation["resource_title"] == "表1 振动环境类别"


async def test_figure_resource_is_not_recorded_as_citation(tmp_path, monkeypatch):
    """Figures are handles, not evidence: citing one leaves no citation."""
    db_path = await _seed(tmp_path)
    replies = [
        reply(
            call(
                "read_resource",
                {"doc_id": "doc1", "resource_id": "fig_6_0"},
                call_id="call_1",
            )
        ),
        reply(
            call(
                "final_answer",
                {
                    "answer": "见图6 [1]。",
                    "sources": [{"resource_id": "fig_6_0"}],
                },
                call_id="call_2",
            )
        ),
    ]
    run = await execute_with_fake_llm(db_path, monkeypatch, replies)
    assert run.status == "done", run.error
    assert _tool_result(run, "final_answer")["accepted"] is True
    assert json.loads(run.citations_json) == []

async def _seed_shared_resource_id(tmp_path):
    db_path = tmp_path / "shared-resource.db"
    engine = make_engine(db_path)
    await init_db(engine)
    table_a = (
        "<table>"
        "<tr><td>寿命阶段</td><td>平台</td><td>类别</td><td>装备描述</td><td>试验量值</td></tr>"
        "<tr><td>甲行</td><td>甲平台</td><td>甲类别</td><td>甲装备</td><td>甲量值</td></tr>"
        "</table>"
    )
    table_b = (
        "<table>"
        "<tr><td>寿命阶段</td><td>平台</td><td>类别</td><td>装备描述</td><td>试验量值</td></tr>"
        "<tr><td>乙行</td><td>乙平台</td><td>乙类别</td><td>乙装备</td><td>乙量值</td></tr>"
        "</table>"
    )
    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        for doc_id, title in (("docA", "甲标准"), ("docB", "乙标准")):
            db.add(
                Document(
                    id=doc_id,
                    namespace_id="shared",
                    title=title,
                    filename=f"{doc_id}.pdf",
                    file_path=f"{doc_id}.pdf",
                    content_hash=doc_id,
                    page_count=10,
                    status="ready",
                )
            )
        await db.flush()
        db.add_all(
            [
                Resource(
                    doc_id="docA",
                    resource_id="tbl_5_0",
                    type="table",
                    title="表5 甲条件",
                    content=table_a,
                    page_idx=5,
                ),
                Resource(
                    doc_id="docB",
                    resource_id="tbl_5_0",
                    type="table",
                    title="表5 乙条件",
                    content=table_b,
                    page_idx=5,
                ),
            ]
        )
        await db.commit()
    await engine.dispose()
    return db_path


async def test_shared_resource_id_without_doc_id_records_nothing(
    tmp_path, monkeypatch
):
    """An ambiguous table is skipped instead of silently bound to one doc."""
    db_path = await _seed_shared_resource_id(tmp_path)
    replies = [
        reply(
            call(
                "read_resource",
                {"doc_id": "docA", "resource_id": "tbl_5_0"},
                call_id="call_1",
            )
        ),
        reply(
            call(
                "read_resource",
                {"doc_id": "docB", "resource_id": "tbl_5_0"},
                call_id="call_2",
            )
        ),
        reply(
            call(
                "final_answer",
                {
                    "answer": "表头相同的引用 [1]。",
                    "sources": [{"resource_id": "tbl_5_0"}],
                },
                call_id="call_3",
            )
        ),
    ]
    run = await execute_with_fake_llm(db_path, monkeypatch, replies)
    assert run.status == "done", run.error
    assert _tool_result(run, "final_answer")["accepted"] is True
    assert json.loads(run.citations_json) == []


async def test_shared_resource_id_with_doc_id_binds_that_document(
    tmp_path, monkeypatch
):
    db_path = await _seed_shared_resource_id(tmp_path)
    replies = [
        reply(
            call(
                "read_resource",
                {"doc_id": "docA", "resource_id": "tbl_5_0"},
                call_id="call_1",
            )
        ),
        reply(
            call(
                "read_resource",
                {"doc_id": "docB", "resource_id": "tbl_5_0"},
                call_id="call_2",
            )
        ),
        reply(
            call(
                "final_answer",
                {
                    "answer": "甲表的数据 [1]。",
                    "sources": [{"doc_id": "docA", "resource_id": "tbl_5_0"}],
                },
                call_id="call_3",
            )
        ),
    ]
    run = await execute_with_fake_llm(db_path, monkeypatch, replies)
    assert run.status == "done", run.error
    citation = json.loads(run.citations_json)[0]
    assert citation["doc_id"] == "docA"
    assert citation["resource_title"] == "表5 甲条件"


@pytest.mark.asyncio
async def test_console_full_external_scenario_chain(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from agent_helpers import execute_with_fake_llm

    db_path = await _seed(tmp_path)
    settings = SimpleNamespace(resources_dir=tmp_path / "data" / "resources")
    replies = [
        reply(call("list_collections", {}, call_id="c1")),
        reply(call("overview", {"query": "振动"}, call_id="c2")),
        reply(call("search", {"query": "振动试验条件"}, call_id="c3")),
        reply(call("get_document", {"doc_id": "doc1"}, call_id="c4")),
        reply(call("get_toc", {"doc_id": "doc1"}, call_id="c5")),
        reply(call("read_section", {"doc_id": "doc1", "node_id": "n1"}, call_id="c6")),
        reply(call("list_resources", {"doc_id": "doc1"}, call_id="c7")),
        reply(call("read_resource", {"doc_id": "doc1", "resource_id": "tbl_5_0"}, call_id="c8")),
        reply(call("read_resource", {"doc_id": "doc1", "resource_id": "fig_6_0",
                                     "include_image": True}, call_id="c9")),
        reply(call("mark_used", {"chunk_id": 1}, call_id="c10")),
        reply(call("add_note", {"body": "表1 在 5.2 节", "kind": "pitfall",
                                "doc_id": "doc1"}, call_id="c11")),
        reply(call("final_answer", {
            "answer": "振动条件按表1 [1][2]。",
            "sources": [{"chunk_id": 2}, {"resource_id": "tbl_5_0"}],
        }, call_id="c12")),
    ]
    run = await execute_with_fake_llm(
        db_path, monkeypatch, replies, max_calls=14, settings=settings
    )

    assert run.status == "done"
    tool_names = [s.name for s in run.steps if s.type == "tool_result"]
    assert tool_names == [
        "list_collections", "overview", "search", "get_document", "get_toc",
        "read_section", "list_resources", "read_resource", "read_resource",
        "mark_used", "final_answer",
    ]
    assert any(s.type == "note" and s.name == "add_note" for s in run.steps)

    read = _tool_result(run, "read_section")
    assert "content" in read and read["chunks"]
    assert [f["resource_id"] for f in read["chunks"][1]["figures"]] == ["fig_6_0"]

    table = _tool_result(run, "read_resource", 0)
    assert table["type"] == "table"
    assert "寿命阶段" in table["content"]

    figure = _tool_result(run, "read_resource", 1)
    assert figure["type"] == "figure"
    assert figure["image_meta"]["mime"] == "image/png"
    figure_step = [s for s in run.steps
                   if s.name == "read_resource" and s.type == "tool_result"][1]
    assert "base64" not in figure_step.result_json

    citations = json.loads(run.citations_json)
    assert any(c.get("chunk_id") == 2 for c in citations)
    assert any(c.get("resource_id") == "tbl_5_0" for c in citations)


@pytest.mark.asyncio
async def test_console_payload_matches_mcp_call_tool(tmp_path):
    from app import mcp_server
    from app.agent.surface import IDENTITY_ARGS, parse_call_tool_result
    from app.agent.tools import ToolContext, dispatch
    from app.db import make_engine, session_factory

    db_path = await _seed(tmp_path)
    engine = make_engine(db_path)
    cases = [
        ("search", {"query": "振动"}),
        ("get_toc", {"doc_id": "doc1", "depth": 2}),
        ("read_section", {"doc_id": "doc1", "node_id": "n1"}),
        ("list_resources", {"doc_id": "doc1"}),
        ("read_resource", {"doc_id": "doc1", "resource_id": "tbl_5_0"}),
        ("overview", {"query": "振动"}),
        ("get_document", {"doc_id": "doc1"}),
        ("list_collections", {}),
        ("memex_overview", {}),
    ]
    async with session_factory(engine)() as db:
        ctx = ToolContext(db=db, run_id="parity", allow_notes=False,
                          resources_root=tmp_path / "data" / "resources")
        for name, args in cases:
            console = await dispatch(ctx, name, args)
            with mcp_server.bridge(db, "parity", tmp_path / "data" / "resources"):
                raw = await mcp_server.server.call_tool(
                    name, {**args, **IDENTITY_ARGS.get(name, {})}
                )
            external = parse_call_tool_result(raw).payload
            assert console == external, name
    await engine.dispose()
