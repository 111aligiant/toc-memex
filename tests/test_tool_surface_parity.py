"""The console tool surface must be the MCP surface minus identity arguments.

The test bench is an in-process stand-in for an external MCP agent: schemas,
descriptions, execution and result payloads all come from the MCP server, so
any drift between the two surfaces is a bug the model would feel.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_console_schema_is_mcp_surface_minus_identity():
    from app.agent.surface import CONSOLE_SHARED_TOOLS, HIDDEN_ARGS, tool_schemas
    from app.mcp_server import server

    # Hardcoded on purpose: the console schemas are built from
    # CONSOLE_SHARED_TOOLS, so deriving this set from that tuple would make
    # the assertion tautological. A retrieval tool silently dropped from the
    # console must turn this test red.
    EXPECTED_CONSOLE_SURFACE = {
        "memex_overview",
        "list_collections",
        "list_documents",
        "get_document",
        "get_toc",
        "read_section",
        "list_resources",
        "read_resource",
        "search",
        "ask",
        "overview",
        "list_notes",
        "add_note",
        "mark_used",
    }

    mcp = {tool.name: tool for tool in await server.list_tools()}
    console = {s["function"]["name"]: s["function"] for s in await tool_schemas(True)}

    assert set(CONSOLE_SHARED_TOOLS) == EXPECTED_CONSOLE_SURFACE
    assert set(console) == EXPECTED_CONSOLE_SURFACE | {"final_answer"}

    for name in CONSOLE_SHARED_TOOLS:
        hidden = HIDDEN_ARGS.get(name, frozenset())
        expected = {k: v for k, v in mcp[name].input_schema["properties"].items()
                    if k not in hidden}
        required = [k for k in mcp[name].input_schema.get("required", [])
                    if k not in hidden]
        assert console[name]["description"] == mcp[name].description
        assert set(console[name]["parameters"]["properties"]) == set(expected)
        assert set(console[name]["parameters"]["required"]) == set(required)

    note_props = console["add_note"]["parameters"]["properties"]
    assert "reply_to" in note_props
    assert "author" not in note_props and "task_id" not in note_props


@pytest.mark.asyncio
async def test_console_schema_notes_gate():
    from app.agent.surface import tool_schemas
    on = {s["function"]["name"] for s in await tool_schemas(True)}
    off = {s["function"]["name"] for s in await tool_schemas(False)}
    assert "add_note" in on and "add_note" not in off
    assert "list_notes" in on and "list_notes" in off


def test_normalize_tool_result_unwraps_read_resource_only():
    from app.agent.surface import normalize_tool_result

    assert normalize_tool_result("read_resource", [{"type": "table"}, object()]) == {
        "type": "table"
    }
    assert normalize_tool_result("search", [1, 2]) == [1, 2]


def test_parse_call_tool_result_keeps_list_shape():
    """List tools return structured ``{"result": [...]}``; text fallback
    flattens one block per item, so both paths must rebuild the list."""
    from app.agent.surface import parse_call_tool_result

    structured = SimpleNamespace(
        structured_content={"result": [{"doc_id": "a"}, {"doc_id": "b"}]},
        content=[],
        is_error=False,
    )
    assert parse_call_tool_result(structured).payload == [
        {"doc_id": "a"},
        {"doc_id": "b"},
    ]

    flattened = SimpleNamespace(
        structured_content=None,
        is_error=False,
        content=[
            SimpleNamespace(type="text", text='{"doc_id": "a"}'),
            SimpleNamespace(type="text", text='{"doc_id": "b"}'),
        ],
    )
    assert parse_call_tool_result(flattened).payload == [
        {"doc_id": "a"},
        {"doc_id": "b"},
    ]


@pytest.mark.asyncio
async def test_call_mcp_uses_server_call_tool_and_parses_payloads(tmp_path, monkeypatch):
    from app import mcp_server
    from app.agent.surface import call_mcp
    from app.db import session_factory
    from test_mcp_resources import _seed

    engine, _ = await _seed(tmp_path)
    calls = []
    original = mcp_server.server.call_tool

    async def spy(name, arguments, context=None):
        calls.append(name)
        return await original(name, arguments, context)

    monkeypatch.setattr(mcp_server.server, "call_tool", spy)
    async with session_factory(engine)() as db:
        table = await call_mcp(db, "run", "read_resource",
            {"doc_id": "doc1", "resource_id": "tbl_1_0"},
            resources_root=tmp_path / "data" / "resources")
        figure = await call_mcp(db, "run", "read_resource",
            {"doc_id": "doc1", "resource_id": "fig_1_0", "include_image": True},
            resources_root=tmp_path / "data" / "resources")
        missing = await call_mcp(db, "run", "get_toc", {"doc_id": "missing"})
    await engine.dispose()

    assert calls == ["read_resource", "read_resource", "get_toc"]
    assert table.payload["type"] == "table"
    assert "外观" in table.payload["content"]
    assert figure.payload["image_meta"]["mime"] == "image/jpeg"
    assert figure.images[0]["data_url"].startswith("data:image/jpeg;base64,")
    assert missing.is_error is True
    assert "error" in missing.payload
