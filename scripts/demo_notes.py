"""Seed the isolated AI-paper demo library with real MCP notes.

Adds a pitfall + a reading note on BERT and a handoff on DDPM (pages are
looked up from the demo DB at runtime, so every claim in a note body is
verifiable against the library). Run while the MCP server on :8792 is up
with TOCMEMEX_DB_PATH pointed at the demo database.

    python scripts/demo_notes.py
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "web/shots/ai-demo/data/tocmemex.db"


def doc_id_of(title_like: str) -> str:
    with sqlite3.connect(DB) as conn:
        row = conn.execute(
            "select id from documents where title like ? limit 1", (title_like,)
        ).fetchone()
    if not row:
        raise SystemExit(f"document not found: {title_like}")
    return row[0]


def pages_of(doc_id: str, heading_num: str) -> int:
    with sqlite3.connect(DB) as conn:
        row = conn.execute(
            "select page_start from toc_nodes where doc_id=? and heading_num=? limit 1",
            (doc_id, heading_num),
        ).fetchone()
    if not row:
        raise SystemExit(f"node {heading_num} not found in {doc_id}")
    return row[0] + 1  # stored 0-based; reports are 1-based


def payload(result: Any) -> dict:
    if getattr(result, "structuredContent", None):
        return result.structuredContent
    for item in getattr(result, "content", []):
        if getattr(item, "type", None) == "text":
            return json.loads(item.text)
    raise RuntimeError(f"MCP result has no structured payload: {result!r}")


async def main() -> None:
    bert = doc_id_of("BERT%")
    ddpm = doc_id_of("Denoising%")
    p32 = pages_of(ddpm, "3.2")
    p34 = pages_of(ddpm, "3.4")
    notes = [
        {
            "kind": "pitfall",
            "author": "dsh-mcp-client",
            "doc_id": bert,
            "body": (
                "踩坑：BERT 的 §4.2 SQuAD v1.1 与 §4.3 SQuAD v2.0 相邻（PDF 第 7–8 页），"
                "检索 SQuAD 会同时命中两节，引用时按 heading_path 区分版本。"
            ),
        },
        {
            "kind": "note",
            "author": "WorkBuddy",
            "doc_id": bert,
            "body": (
                "§3.1 Pre-training BERT 在 PDF 第 5 页，MLM 与 NSP 两个预训练任务的"
                "正式定义就在这一节。"
            ),
        },
        {
            "kind": "handoff",
            "author": "WorkBuddy",
            "doc_id": ddpm,
            "body": (
                f"DDPM 已入库（25 页，目录来自书签）。§3.2 Reverse process 在 PDF 第 {p32} 页，"
                f"§3.4 Simplified training objective 在第 {p34} 页；引用训练目标时直接从这两节进入，"
                "不要整篇搜索 diffusion。"
            ),
        },
    ]
    async with streamable_http_client("http://127.0.0.1:8792/mcp") as (read, write):
        async with ClientSession(
            read, write, client_info=Implementation(name="demo-seeder", version="0")
        ) as session:
            await session.initialize()
            for note in notes:
                result = await session.call_tool(
                    "add_note",
                    {
                        "body": note["body"],
                        "kind": note["kind"],
                        "author": note["author"],
                        "doc_id": note["doc_id"],
                    },
                )
                if getattr(result, "isError", False):
                    raise RuntimeError(f"add_note failed: {result!r}")
                print(note["kind"], note["author"], payload(result))


if __name__ == "__main__":
    asyncio.run(main())
