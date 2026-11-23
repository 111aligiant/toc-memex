"""Console runs for external MCP sessions.

The built-in agent writes its own run. An external agent (Claude Code, Codex,
an HTTP MCP client) has no agent loop to hook, so the MCP server observes it
instead: each connection gets a server-assigned ``session_id``, and the first
call on that connection opens an ``AgentRun`` with ``source="mcp"``. Every
observed call appends the same ``tool_call`` / ``tool_result`` rows the
built-in loop writes.

The console is for testing the built-in agent; an external agent is doing
real work, so its calls are archived under a task in the activity layer.
Calls from one client spaced closer than ``SESSION_IDLE_SECONDS`` share one
"外部会话" task — a client that opens a fresh connection per call (DSH does)
still gets one task. ``run_start`` starts a new task (or binds an existing
``task_id``) and ``run_end`` closes it. The ``agent_runs`` row remains as
that task's call trace but is no longer listed by the console history.

Observation must never break retrieval: every helper here can fail without
the caller noticing, and the wrapper in ``mcp_server`` swallows such errors.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import ensure_schema, make_engine, session_factory
from ..index.activity import Activity, Task
from ..models import AgentNote, new_uuid, utcnow_iso
from .call_guard import reset_session
from .models import AgentRun, AgentStep
from .surface import normalize_tool_result

MAX_JSON_CHARS = 40_000
MAX_QUESTION_CHARS = 2_000
AUTO_MAX_CALLS = 8

# Calls from the same client spaced closer than this belong to one session
# task. Explicit run_start/run_end always splits, so an agent that wants
# clear boundaries just says so.
SESSION_IDLE_SECONDS = 30 * 60
SESSION_KIND = "session"


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


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_generic_question(question: str) -> bool:
    """Titles derived from a bare read/navigation, not a human's question."""
    text = (question or "").strip()
    if not text:
        return True
    return text.startswith(
        (
            "读取",
            "查看目录",
            "查看文档",
            "列出文档",
            "概览",
            "浏览",
            "MCP 会话",
        )
    )


def _task_title(question: str, client: str | None, *, stamp: str | None = None) -> str:
    text = (question or "").strip()
    if text and not _is_generic_question(text):
        return text[:500]
    when = stamp or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    return f"外部会话 · {client or 'agent'} · {when}"


async def _session_task(
    db: AsyncSession,
    *,
    client: str | None,
    question: str,
    task_id: str | None = None,
    force_new: bool = False,
) -> Task:
    """Find the session task to file this call under, or open a new one.

    ``task_id`` (the caller's own task) wins. Otherwise calls from the same
    client within ``SESSION_IDLE_SECONDS`` share one task; ``force_new`` is
    used by run_start so an explicit boundary always makes a new task.
    """
    now = utcnow_iso()
    if task_id:
        task = await db.get(Task, task_id)
        if task is not None:
            # Keep the bump local to our own session tasks: an agent-created
            # task's ordering should follow its own work, not our calls.
            if task.kind == SESSION_KIND:
                task.updated_at = now
            return task
    if not force_new:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=SESSION_IDLE_SECONDS)
        ).isoformat()
        stmt = (
            select(Task)
            .where(
                Task.kind == SESSION_KIND,
                Task.status.in_(("open", "doing")),
                Task.updated_at >= cutoff,
            )
            .order_by(Task.updated_at.desc())
            .limit(1)
        )
        if client:
            stmt = stmt.where(Task.owner_agent == client)
        else:
            stmt = stmt.where(Task.owner_agent.is_(None))
        task = (await db.execute(stmt)).scalars().first()
        if task is not None:
            task.status = "doing"
            task.updated_at = now
            # A session that opened with "读取 …" gets a readable title once
            # its first real question arrives.
            if _is_generic_question(task.title) and not _is_generic_question(question):
                task.title = (question or "").strip()[:500]
            return task
    # No session is active for this client any more: close the ones that
    # went idle so the 任务 page does not collect forever-"doing" sessions.
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=SESSION_IDLE_SECONDS)
    ).isoformat()
    retire = (
        update(Task)
        .where(
            Task.kind == SESSION_KIND,
            Task.status.in_(("open", "doing")),
            Task.updated_at < cutoff,
        )
        .values(status="done", updated_at=now)
    )
    if client:
        retire = retire.where(Task.owner_agent == client)
    else:
        retire = retire.where(Task.owner_agent.is_(None))
    await db.execute(retire)
    task = Task(
        id=new_uuid(),
        title=_task_title(question, client),
        goal=f"外部 agent 会话 · {client or 'anonymous'}",
        kind=SESSION_KIND,
        status="doing",
        owner_agent=client or None,
        created_at=now,
        updated_at=now,
    )
    db.add(task)
    await db.flush()
    return task


async def _close_task_if_idle(
    db: AsyncSession, run: AgentRun | None, status: str
) -> None:
    """Mark the run's task closed once no other run is still working on it."""
    if run is None or not run.task_id:
        return
    # Runs whose client vanished without run_end stay "running" forever.
    # Only a recently active run (same grouped session) blocks closing.
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=SESSION_IDLE_SECONDS)
    ).isoformat()
    still_running = (
        await db.execute(
            select(AgentRun.id)
            .where(
                AgentRun.task_id == run.task_id,
                AgentRun.status == "running",
                AgentRun.id != run.id,
                AgentRun.last_seen_at >= cutoff,
            )
            .limit(1)
        )
    ).first()
    if still_running is not None:
        return
    task = await db.get(Task, run.task_id)
    if task is None or task.kind != SESSION_KIND:
        return
    if task.status in ("open", "doing"):
        task.status = status
        task.updated_at = utcnow_iso()


async def session_task_id(
    db: AsyncSession,
    *,
    session_id: str | None,
    client: str | None,
    question: str = "",
) -> str | None:
    """Task id for a write from an external connection.

    Used by tools that are not wrapped in ``observed`` (activity_write,
    lesson_write, score_write): they still belong to the connection's work.
    A task is opened on demand so the first call is never unattributed.
    """
    if not session_id:
        return None
    run = await active_run(db, session_id)
    if run is not None and run.task_id:
        return run.task_id
    task = await _session_task(db, client=client, question=question)
    return task.id


async def ensure_run(
    settings,
    *,
    session_id: str | None,
    client: str | None,
    tool: str,
    args: dict,
) -> str | None:
    """Open this connection's run before the tool body runs.

    The tool needs the run's task id while it writes its activity row; doing
    this after the call (as ``record_call`` does) would file the first read
    outside the task. Best-effort, like every other helper here.
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
            else:
                run.last_seen_at = utcnow_iso()
            return run.task_id


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
    explicit_task = str(args.get("task_id") or "").strip() or None
    task = await _session_task(
        db, client=client, question=question, task_id=explicit_task
    )
    now = utcnow_iso()
    run = AgentRun(
        question=question[:MAX_QUESTION_CHARS],
        status="running",
        source="mcp",
        session_id=session_id,
        client=client,
        task_id=task.id,
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
    await _close_task_if_idle(db, run, "done")
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
    task_id: str | None = None,
) -> AgentRun:
    """Open a named run for this MCP session.

    A connection can own only one running run; an earlier one is closed
    first so later tool calls cannot be attributed to the wrong question.
    ``task_id`` binds the work to an existing task; without it a new
    session task is opened, so the question the agent declares stays the
    task title on the 任务 page.
    """
    reset_session(session_id)
    await _close_active(db, session_id, "客户端开始了新的 run")
    now = utcnow_iso()
    full_question = (question or "").strip()[:MAX_QUESTION_CHARS] or "MCP 会话"
    task = await _session_task(
        db,
        client=client,
        question=full_question,
        task_id=(task_id or "").strip() or None,
        force_new=True,
    )
    run = AgentRun(
        question=full_question,
        status="running",
        source="mcp",
        session_id=session_id,
        client=client,
        task_id=task.id,
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
            if run.task_id:
                # The task timeline is the archive the 任务 page renders; a
                # final answer that lives only in the run trace would be
                # invisible there.
                db.add(
                    Activity(
                        kind="note",
                        task_id=run.task_id,
                        actor_agent=run.client or "external",
                        session_id=session_id,
                        content=answer,
                    )
                )
        run.status = "done"
        await _append_step(db, run.id, "done")
    run.finished_at = now
    run.last_seen_at = now
    await _close_task_if_idle(db, run, "blocked" if status == "failed" else "done")
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


async def backfill_external_tasks(engine) -> int:
    """File legacy ``source="mcp"`` runs under session tasks.

    Idempotent: only runs whose ``task_id`` is still NULL are considered, so
    the sweep costs one indexed query on every start after the first. Runs
    are grouped by client and a ``SESSION_IDLE_SECONDS`` idle gap (the same
    rule live calls use), and the reads/notes those sessions already wrote
    are attached to the task so the 任务 page shows the whole session.
    Returns the number of tasks created.
    """
    async with session_factory(engine)() as db:
        runs = (
            (
                await db.execute(
                    select(AgentRun)
                    .where(AgentRun.source == "mcp", AgentRun.task_id.is_(None))
                    .order_by(AgentRun.created_at, AgentRun.id)
                )
            )
            .scalars()
            .all()
        )
        if not runs:
            return 0

        groups: list[list[AgentRun]] = []
        current: list[AgentRun] = []
        last_seen: datetime | None = None
        for run in runs:
            created = _parse_iso(run.created_at)
            gap = (
                created is not None
                and last_seen is not None
                and (created - last_seen).total_seconds() > SESSION_IDLE_SECONDS
            )
            if current and (run.client != current[-1].client or gap):
                groups.append(current)
                current = []
            current.append(run)
            last_seen = created
        if current:
            groups.append(current)

        created_count = 0
        for group in groups:
            first, last = group[0], group[-1]
            title = _task_title(
                next(
                    (
                        run.question
                        for run in group
                        if not _is_generic_question(run.question)
                    ),
                    first.question,
                ),
                first.client,
                stamp=(first.created_at or "")[:16].replace("T", " "),
            )
            task = Task(
                id=new_uuid(),
                title=title,
                goal=f"外部 agent 会话 · {first.client or 'anonymous'}",
                kind=SESSION_KIND,
                status="done",
                owner_agent=first.client or None,
                created_at=first.created_at or utcnow_iso(),
                updated_at=last.created_at or utcnow_iso(),
            )
            db.add(task)
            await db.flush()
            created_count += 1
            run_ids = [run.id for run in group]
            await db.execute(
                update(AgentRun)
                .where(AgentRun.id.in_(run_ids))
                .values(task_id=task.id)
            )
            for run in group:
                if run.answer:
                    db.add(
                        Activity(
                            kind="note",
                            task_id=task.id,
                            actor_agent=run.client or "external",
                            session_id=run.session_id,
                            content=run.answer,
                            created_at=run.finished_at or run.created_at,
                        )
                    )
            sessions = {run.session_id for run in group if run.session_id}
            if sessions:
                await db.execute(
                    update(Activity)
                    .where(
                        Activity.session_id.in_(sessions),
                        Activity.task_id.is_(None),
                    )
                    .values(task_id=task.id)
                )
                await db.execute(
                    update(AgentNote)
                    .where(
                        AgentNote.session_id.in_(sessions),
                        AgentNote.task_id.is_(None),
                    )
                    .values(task_id=task.id)
                )
        await db.commit()
        return created_count
