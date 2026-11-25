"""Run two real external MCP sessions against the isolated AI-paper demo.

Prerequisites: the public AI-paper PDFs have been ingested into the demo DB and
``app.mcp_server --transport streamable-http --port 8792`` is running with
TOCMEMEX_DB_PATH/TOCMEMEX_DATA_DIR pointed at that isolated DB.

This script never reads or writes the developer's primary library.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation

ROOT = Path(__file__).resolve().parent.parent
ATTENTION_DOC = "1f3b0db4-f17d-49da-99de-a4cd9f612f8c"
SCALED_NODE = f"{ATTENTION_DOC}:t:42cb9615"
MULTIHEAD_NODE = f"{ATTENTION_DOC}:t:cd387d7e"


def payload(result: Any) -> dict:
    if getattr(result, "structuredContent", None):
        return result.structuredContent
    content = getattr(result, "content", [])
    for item in content:
        if getattr(item, "type", None) == "text":
            return json.loads(item.text)
    raise RuntimeError(f"MCP result has no structured payload: {result!r}")


async def call(session: ClientSession, name: str, **arguments: Any) -> dict:
    result = await session.call_tool(name, arguments)
    if getattr(result, "isError", False):
        raise RuntimeError(f"{name} failed: {result!r}")
    data = payload(result)
    print(name, json.dumps(data, ensure_ascii=False)[:220])
    return data


async def workbuddy(base: str) -> dict:
    async with streamable_http_client(base) as (read, write):
        async with ClientSession(
            read, write, client_info=Implementation(name="WorkBuddy", version="demo")
        ) as session:
            await session.initialize()
            started = await call(
                session,
                "run_start",
                question="Transformer 注意力机制：章节定位与原文页码核对",
                actor_agent="WorkBuddy",
                max_calls=16,
            )
            task_id = started["task_id"]
            await call(session, "memex_overview")
            found = await call(
                session,
                "search",
                query="scaled dot-product attention",
                top_k=4,
                doc_ids=[ATTENTION_DOC],
                actor_agent="WorkBuddy",
                task_id=task_id,
            )
            await call(
                session,
                "read_section",
                doc_id=ATTENTION_DOC,
                node_id=SCALED_NODE,
                max_chars=3600,
                actor_agent="WorkBuddy",
                task_id=task_id,
            )
            await call(
                session,
                "search",
                query="multi-head attention",
                top_k=3,
                doc_ids=[ATTENTION_DOC],
                actor_agent="WorkBuddy",
                task_id=task_id,
            )
            await call(
                session,
                "read_section",
                doc_id=ATTENTION_DOC,
                node_id=MULTIHEAD_NODE,
                max_chars=3000,
                actor_agent="WorkBuddy",
                task_id=task_id,
            )
            first = found["results"][0]
            await call(
                session,
                "mark_used",
                chunk_id=first["chunk_id"],
                actor_agent="WorkBuddy",
                task_id=task_id,
            )
            await call(
                session,
                "add_note",
                body=(
                    "已核对 Transformer 的两类注意力结构：Scaled Dot-Product Attention "
                    "在 PDF 第 4 页（印刷页 3），Multi-Head Attention 同页；下次会话可从这两个节点继续。"
                ),
                kind="handoff",
                author="WorkBuddy",
                doc_id=ATTENTION_DOC,
                node_id=SCALED_NODE,
                task_id=task_id,
            )
            await call(
                session,
                "run_end",
                answer=(
                    "Scaled Dot-Product Attention 与 Multi-Head Attention 均位于 "
                    "Attention Is All You Need 的 PDF 第 4 页（印刷页 3）。"
                ),
            )
            return {"run_id": started["run_id"], "task_id": task_id}


async def dsh(base: str) -> dict:
    async with streamable_http_client(base) as (read, write):
        async with ClientSession(
            read, write, client_info=Implementation(name="dsh-mcp-client", version="demo")
        ) as session:
            await session.initialize()
            started = await call(
                session,
                "run_start",
                question="DSH 跨会话复核：Transformer 位置编码",
                actor_agent="dsh-mcp-client",
                max_calls=8,
            )
            task_id = started["task_id"]
            found = await call(
                session,
                "search",
                query="positional encoding",
                top_k=3,
                doc_ids=[ATTENTION_DOC],
                actor_agent="dsh-mcp-client",
                task_id=task_id,
            )
            node_id = found["results"][0]["node_id"]
            await call(
                session,
                "read_section",
                doc_id=ATTENTION_DOC,
                node_id=node_id,
                max_chars=2600,
                actor_agent="dsh-mcp-client",
                task_id=task_id,
            )
            await call(
                session,
                "add_note",
                body="DSH 已读取 Positional Encoding 节点，页码与章节锚点已保留。",
                kind="note",
                author="dsh-mcp-client",
                doc_id=ATTENTION_DOC,
                node_id=node_id,
                task_id=task_id,
            )
            await call(session, "run_end", answer="Positional Encoding 的章节与页码已核对。")
            return {"run_id": started["run_id"], "task_id": task_id}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8792/mcp")
    parser.add_argument("--out", default="web/shots/ai-demo/sessions.json")
    args = parser.parse_args()
    result = {"workbuddy": await workbuddy(args.url), "dsh": await dsh(args.url)}
    out = (ROOT / args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
