"""Console-side tool loop: evidence, citations and the final answer.

Shared retrieval tools are not implemented here. ``app.agent.surface`` builds
their schemas from the MCP server and executes every call through
``server.call_tool()``, so the test bench behaves like an external MCP agent.
This module keeps only what is genuinely console-specific: the evidence map
built from reads, citation rendering, ``final_answer``, and the duplicate-call
policy.

Citations are a trace, not a gate. ``final_answer`` accepts an answer with
no sources at all; when the model names the chunks it used, the code records
those that it actually read this run. Which chunks were read is tracked in
``ToolContext.evidence`` either way, so the console can show the trail.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .call_guard import CallGuard, REPLAY_NOTE
from .surface import IDENTITY_ARGS, call_mcp


@dataclass
class ToolContext:
    db: AsyncSession
    run_id: str
    allow_notes: bool
    evidence: dict[int, dict] = field(default_factory=dict)
    # resource_id is only unique inside a document (tbl_5_0 exists in many
    # documents), so the evidence map must be keyed by both.
    resource_evidence: dict[tuple[str, str], dict] = field(default_factory=dict)
    resources_root: Path | None = None
    vision_blocked: bool = False
    final: dict | None = None
    # Successful calls replay their first result; failed calls are not cached
    # so a retry reaches the tool and its real error surfaces.
    call_guard: CallGuard = field(default_factory=CallGuard)


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _remember_read(ctx: ToolContext, payload: Any) -> None:
    """Every visible chunk segment becomes evidence for citations."""
    if not isinstance(payload, dict) or payload.get("error"):
        return
    chunks = list(payload.get("chunks") or [])
    if not chunks:
        # Callers that only produced concatenated content.
        chunk_ids = list(payload.get("chunk_ids") or [])
        chunks = [
            {
                "chunk_id": chunk_ids[0] if chunk_ids else None,
                "node_id": payload.get("node_id"),
                "page_start": payload.get("page_start"),
                "page_end": payload.get("page_end"),
                "heading_path": payload.get("heading_path") or "",
                "text": payload.get("content") or "",
            }
        ]
    title = payload.get("doc_title") or payload.get("doc_id")
    for segment in chunks:
        chunk_id = segment.get("chunk_id")
        if chunk_id is None:
            continue
        ctx.evidence[int(chunk_id)] = {
            "doc_id": payload.get("doc_id"),
            "doc_title": title,
            "node_id": segment.get("node_id"),
            "heading_path": segment.get("heading_path") or "",
            "page_start": segment.get("page_start"),
            "page_end": segment.get("page_end"),
            "content": segment.get("text") or "",
        }


def _remember_resource(
    ctx: ToolContext, payload: Any, images: tuple[dict[str, str], ...]
) -> None:
    if not isinstance(payload, dict) or payload.get("error"):
        return
    doc_id = payload.get("doc_id") or ""
    resource_id = payload.get("resource_id") or ""
    evidence = {
        "doc_id": doc_id,
        "doc_title": payload.get("doc_title") or doc_id,
        "resource_id": resource_id,
        "type": payload.get("type"),
        "title": payload.get("title"),
        "page": payload.get("page"),
        "node_id": payload.get("node_id"),
        "heading_path": payload.get("heading_path") or "",
        "content": None,
    }
    if payload.get("type") == "table" and (payload.get("content") or "").strip():
        evidence["content"] = payload["content"]
    ctx.resource_evidence[(doc_id, resource_id)] = evidence
    if images:
        if ctx.vision_blocked:
            payload["image_note"] = "当前端点不接受图片输入，已改用图注与正文"
        else:
            payload["_image"] = images[0]


def _chunk_citation(chunk_id: int, evidence: dict) -> dict:
    content = evidence.get("content") or ""
    return {
        "ref": None,
        "chunk_id": chunk_id,
        "resource_id": None,
        "resource_type": None,
        "resource_title": None,
        "resource_page": None,
        "doc_id": evidence.get("doc_id"),
        "doc_title": evidence.get("doc_title"),
        "node_id": evidence.get("node_id"),
        "heading_path": evidence.get("heading_path") or "",
        "page_start": evidence.get("page_start"),
        "page_end": evidence.get("page_end"),
        # The chunk text stands in for a quote: the console shows what the
        # answer rested on without asking the model to copy it verbatim.
        "quote": content[:240],
        "content": content[:2000],
    }


def _resource_citation(resource_id: str, evidence: dict) -> dict:
    content = evidence.get("content") or ""
    return {
        "ref": None,
        "chunk_id": None,
        "resource_id": resource_id,
        "resource_type": evidence.get("type"),
        "resource_title": evidence.get("title"),
        "resource_page": evidence.get("page"),
        "doc_id": evidence.get("doc_id"),
        "doc_title": evidence.get("doc_title"),
        "node_id": evidence.get("node_id"),
        "heading_path": evidence.get("heading_path") or "",
        "page_start": evidence.get("page_start"),
        "page_end": evidence.get("page_end"),
        "quote": content[:240],
        "content": content[:2000],
    }


def _final(ctx: ToolContext, args: dict) -> dict:
    """Record the answer and, best-effort, the evidence it used.

    Citations are a trace, not a gate. ``sources`` may be omitted, quotes are
    not required, and a chunk the model did not read this run is skipped
    instead of failing the run. What the model read already lives in
    ``ctx.evidence``; the console shows that regardless of this call.
    """
    answer = str(args.get("answer") or "").strip()
    if not answer:
        return {"error": "answer 不能为空"}
    if bool(args.get("insufficient")):
        ctx.final = {"answer": answer, "citations": [], "insufficient": True}
        return {"accepted": True, "insufficient": True}
    if not ctx.evidence and not ctx.resource_evidence:
        return {
            "error": (
                "本轮还没有读过任何切片；请先 read_section，"
                "或在档案确实没有证据时设置 insufficient=true"
            )
        }
    raw_sources = args.get("sources")
    if not isinstance(raw_sources, list):
        raw_sources = []
    citations: list[dict] = []
    seen: set[tuple[Any, ...]] = set()
    for source in raw_sources:
        if not isinstance(source, dict):
            continue
        resource_id = str(source.get("resource_id") or "").strip()
        doc_id = str(source.get("doc_id") or "").strip() or None
        if resource_id:
            entries = [
                (key, evidence)
                for key, evidence in ctx.resource_evidence.items()
                if key[1] == resource_id
                and (doc_id is None or key[0] == doc_id)
                and evidence.get("type") == "table"
                and (evidence.get("content") or "").strip()
            ]
            if len(entries) != 1:
                # Unknown or ambiguous table: skip it; the answer stands.
                continue
            key = ("r", entries[0][0][0], resource_id)
            if key in seen:
                continue
            seen.add(key)
            citations.append(_resource_citation(resource_id, entries[0][1]))
            continue
        try:
            chunk_id = int(source.get("chunk_id"))
        except (TypeError, ValueError):
            continue
        evidence = ctx.evidence.get(chunk_id)
        if evidence is None:
            continue
        key = ("c", chunk_id)
        if key in seen:
            continue
        seen.add(key)
        citations.append(_chunk_citation(chunk_id, evidence))
    for index, citation in enumerate(citations, start=1):
        citation["ref"] = index
    ctx.final = {"answer": answer, "citations": citations, "insufficient": False}
    return {"accepted": True, "citations": len(citations)}


async def dispatch(ctx: ToolContext, name: str, args: dict) -> Any:
    """Run one tool call through the MCP surface and return its payload."""
    if name == "final_answer":
        return _jsonable(_final(ctx, args))
    cached = ctx.call_guard.replay(name, args)
    if cached is not None:
        cached["note"] = REPLAY_NOTE
        return cached
    if name == "add_note" and not ctx.allow_notes:
        return {"error": "本轮不允许写手记"}
    kwargs = {**args, **IDENTITY_ARGS.get(name, {})}
    outcome = await call_mcp(
        ctx.db, ctx.run_id, name, kwargs, resources_root=ctx.resources_root
    )
    payload = outcome.payload
    if name == "read_section":
        _remember_read(ctx, payload)
    elif name == "read_resource":
        _remember_resource(ctx, payload, outcome.images)
    payload = _jsonable(payload)
    if not outcome.is_error:
        ctx.call_guard.remember(name, args, payload)
    return payload
