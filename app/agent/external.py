"""Console runs for external MCP sessions.

The built-in agent writes its own run. An external agent (Claude Code, Codex,
an HTTP MCP client) has no agent loop to hook, so the MCP server observes it
instead: each connection gets a server-assigned ``session_id``, and the first
retrieval call on that connection opens an ``AgentRun`` with
``source="mcp"``. Every observed call appends the same ``tool_call`` /
``tool_result`` rows the built-in loop writes, so the console renders both
through one timeline.

``run_start`` names the work explicitly (and is what agents should call at
the beginning of a task); ``run_end`` closes it with an answer or an error.
Without them the run is still recorded, but its question is derived from the
first call and it stays open until the client starts a new run.

Observation must never break retrieval: every helper here can fail without
the caller noticing, and the wrapper in ``mcp_server`` swallows such errors.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import ensure_schema, make_engine, session_factory
from ..models import utcnow_iso
from .call_guard import reset_session
from .models import AgentRun, AgentStep
from .surface import normalize_tool_result

MAX_JSON_CHARS = 40_000
MAX_QUESTION_CHARS = 2_000
AUTO_MAX_CALLS = 8

# One lock serialises seq allocation inside this process. A single connection
# may issue parallel tool calls, and ``UNIQUE(run_id, seq)`` would reject two
# steps that read the same MAX(seq). Separate processes own separate runs.
_STEP_LOCK = asyncio.Lock()


@asynccontextmanager
async def _db(settings) -> Any:
    engine = make_engine(settings.tocmemex_db_path)
    await ensure_schema(engine)
    session = session_factory(engine)()
    try:
        yield session
        await session.commit()
    finally:
        await session.close()
        await engine.dispose()


def _is_image(value: Any) -> bool:
    # mcp.server.mcpserver.utilities.types.Image keeps bytes in .data; a
    # base64 preview would bloat the step and tell the reader nothing.
    return hasattr(value, "data") and hasattr(value, "_mime_type")


def _json_text(value: Any, limit: int = MAX_JSON_CHARS) -> str:
    if _is_image(value):
        data = getattr(value, "data", None)
        path = getattr(value, "path", None)
        return json.dumps(
            {
                "image": True,
                "mimeType": getattr(value, "_mime_type", None),
                "bytes": len(data) if isinstance(data, (bytes, bytearray)) else None,
                "path": str(path) if path is not None else None,
            },
            ensure_ascii=False,
        )
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = json.dumps(str(value), ensure_ascii=False)
    if len(text) <= limit:
        return text
    return json.dumps(
        {"_truncated": True, "preview": text[:limit]},
        ensure_ascii=False,
    )


async def _append_step(
    db: AsyncSession,
    run_id: str,
    type_: str,
    *,
    name: str | None = None,
    args: Any = None,
    result: Any = None,
    content: str | None = None,
    elapsed_ms: int | None = None,
) -> AgentStep:
    last = (
        await db.execute(
            select(func.max(AgentStep.seq)).where(AgentStep.run_id == run_id)
        )
    ).scalar() or 0
    step = AgentStep(
        run_id=run_id,
        seq=last + 1,
        type=type_,
        name=name,
        args_json=_json_text(args) if args is not None else None,
        result_json=_json_text(result) if result is not None else None,
        content=content,
        elapsed_ms=elapsed_ms,
    )
    db.add(step)
    await db.flush()
    return step


async def active_run(db: AsyncSession, session_id: str | None) -> AgentRun | None:
    """The running MCP run owned by this connection, if any."""
    if not session_id:
        return None
    return (
        (
            await db.execute(
                select(AgentRun)
                .where(
                    AgentRun.session_id == session_id,
                    AgentRun.source == "mcp",
                    AgentRun.status == "running",
                )
                .order_by(AgentRun.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


def _question_for(tool: str, args: dict) -> str:
    def text(key: str) -> str:
        value = args.get(key)
        return str(value).strip() if value else ""

    if tool in ("search", "ask"):
        return text("query") or text("question")
    if tool == "read_section":
        doc_id, node_id = text("doc_id"), text("node_id")
        return f"读取 {doc_id} · {node_id}".strip(" ·")
    if tool == "get_toc":
        return f"查看目录 {text('doc_id')}".strip()
    if tool == "get_document":
        return f"查看文档 {text('doc_id')}".strip()
    if tool == "list_documents":
        query = text("query")
        return f"列出文档 {query}".strip()
    if tool == "list_collections":
        return "浏览分区"
    if tool == "memex_overview":
        return "浏览库概览"
    if tool == "overview":
        ids = "、".join(str(item) for item in (args.get("doc_ids") or []))
        return text("query") or (f"概览 {ids}" if ids else "浏览文档概览")
    if tool == "read_resource":
        resource_id = text("resource_id")
        doc_id = text("doc_id")
        target = f"{doc_id} · {resource_id}".strip(" ·")
        return f"读取资源 {target}".strip()
    return ""


async def _open_auto_run(
    db: AsyncSession,
    *,
    session_id: str,
    client: str | None,
    tool: str,
    args: dict,
) -> AgentRun:
    question = _question_for(tool, args) or f"MCP 会话 · {client or 'anonymous'}"
    now = utcnow_iso()
    run = AgentRun(
        question=question[:MAX_QUESTION_CHARS],
        status="running",
        source="mcp",
        session_id=session_id,
        client=client,
        allow_notes=True,
        max_calls=AUTO_MAX_CALLS,
        started_at=now,
        last_seen_at=now,
    )
    db.add(run)
    await db.flush()
    await _append_step(db, run.id, "start", content=run.question)
    return run


async def _close_active(
    db: AsyncSession, session_id: str | None, note: str
) -> AgentRun | None:
    run = await active_run(db, session_id)
    if run is None:
        return None
    now = utcnow_iso()
    run.status = "done"
    run.finished_at = now
    run.last_seen_at = now
    await _append_step(db, run.id, "done", content=note)
    return run


async def start_run(
    db: AsyncSession,
    *,
    session_id: str | None,
    client: str | None,
    question: str,
    allow_notes: bool = True,
    max_calls: int = AUTO_MAX_CALLS,
    prior_run_id: str | None = None,
) -> AgentRun:
    """Open a named run for this MCP session.

    A connection can own only one running run; an earlier one is closed
    first so later tool calls cannot be attributed to the wrong question.
    """
    reset_session(session_id)
    await _close_active(db, session_id, "客户端开始了新的 run")
    now = utcnow_iso()
    run = AgentRun(
        question=(question or "").strip()[:MAX_QUESTION_CHARS] or "MCP 会话",
        status="running",
        source="mcp",
        session_id=session_id,
        client=client,
        allow_notes=bool(allow_notes),
        max_calls=max(1, min(int(max_calls or AUTO_MAX_CALLS), 20)),
        prior_run_id=prior_run_id or None,
        started_at=now,
        last_seen_at=now,
    )
    db.add(run)
    await db.flush()
    await _append_step(db, run.id, "start", content=run.question)
    return run


async def finish_run(
    db: AsyncSession,
    *,
    session_id: str | None,
    status: str = "done",
    answer: str | None = None,
    error: str | None = None,
) -> AgentRun | None:
    reset_session(session_id)
    run = await active_run(db, session_id)
    if run is None:
        return None
    now = utcnow_iso()
    if status == "failed":
        run.status = "failed"
        run.error = (error or "外部 agent 结束于失败状态")[:2000]
        await _append_step(db, run.id, "error", content=run.error)
    else:
        if answer:
            run.answer = answer
            await _append_step(
                db,
                run.id,
                "answer",
                content=answer,
                result={"citations": [], "insufficient": False, "external": True},
            )
        run.status = "done"
        await _append_step(db, run.id, "done")
    run.finished_at = now
    run.last_seen_at = now
    return run


async def record_call(
    settings,
    *,
    session_id: str | None,
    client: str | None,
    tool: str,
    args: dict,
    result: Any = None,
    elapsed_ms: int | None = None,
    error: str | None = None,
) -> str | None:
    """Append one observed MCP call to its session's run.

    Returns the run id, or ``None`` when the caller has no session (an
    in-process tool call) and there is nothing to attribute the call to.
    """
    if not session_id:
        return None
    async with _STEP_LOCK:
        async with _db(settings) as db:
            run = await active_run(db, session_id)
            if run is None:
                run = await _open_auto_run(
                    db,
                    session_id=session_id,
                    client=client,
                    tool=tool,
                    args=args,
                )
            run.last_seen_at = utcnow_iso()
            await _append_step(db, run.id, "tool_call", name=tool, args=args)
            if error is None:
                # read_resource returns a list of blocks; the console and the
                # task bar both read the payload dict, not the transport list.
                result = normalize_tool_result(tool, result)
            if error is not None:
                await _append_step(
                    db,
                    run.id,
                    "tool_result",
                    name=tool,
                    result={"error": error},
                    elapsed_ms=elapsed_ms,
                )
            else:
                kind = (
                    "note"
                    if tool == "add_note"
                    and isinstance(result, dict)
                    and "id" in result
                    else "tool_result"
                )
                if kind == "note":
                    # Older MCP clients may return the note without its text;
                    # the call args are the authoritative copy for display.
                    result = dict(result)
                    result.setdefault("body", args.get("body"))
                    result.setdefault("kind", args.get("kind") or "note")
                    result.setdefault("doc_id", args.get("doc_id") or None)
                    result.setdefault("node_id", args.get("node_id") or None)
                await _append_step(
                    db,
                    run.id,
                    kind,
                    name=tool,
                    result=result,
                    elapsed_ms=elapsed_ms,
                )
            return run.id
