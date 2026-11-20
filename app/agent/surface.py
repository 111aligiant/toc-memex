"""The test bench's window onto the MCP tool surface.

The built-in console is an in-process stand-in for an external MCP agent, so
it must not have its own implementation of shared tools. This module builds
the console's tool schemas from ``server.list_tools()``, hides the identity
arguments the adapter fills in, and executes every shared call through
``server.call_tool()`` inside :func:`app.mcp_server.bridge`. Only
``final_answer`` and the run lifecycle remain console-specific.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

AGENT_NAME = "tocmemex-agent"

# Ordered as the model should see them; the MCP server is the source of the
# schema, description and defaults for each.
CONSOLE_SHARED_TOOLS: tuple[str, ...] = (
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
)

# Filled by the adapter (who is calling / which run), never by the model.
IDENTITY_ARGS: dict[str, dict[str, str]] = {
    "search": {"actor_agent": AGENT_NAME, "task_id": ""},
    "read_section": {"actor_agent": AGENT_NAME, "task_id": ""},
    "read_resource": {"actor_agent": AGENT_NAME, "task_id": ""},
    "ask": {"actor_agent": AGENT_NAME, "task_id": ""},
    "mark_used": {"actor_agent": AGENT_NAME, "task_id": ""},
    "add_note": {"author": AGENT_NAME, "task_id": ""},
    "list_notes": {"task_id": ""},
}
HIDDEN_ARGS: dict[str, frozenset[str]] = {
    name: frozenset(values) for name, values in IDENTITY_ARGS.items()
}

FINAL_ANSWER = {
    "type": "function",
    "function": {
        "name": "final_answer",
        "description": (
            "提交最终答案。答案只能基于本轮 read_section / read_resource 读到的内容。"
            "sources 可选：把实际用到的切片 chunk_id 填进去（表格填 resource_id，"
            "必要时补 doc_id），代码会把它们留痕并在前端展示，用于回溯；"
            "不需要复制原文，也不做逐字校验。"
            "正文可以用 [1][2] 指向 sources 的顺序，方便跳转；不写也可以。"
            "证据不足时把 insufficient 设为 true，并在 answer 里说明。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "answer": {"type": "string"},
                "sources": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "chunk_id": {"type": "integer"},
                            "resource_id": {"type": "string"},
                            "doc_id": {
                                "type": "string",
                                "description": "可选：同名资源出现在多份文档时用来指明来源",
                            },
                        },
                        "additionalProperties": False,
                    },
                },
                "insufficient": {"type": "boolean", "default": False},
            },
            "required": ["answer"],
            "additionalProperties": False,
        },
    },
}


@dataclass(frozen=True)
class ToolOutcome:
    """One MCP result, parsed into what the console loop consumes."""

    payload: Any
    # {"mime", "data_url"} — memory only; never written to steps or logs.
    images: tuple[dict[str, str], ...] = ()
    is_error: bool = False


def _mcp():
    """Lazy import: mcp_server imports app.agent.external, never this module."""
    from .. import mcp_server

    return mcp_server


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


async def tool_schemas(allow_notes: bool) -> list[dict]:
    """MCP schemas minus identity args, plus the console-only final_answer."""
    mcp = _mcp()
    tools = {tool.name: tool for tool in await mcp.server.list_tools()}
    schemas: list[dict] = []
    for name in CONSOLE_SHARED_TOOLS:
        if name == "add_note" and not allow_notes:
            continue
        tool = tools[name]
        hidden = HIDDEN_ARGS.get(name, frozenset())
        properties = {
            key: value
            for key, value in tool.input_schema.get("properties", {}).items()
            if key not in hidden
        }
        required = [
            key
            for key in tool.input_schema.get("required", [])
            if key not in hidden
        ]
        schemas.append(_tool(name, tool.description, properties, required))
    schemas.append(FINAL_ANSWER)
    return schemas


def _decode(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def parse_call_tool_result(result: Any) -> ToolOutcome:
    """Turn a ``CallToolResult`` into a payload plus in-memory images.

    List-returning tools carry ``structured_content = {"result": [...]}``;
    when only text blocks are available the SDK flattened the list into one
    block per item, so all of them are decoded and rebuilt into a list.
    """
    blocks = list(getattr(result, "content", []) or [])
    images = tuple(
        {
            "mime": block.mime_type,
            "data_url": f"data:{block.mime_type};base64,{block.data}",
        }
        for block in blocks
        if getattr(block, "type", None) == "image"
    )
    text_blocks = [
        block for block in blocks if getattr(block, "type", None) == "text"
    ]
    if getattr(result, "is_error", False):
        text = text_blocks[0].text if text_blocks else ""
        return ToolOutcome(
            payload={"error": text or "工具调用失败"},
            images=images,
            is_error=True,
        )
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        if isinstance(structured, dict) and set(structured) == {"result"}:
            structured = structured["result"]
        return ToolOutcome(payload=structured, images=images)
    if not text_blocks:
        return ToolOutcome(payload=None, images=images)
    values = [_decode(block.text) for block in text_blocks]
    payload = values[0] if len(values) == 1 else values
    return ToolOutcome(payload=payload, images=images)


def normalize_tool_result(tool: str, result: Any) -> Any:
    """Stored payload shape shared by the console and external runs."""
    if tool == "read_resource" and isinstance(result, list):
        return next(
            (item for item in result if isinstance(item, dict)), result
        )
    return result


async def call_mcp(
    db: AsyncSession,
    run_id: str,
    name: str,
    args: dict,
    *,
    resources_root: Path | None = None,
) -> ToolOutcome:
    """Execute one shared tool exactly as an external MCP client would."""
    mcp = _mcp()
    try:
        with mcp.bridge(db, run_id, resources_root):
            result = await mcp.server.call_tool(name, args)
    except (mcp.ToolError, ValueError) as exc:
        return ToolOutcome(payload={"error": str(exc)}, is_error=True)
    return parse_call_tool_result(result)
