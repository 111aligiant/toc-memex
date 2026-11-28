"""Read-only checks behind docs/mcp-agent-feedback-2026-11-28.md.

Run from the repository root (uses the same .env / DB as the server):

    python scripts/mcp_probe_2026-11-28.py
    python scripts/mcp_probe_2026-11-28.py --write-probe   # dangling anchor case
    python scripts/mcp_probe_2026-11-28.py --ingest FILE   # MCP ingest round trip

Default is read-only: it calls lookup/search tools, plus ``add_note`` cases
that are *expected* to be rejected (kind="ingest", dangling chunk_id).
``--write-probe`` is kept as an alias for the anchor probe.
``--ingest`` submits a file through the MCP ingest tools and polls it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app.mcp_server as m  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

IMG_DOC = "59254d3a-250b-4d12-b22c-59ab3020c381"  # 颜术, image / OCR
MD_DOC = "4e908db2-46d4-417e-a26b-3d7653962742"   # 有赞, markdown


def show(tag: str, ok: bool, payload: object) -> None:
    print(f"\n### {tag} :: {'OK' if ok else 'ERR'}")
    print(json.dumps(payload, ensure_ascii=False, default=str)[:900])


async def probe(tag: str, coro) -> object | None:
    try:
        out = await coro
        show(tag, True, out)
        return out
    except ToolError as exc:
        show(tag, False, {"ToolError": str(exc)})
    except Exception as exc:  # noqa: BLE001
        show(tag, False, {"Exc": f"{type(exc).__name__}: {exc}"})
    return None


async def main(anchor_probe: bool, ingest: str | None) -> None:
    registry = [(name, profile) for name, _fn, profile in m._REGISTRY]
    default = sorted(name for name, profile in registry if profile == "default")
    print("default tools", len(default), default)
    print("full tools", len(registry))

    await probe("add_note kind=ingest (expected reject)",
                m.add_note(body="probe", kind="ingest", author="probe"))
    await probe("add_note unknown doc",
                m.add_note(body="probe", kind="note", author="probe",
                           doc_id="00000000-0000-0000-0000-000000000000"))

    if anchor_probe:
        # A dangling anchor must be refused now; previously it wrote a note
        # that no reader could ever find.
        await probe("add_note dangling chunk_id (expected reject)",
                    m.add_note(body="probe", kind="note", author="probe",
                               doc_id=IMG_DOC, chunk_id=999999))

    toc_img = await probe("get_toc image", m.get_toc(doc_id=IMG_DOC, depth=2))
    await probe("get_toc md", m.get_toc(doc_id=MD_DOC, depth=2))
    if isinstance(toc_img, dict):
        roots = toc_img.get("roots") or []
        print("image toc roots:", len(roots), "keys:", sorted(toc_img))
        print("status/parse_plan:", toc_img.get("status"), toc_img.get("parse_plan"))
        if roots:
            node_id = roots[0].get("node_id") or roots[0].get("id")
            await probe("read_section image",
                        m.read_section(doc_id=IMG_DOC, node_id=str(node_id), max_chars=600))

    await probe("search Chinese sentence (OCR doc)",
                m.search(query="不要求熟练掌握某种编程语言", top_k=3, actor_agent="probe"))
    await probe("search in collection",
                m.search(query="TPF 技术可行性验证 工作流 解构 重构", top_k=3,
                         collection="招聘与岗位情报", actor_agent="probe"))
    payload = await probe("search unknown collection",
                          m.search(query="有赞", top_k=3, collection="不存在的分区",
                                   actor_agent="probe"))
    if isinstance(payload, dict):
        print("collection_note:", payload.get("collection_note"))

    await probe("overview", m.memex_overview())
    await probe("get_toc unknown doc", m.get_toc(doc_id="nope", depth=2))

    if ingest:
        created = await probe("ingest_path (MCP)",
                              m.ingest_path(path=ingest, wait_seconds=0))
        job = (created or {}).get("job") if isinstance(created, dict) else None
        if job:
            for _ in range(120):
                state = await m.get_ingest(job_id=job["id"])
                status = (state or {}).get("job", {}).get("status")
                if status in {"ready", "failed", "duplicate"}:
                    show("get_ingest final", True, state)
                    break
                await asyncio.sleep(2)
            else:
                show("get_ingest timeout", False, state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-probe", action="store_true",
                        help="also test the dangling chunk_id case (now expected to fail)")
    parser.add_argument("--ingest", default=None,
                        help="ingest one file through the MCP tools and poll it")
    args = parser.parse_args()
    asyncio.run(main(args.write_probe, args.ingest))