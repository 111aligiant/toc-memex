from __future__ import annotations

import argparse
import contextvars
import functools
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession

from .agent import external as _external
from .agent.call_guard import REPLAY_NOTE, guard_for
from .agent.instructions import FULL_INSTRUCTIONS
from .bindguard import enforce_bind_host
from .config import get_settings
from .db import ensure_schema, make_engine, session_factory
from .index import activity as _activity
from .index import collections as _collections
from .index.activity import Erratum, Task, UsageLog
from .index.ask import ask_question
from .index.documents import memex_overview as _memex_overview
from .index.documents import get_document as _get_document
from .index.documents import get_toc as _get_toc
from .index.documents import list_documents as _list_documents
from .index.fts import scoped_search
from .index.overview import overview as _overview
from .index import resources as _resources
from .index.sections import read_section as _read_section
from .models import AgentNote, Document

server = MCPServer(
    "toc-memex",
    title="Toc Memex",
    description=(
        "Toc Memex: a local-first AI memory over your documents, with TOC "
        "sections, cited search, extractive answers, and agent hand-off notes."
    ),
    instructions=FULL_INSTRUCTIONS,
    version="0.1.0",
)


@dataclass(frozen=True)
class Bridge:
    """In-process caller (the built-in console agent) using the MCP tools.

    The console has its own database session, run id and resources root; the
    MCP tool bodies must use those instead of opening a fresh engine from
    settings. External MCP clients never set this, so their behavior is
    unchanged.
    """

    session: AsyncSession
    session_id: str | None = None
    resources_root: Path | None = None


_BRIDGE: contextvars.ContextVar[Bridge | None] = contextvars.ContextVar(
    "tocmemex_mcp_bridge", default=None
)


@contextmanager
def bridge(
    session: AsyncSession,
    session_id: str | None = None,
    resources_root: Path | None = None,
):
    token = _BRIDGE.set(
        Bridge(session=session, session_id=session_id, resources_root=resources_root)
    )
    try:
        yield
    finally:
        _BRIDGE.reset(token)


def _bridge() -> Bridge | None:
    return _BRIDGE.get()


def _log_session_id(ctx: Context | None) -> str | None:
    """Session id for activity rows: the connection's, else the console run's."""
    bridged = _bridge()
    if bridged is not None and bridged.session_id:
        return bridged.session_id
    return _session_id(ctx)


def _session_id(ctx: Context | None) -> str | None:
    """Server-assigned id for the caller's MCP connection.

    Cached on the connection object, which is stable for the life of one
    client connection and distinct between connections (verified: two
    sequential stdio clients get different ids, two calls in one
    connection share one). This is the only identity signal a client
    cannot forge — but it identifies a *connection*, not a model.
    """
    if ctx is None:
        return None
    try:
        connection = ctx.session._connection
    except Exception:
        return None
    existing = getattr(connection, "_memex_session_id", None)
    if existing:
        return existing
    assigned = uuid.uuid4().hex
    try:
        connection._memex_session_id = assigned
    except Exception:
        return None
    return assigned


def _client_name(ctx: Context | None) -> str | None:
    """The client's self-declared name from the MCP handshake.

    Unverified: any client may claim any name, and SDKs default it to
    "mcp". Used only as a fallback when the caller omits `author`.
    """
    if ctx is None:
        return None
    try:
        params = ctx.session.client_params
    except Exception:
        return None
    if params is None or params.client_info is None:
        return None
    return params.client_info.name or None


def observed(fn):
    """Record one MCP tool call in the connection's console run.

    The run is opened on the first observed call (or by ``run_start``) and
    every call appends ``tool_call`` + ``tool_result`` steps, the same shape
    the built-in loop writes. Recording is best-effort: a broken console
    write must never fail the retrieval the agent actually asked for.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        if _bridge() is not None:
            # In-process console call: the runner records the steps itself.
            return await fn(*args, **kwargs)
        ctx = kwargs.get("ctx")
        session_id = _session_id(ctx)
        if not session_id:
            # In-process tool calls (tests, nested use) have no connection
            # identity, so there is nothing to attribute the call to.
            return await fn(*args, **kwargs)
        client = _client_name(ctx)
        call_args = {key: value for key, value in kwargs.items() if key != "ctx"}
        settings = get_settings()
        guard = guard_for(
            str(settings.tocmemex_db_path), session_id, max_entries=64
        )
        replay = guard.replay(fn.__name__, call_args)
        if replay is not None:
            # Same policy as the built-in loop: the call still appears in the
            # console timeline, but the tool does not run a second time.
            replay["note"] = REPLAY_NOTE
            try:
                await _external.record_call(
                    settings,
                    session_id=session_id,
                    client=client,
                    tool=fn.__name__,
                    args=call_args,
                    result=replay,
                    elapsed_ms=0,
                )
            except Exception:
                pass
            return replay

        started = time.monotonic()
        try:
            result = await fn(*args, **kwargs)
        except Exception as exc:
            try:
                await _external.record_call(
                    settings,
                    session_id=session_id,
                    client=client,
                    tool=fn.__name__,
                    args=call_args,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    error=f"{type(exc).__name__}: {exc}",
                )
            except Exception:
                pass
            raise
        guard.remember(fn.__name__, call_args, result)
        try:
            await _external.record_call(
                settings,
                session_id=session_id,
                client=client,
                tool=fn.__name__,
                args=call_args,
                result=result,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception:
            pass
        return result

    return wrapper


async def _with_db(
    fn: Callable[[AsyncSession], Awaitable[Any]],
) -> Any:
    bridged = _bridge()
    if bridged is not None:
        db = bridged.session
        try:
            result = await fn(db)
        except _collections.CollectionNotFound as exc:
            await _safe_rollback(db)
            raise ToolError(str(exc)) from exc
        except Exception:
            await _safe_rollback(db)
            raise
        await db.commit()
        return result

    settings = get_settings()
    engine = make_engine(settings.tocmemex_db_path)
    await ensure_schema(engine)
    try:
        async with session_factory(engine)() as db:
            try:
                result = await fn(db)
            except _collections.CollectionNotFound as exc:
                raise ToolError(str(exc)) from exc
            # Side-effect tools (read_section auto-logging, activity_write)
            # only flush; without this commit the session close rolls the
            # writes back and the activity log silently loses rows.
            await db.commit()
            return result
    finally:
        await engine.dispose()


async def _safe_rollback(db: AsyncSession) -> None:
    """Never let cleanup hide the tool's real exception."""
    try:
        await db.rollback()
    except Exception:
        pass


def _tool_error(exc: Exception) -> ToolError:
    """Turn a library-level error into something the agent can act on.

    Any exception the SDK does not recognise reaches the agent as the bare
    "Error executing tool <name>". CollectionNotFound's value is its
    listing of valid collections, so that text must survive the boundary.
    """
    return ToolError(str(exc))


@server.tool()
@observed
async def memex_overview(ctx: Context | None = None) -> dict:
    """Show document counts, type distribution, recent documents, and the
    collection (topic area) distribution."""
    return await _with_db(_memex_overview)


@server.tool()
@observed
async def overview(
    query: str = "",
    doc_ids: list[str] | None = None,
    limit: int = 10,
    ctx: Context | None = None,
) -> dict:
    """One compact overview of several documents: summary, keywords and top headings."""
    return await _with_db(
        lambda db: _overview(db, query=query, doc_ids=doc_ids or [], limit=limit)
    )


@server.tool()
@observed
async def list_collections(ctx: Context | None = None) -> dict:
    """List the library's topic collections, with the description agents
    use to decide where a request belongs, and the unfiled document count.

    Call this first when a request is about a specific topic, then pass
    ``collection=`` (id or exact name) to search(), ask(), or
    list_documents(). A collection whose description is empty carries
    ``description_missing: true`` — its name is all you have to go on.
    """
    async def _run(db: AsyncSession) -> dict:
        rows = await _collections.list_collections(db)
        for row in rows:
            if not row["description"]:
                row["description_missing"] = True
        return {
            "collections": rows,
            "unassigned_doc_count": await _collections.unassigned_doc_count(db),
        }

    return await _with_db(_run)


@server.tool()
@observed
async def list_documents(
    query: str = "",
    doc_type: str = "",
    limit: int = 20,
    collection: str = "",
    ctx: Context | None = None,
) -> list[dict]:
    """List documents, optionally filtered by title/filename, doc_type, or
    collection (topic area id/name, or "unassigned")."""
    return await _with_db(
        lambda db: _list_documents(
            db, query=query, doc_type=doc_type, limit=limit, collection=collection
        )
    )


@server.tool()
@observed
async def get_document(doc_id: str, ctx: Context | None = None) -> dict:
    """Get document metadata, counts, and top-level TOC roots."""
    return await _with_db(lambda db: _get_document(db, doc_id))


@server.tool()
@observed
async def get_toc(doc_id: str, depth: int = 2, ctx: Context | None = None) -> dict:
    """Get the TOC tree. depth=2 returns roots and their children."""
    if not (doc_id or "").strip():
        raise ToolError("doc_id 不能为空")
    depth = max(1, min(int(depth or 1), 4))

    async def _run(db: AsyncSession) -> dict:
        try:
            return await _get_toc(db, doc_id, depth=depth)
        except ValueError as exc:
            raise _tool_error(exc) from exc

    return await _with_db(_run)


@server.tool()
@observed
async def read_section(
    doc_id: str,
    node_id: str,
    max_chars: int = 8000,
    cursor: int = 0,
    actor_agent: str = "",
    task_id: str = "",
    ctx: Context | None = None,
) -> dict:
    """Read a whole section by node_id, with citation metadata.

    Pass actor_agent (your name) and task_id so the read is logged —
    the log answers "which agent used which material under which task".
    Long sections come back in pages: ``max_chars`` caps how much text one
    call returns (default 8000, clamped to 500–20000), and ``cursor`` says
    where to start (default 0). When the response's ``next_cursor`` is not
    null, there is more text — call again with ``cursor=next_cursor``.
    The response includes any confirmed errata for this section. Copy
    node_id from search/get_toc rather than building it from a section
    number; a guessed number is resolved to its parent section when no
    node exists (the response then carries ``resolved_from``).
    """
    if not (doc_id or "").strip() or not (node_id or "").strip():
        raise ToolError("doc_id 和 node_id 都不能为空")
    max_chars = max(500, min(int(max_chars or 8000), 20000))
    cursor = max(0, int(cursor or 0))
    async with _session() as db:
        try:
            result = await _read_section(
                db,
                doc_id,
                node_id=node_id,
                max_chars=max_chars,
                cursor=cursor,
                reader_agent=actor_agent or None,
                task_id=task_id or None,
                session_id=_log_session_id(ctx),
            )
        except ValueError as exc:
            raise _tool_error(exc) from exc
        errata = await _activity.errata_for_chunks(
            db, doc_id, result.get("chunk_ids", []), [result.get("node_id") or node_id]
        )
        chunks = result.get("chunks") or []
        if chunks:
            # Bind each figure to the chunk that covers it, so an external
            # agent sees the picture handle next to the text it recalls.
            for chunk in chunks:
                chunk["doc_id"] = doc_id
            try:
                await _resources.attach_resources(db, chunks)
            except Exception:
                pass  # resource refs are an aid, never a reason a read fails
            section_resources: list[dict] = []
            seen: set[tuple[str, str]] = set()
            for chunk in chunks:
                chunk["figures"] = _resources.figures_of(chunk)
                for ref in chunk.get("resources") or []:
                    key = (ref.get("type") or "", ref.get("resource_id") or "")
                    if key in seen:
                        continue
                    seen.add(key)
                    section_resources.append(ref)
            result["resources"] = section_resources[:12]
        else:
            try:
                await _resources.attach_resources(db, [result])
            except Exception:
                pass
            result["figures"] = _resources.figures_of(result)
        await db.commit()
    if errata:
        result["errata"] = [
            _activity.erratum_out(erratum) for erratum in errata
        ]
    return result


@server.tool()
@observed
async def list_resources(
    doc_id: str,
    page_start: int | None = None,
    page_end: int | None = None,
    type: str = "",
    query: str = "",
    limit: int = 50,
    ctx: Context | None = None,
) -> dict:
    """List a document's tables and figures as lightweight handles.

    Use this when you need a table or figure without running a text search:
    filter by ``type`` ("table" / "figure"), by 0-based page range, or by a
    caption substring in ``query`` (``"表1"`` / ``"图3"``). Each handle
    carries ``resource_id``, ``type``, ``title``, ``page``, ``node_id``,
    ``has_content`` and ``has_image``. Fetch the payload with
    read_resource(); resource ids are opaque handles, so never parse them.
    """

    async def _run(db: AsyncSession) -> dict:
        try:
            return await _resources.list_resources(
                db,
                doc_id,
                page_start=page_start,
                page_end=page_end,
                type_=type,
                query=query,
                limit=limit,
            )
        except ValueError as exc:
            raise _tool_error(exc) from exc

    return await _with_db(_run)


@server.tool()
@observed
async def read_resource(
    doc_id: str,
    resource_id: str,
    include_image: bool = False,
    actor_agent: str = "",
    task_id: str = "",
    ctx: Context | None = None,
) -> list[dict | Image]:
    """Fetch one table or figure by resource_id.

    A table comes back as a rectangular Markdown grid (merged OCR cells are
    expanded). A figure returns its caption and metadata; pass
    ``include_image=True`` to also receive the image as MCP image content.
    Pass actor_agent (your name) and task_id to log the read. ``page`` is
    0-based and matches the page ranges in search/read_section citations.
    """
    async with _session() as db:
        try:
            payload, image_path = await _resources.get_resource(
                db, doc_id, resource_id
            )
        except ValueError as exc:
            raise _tool_error(exc) from exc
        if actor_agent:
            try:
                await _activity.activity_write(
                    db,
                    "read",
                    actor_agent,
                    task_id=task_id or None,
                    doc_id=doc_id,
                    node_id=payload.get("node_id"),
                    session_id=_log_session_id(ctx),
                )
            except ValueError:
                # A bad task_id must not stop the read itself.
                pass
        await db.commit()

    content: list[dict | Image] = [payload]
    if include_image:
        meta = {
            "mime": _resources.image_mime(image_path or ""),
            "bytes": 0,
            "url": f"/api/documents/{doc_id}/resources/{resource_id}/image",
        }
        payload["image_meta"] = meta
        if payload.get("type") != "figure":
            payload["image_include_error"] = "只有 figure 类型的资源可以取图"
        else:
            bridged = _bridge()
            root = (bridged.resources_root if bridged is not None else None) or (
                get_settings().resources_dir
            )
            safe = _resources.image_file(image_path, root)
            if safe is None:
                payload["image_include_error"] = "图片文件不可用或不在资源目录中"
            else:
                size = safe.stat().st_size
                meta["mime"] = _resources.image_mime(safe)
                meta["bytes"] = size
                if size > 4 * 1024 * 1024:
                    payload["image_include_error"] = "图片超过 4 MB，未发送给模型"
                else:
                    content.append(Image(path=safe))
    elif payload.get("type") == "figure":
        payload["hint"] = (
            "图不是引用证据；需要看图时调用 "
            f"read_resource(include_image=true) 读取 {resource_id}"
        )
    return content


@server.tool()
@observed
async def search(
    query: str,
    top_k: int = 8,
    collection: str = "",
    actor_agent: str = "",
    task_id: str = "",
    ctx: Context | None = None,
) -> dict:
    """Section-first search with heading_path, page range, and chunk_ids.

    Returns ``{"results": [...], "hint": str | null, "scope": {...}}``.
    Every hit carries its ``collection`` ({"id","name"} or null when the
    document is unfiled). Pass ``collection`` (id, exact name, or
    "unassigned") to narrow the search to one topic area; an unknown value
    falls back to a whole-library search and adds ``collection_note``
    listing the valid collections. When a scoped search finds nothing,
    ``hint`` names the collections that do have hits — re-run without the
    filter or pick another.

    Every search is usage-logged (query, hits, ranks) for relevance
    feedback; call mark_used() on the chunk_ids you actually rely on.
    """
    query = (query or "").strip()
    if not query:
        raise ToolError("query 不能为空")
    top_k = max(1, min(int(top_k or 8), 12))
    async with _session() as db:
        try:
            payload = await scoped_search(
                db, query, top_k=top_k, collection=collection
            )
        except _collections.CollectionNotFound as exc:
            # The model often derives a collection name from a document title.
            # A dead end costs a turn and teaches nothing; a whole-library
            # search still finds the document.
            payload = await scoped_search(db, query, top_k=top_k, collection="")
            valid = "、".join(name for name, _count in exc.valid[:10]) or "（无）"
            payload["collection_note"] = (
                f"未找到分区「{collection}」，已改为全库检索；可用分区：{valid}"
            )
        hits = payload["results"]
        try:
            await _activity.usage_log_write(
                db, query, "search", hits,
                actor_agent=actor_agent or None,
                task_id=task_id or None,
            )
        except Exception:
            pass  # logging must never break retrieval
        await db.commit()
    if hits:
        try:
            async with _session() as db:
                per_hit = await _activity.errata_by_hit(db, hits)
            for hit, errata in zip(hits, per_hit):
                hit["errata"] = errata
        except Exception:
            pass
    if hits:
        try:
            async with _session() as db:
                await _resources.attach_resources(db, hits)
            for hit in hits:
                hit["figures"] = _resources.figures_of(hit)
        except Exception:
            pass
    return payload


@server.tool()
@observed
async def ask(
    question: str,
    top_k: int = 3,
    max_chars: int = 1200,
    collection: str = "",
    actor_agent: str = "",
    task_id: str = "",
    ctx: Context | None = None,
) -> dict:
    """Extractive answer with section citations.

    ``collection`` narrows retrieval to one topic area (id, exact name, or
    "unassigned"). Citations carry their ``collection``; a zero-hit scoped
    answer explains which collections do have hits. Retrieval is
    usage-logged like search; the answer carries confirmed errata for the
    cited sections under "errata".
    """
    async with _session() as db:
        try:
            result = await ask_question(
                db, question, top_k=top_k, max_chars=max_chars, collection=collection
            )
        except _collections.CollectionNotFound as exc:
            raise _tool_error(exc) from exc
        hits = result.get("hits", [])
        try:
            await _activity.usage_log_write(
                db, question, "ask", hits,
                actor_agent=actor_agent or None,
                task_id=task_id or None,
            )
        except Exception:
            pass
        try:
            errata = await _activity.confirmed_errata_hint(db, hits)
        except Exception:
            errata = []
        await db.commit()
    if errata:
        result["errata"] = errata
    anchors = [*result.get("citations", []), *result.get("hits", [])]
    if anchors:
        try:
            async with _session() as db:
                await _resources.attach_resources(db, anchors)
            for anchor in anchors:
                anchor["figures"] = _resources.figures_of(anchor)
        except Exception:
            pass
    return result


@server.tool()
@observed
async def mark_used(
    chunk_id: int,
    actor_agent: str = "",
    task_id: str = "",
    ctx: Context | None = None,
) -> dict:
    """Flag a retrieved chunk as actually used in your answer/work.
    usage_log rows start used=0; marking them builds the signal for
    which Toc Memex content genuinely helps (future relevance weighting).
    """
    from sqlalchemy import update

    async with _session() as db:
        stmt = (
            update(_activity.UsageLog)
            .where(
                _activity.UsageLog.chunk_id == chunk_id,
                _activity.UsageLog.used == 0,
            )
            .values(used=1, actor_agent=actor_agent or None,
                    task_id=task_id or None)
        )
        result = await db.execute(stmt)
        await db.commit()
        return {"marked": result.rowcount}


# ── console runs for external agents ──────────────────────────────────
# The console watches a connection through these two tools plus the
# automatic observation in `observed`. run_start names the work; run_end
# closes it with a real answer. Neither is required: the first retrieval
# call on a connection opens a run on its own, so an agent that has not
# adopted them still shows up.


@server.tool()
async def run_start(
    question: str,
    actor_agent: str = "",
    prior_run_id: str = "",
    allow_notes: bool = True,
    max_calls: int = 8,
    ctx: Context | None = None,
) -> dict:
    """Open a run in the Memex console for this connection.

    Call this when you start a task so the console can name the work and
    group every following search / read / note under it. One connection
    owns at most one running run; starting another closes the previous one.
    The run id is informational — later tool calls are attributed by the
    server-assigned session, not by passing the id around.
    """
    client = _client_name(ctx) or actor_agent or None
    async with _session() as db:
        run = await _external.start_run(
            db,
            session_id=_session_id(ctx),
            client=client,
            question=question,
            allow_notes=allow_notes,
            max_calls=max_calls,
            prior_run_id=prior_run_id or None,
        )
        await db.commit()
        return {
            "run_id": run.id,
            "question": run.question,
            "status": run.status,
            "session_id": run.session_id,
            "client": run.client,
        }


@server.tool()
async def run_end(
    status: str = "done",
    answer: str = "",
    error: str = "",
    ctx: Context | None = None,
) -> dict:
    """Close this connection's console run.

    Call it when the task is finished so the console stops showing the run
    as live and records the final answer. status is "done" (default) or
    "failed"; on failure pass error. The answer is recorded as text — the
    console cannot verify quotes from an external agent, so citations are
    left empty rather than fabricated.
    """
    if status not in ("done", "failed"):
        raise _tool_error(ValueError("status must be 'done' or 'failed'"))
    async with _session() as db:
        run = await _external.finish_run(
            db,
            session_id=_session_id(ctx),
            status=status,
            answer=answer or None,
            error=error or None,
        )
        await db.commit()
        if run is None:
            return {"error": "这个连接没有进行中的 run"}
        return {
            "run_id": run.id,
            "status": run.status,
            "answer": run.answer,
            "error": run.error,
        }


# ── lessons learned + task outcome scores ─────────────────────────────


@server.tool()
async def lesson_write(
    trigger: str,
    issue: str,
    solution: str,
    actor_agent: str,
    keywords: str = "",
    task_id: str = "",
    doc_id: str = "",
) -> dict:
    """Record a structured lesson learned so other agents don't repeat it.
    trigger: when does this apply (task/document type/section type);
    issue: what went wrong; solution: what to do instead. Write lessons
    only when a task failed or needed rework — keep the store clean."""
    async with _session() as db:
        lesson = await _activity.lesson_write(
            db, trigger, issue, solution,
            author_agent=actor_agent,
            keywords=keywords or None,
            task_id=task_id or None,
            doc_id=doc_id or None,
        )
        await db.commit()
        return _activity.lesson_out(lesson)


@server.tool()
async def lesson_search(query: str, top_k: int = 5) -> list[dict]:
    """Search lessons by trigger/keywords/issue. Call this when starting a
    task: lessons whose trigger matches your scenario are returned first."""
    async with _session() as db:
        lessons = await _activity.lesson_search(db, query, top_k=top_k)
        await db.commit()
        return [_activity.lesson_out(lesson) for lesson in lessons]


@server.tool()
async def score_write(
    task_id: str,
    dimension: str,
    value: str,
    actor_agent: str,
    comment: str = "",
) -> dict:
    """Score a finished task's outcome. dimension: e.g. hit_rate,
    correctness, rework, completeness; value: number or short verdict;
    comment: why. Scores are separate from the activity log and feed
    future relevance weighting."""
    async with _session() as db:
        score = await _activity.score_write(
            db, task_id, dimension, value,
            by=actor_agent,
            comment=comment or None,
        )
        await db.commit()
        return _activity.score_out(score)


@server.tool()
async def score_list(task_id: str = "", dimension: str = "", limit: int = 50) -> list[dict]:
    """List task outcome scores, newest first, optionally filtered."""
    async with _session() as db:
        scores = await _activity.score_list(
            db, task_id=task_id or None, dimension=dimension or None, limit=limit
        )
        return [_activity.score_out(score) for score in scores]


@server.tool()
@observed
async def add_note(
    body: str,
    kind: str = "note",
    author: str = "",
    doc_id: str = "",
    node_id: str = "",
    chunk_id: int | None = None,
    task_id: str = "",
    reply_to: int | None = None,
    ctx: Context | None = None,
) -> dict:
    """Leave a hand-off note for other agents.

    kind: "note" (what was done), "pitfall" (what went wrong),
    "handoff" (state for the next agent). "ingest" is rejected: 入库备注
    is written by the human at upload time and is read-only for agents.
    Anchor it to a document
    (and optionally node/chunk) or leave doc_id empty for the common area.
    reply_to links this note to the note id it responds to.
    Prefix the body with @author of the note you answer.

    Pass task_id to attach the note to the task you are working on. The task
    page then shows this note together with the reads, cites and errata from
    the same task, which is what makes the hand-off readable later.

    author is who you want to be known as (e.g. "claude-code", "codex").
    It is self-declared and unverified; omit it and the MCP client's own
    name is used. The server also stamps a session id, so notes from one
    connection can be grouped even when two agents share a name.
    """
    if not author.strip():
        author = _client_name(ctx) or "anonymous"
    body = (body or "").strip()
    if not body:
        raise ToolError("body 不能为空")
    if kind == "ingest":
        raise ToolError(
            "入库备注由人类在上传时填写，agent 只能读取，不能创建或修改"
        )
    async with _session() as db:
        if doc_id and await db.get(Document, doc_id) is None:
            raise ToolError(f"文档不存在：{doc_id}")
        if not task_id and reply_to is not None:
            # A reply belongs to the same work unit as the note it answers;
            # inheriting task_id keeps the thread on the task timeline
            # instead of dropping it back into the unattached common pool.
            parent = await db.get(AgentNote, reply_to)
            if parent is not None and parent.task_id:
                task_id = parent.task_id
        if task_id and await db.get(Task, task_id) is None:
            raise ToolError(f"task not found: {task_id}")
        row = AgentNote(
            author=author,
            kind=kind if kind in ("note", "pitfall", "handoff") else "note",
            body=body,
            doc_id=doc_id or None,
            node_id=node_id or None,
            chunk_id=chunk_id,
            task_id=task_id or None,
            session_id=_log_session_id(ctx),
            reply_to=reply_to,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return {
            "id": row.id,
            "author": row.author,
            "kind": row.kind,
            "body": row.body,
            "doc_id": row.doc_id,
            "node_id": row.node_id,
            "chunk_id": row.chunk_id,
            "task_id": row.task_id,
            "reply_to": row.reply_to,
            "session_id": row.session_id,
            "created_at": row.created_at,
        }


@server.tool()
async def list_notes(
    doc_id: str = "",
    task_id: str = "",
    author: str = "",
    kind: str = "",
    limit: int = 50,
) -> list[dict]:
    """Read hand-off notes, newest first. Check notes before redoing work
    another agent may have already done.

    kind="ingest" returns 入库备注 — a human's note written when the file
    was uploaded, explaining what it is and why it is in the library. It is
    read-only for agents; it is not a hand-off and cannot be edited.

    Pass ``task_id`` to read exactly the notes handed over for one task —
    the same task whose reads, cites and errata ``activity_list`` shows.
    """
    async with _session() as db:
        rows = await _activity.notes_list(
            db,
            doc_id=doc_id or None,
            task_id=task_id or None,
            author=author or None,
            kind=kind or None,
            limit=limit,
        )
        return [_activity.note_out(row) for row in rows]


@server.tool()
async def task_timeline(task_id: str, limit: int = 200) -> dict:
    """One task's merged timeline: activities *and* hand-off notes.

    ``activity_list`` only reads the append-only ``activities`` table, so a
    hand-off written with ``add_note(kind='handoff', task_id=...)`` was
    invisible there. This tool merges both sources (the same view the Web
    task page shows). Use it before starting work on a task.
    """
    async with _session() as db:
        result = await _activity.task_timeline(db, task_id, limit=limit)
    if result is None:
        raise ToolError(f"task not found: {task_id}")
    return result


@server.tool()
async def delete_note(note_id: int, author: str) -> dict:
    """Delete your own note (author must match).

    入库备注 (kind="ingest") is human-owned and cannot be deleted by agents.
    """
    from sqlalchemy import delete

    async with _session() as db:
        row = await db.get(AgentNote, note_id)
        if row is None:
            return {"deleted": 0}
        if row.kind == "ingest":
            raise ToolError("入库备注由人类维护，agent 不能删除")
        result = await db.execute(
            delete(AgentNote)
            .where(AgentNote.id == note_id, AgentNote.author == author)
        )
        await db.commit()
        return {"deleted": result.rowcount}


# ── activity layer: tasks + append-only activity log ──────────────────


@server.tool()
async def task_create(
    title: str,
    goal: str = "",
    owner_agent: str = "",
    task_id: str = "",
) -> dict:
    """Create a task agents cooperate on. Reads, cites, notes, handoffs
    and errata can attach to it, so the next agent sees what material the
    task used and what came of it. Pass task_id to use your own ID."""
    async with _session() as db:
        task = await _activity.task_create(
            db,
            title,
            goal=goal or None,
            owner_agent=owner_agent or None,
            task_id=task_id or None,
        )
        await db.commit()
        return _activity.task_out(task)


@server.tool()
async def task_update(
    task_id: str,
    status: str = "",
    owner_agent: str = "",
    goal: str = "",
) -> dict:
    """Update a task's status (open/doing/blocked/done/archived), owner or goal."""
    async with _session() as db:
        task = await _activity.task_update(
            db,
            task_id,
            status=status or None,
            owner_agent=owner_agent or None,
            goal=goal or None,
        )
        if task is None:
            raise ValueError(f"task not found: {task_id}")
        await db.commit()
        return _activity.task_out(task)


@server.tool()
async def task_list(status: str = "", owner_agent: str = "", limit: int = 20) -> list[dict]:
    """List tasks, newest activity first, optionally filtered."""
    async with _session() as db:
        tasks = await _activity.task_list(
            db, status=status, owner_agent=owner_agent, limit=limit
        )
        return [_activity.task_out(task) for task in tasks]


@server.tool()
async def activity_write(
    kind: str,
    actor_agent: str,
    content: str = "",
    task_id: str = "",
    doc_id: str = "",
    node_id: str = "",
    chunk_id: int | None = None,
    session_id: str = "",
    reply_to: int | None = None,
    accepts: bool | None = None,
    scope: str = "error",
    supersedes_doc_id: str = "",
    supersedes_node_id: str = "",
) -> dict:
    """Append an activity to the shared log. kind:
    - "cite": you used a section in your answer (anchor doc/node);
    - "note": remark on the material;
    - "handoff": state for the next agent on a task;
    - "erratum": this Toc Memex content is WRONG — anchor it, describe the
      error and the correct reading. Creates an erratum (status proposed).
      scope: "error" (mistake in text), "superseded" (legal: replaced or
      repealed by another document/clause — pass supersedes_doc_id or say
      where the replacement lives), "retracted" (academic: withdrawn,
      must not be cited without its retraction notice);
    - "review": verdict on an erratum (reply_to its activity id).
      accepts=true confirms the erratum (it will then be returned
      alongside the content in retrieval); accepts=false rejects it.
      Verify the original content yourself before accepting. You cannot
      confirm your own erratum — the reviewer must differ from the
      proposer.
    Reads are auto-logged by read_section; do not write them manually."""
    async with _session() as db:
        activity = await _activity.activity_write(
            db,
            kind,
            actor_agent,
            content=content or None,
            task_id=task_id or None,
            doc_id=doc_id or None,
            node_id=node_id or None,
            chunk_id=chunk_id,
            session_id=session_id or None,
            reply_to=reply_to,
            scope=scope,
            supersedes_doc_id=supersedes_doc_id or None,
            supersedes_node_id=supersedes_node_id or None,
        )
        erratum_result = None
        if kind == "review" and accepts is not None and reply_to is not None:
            try:
                erratum = await _activity.confirm_erratum(
                    db, reply_to, actor_agent, accepts
                )
            except ValueError as exc:
                await db.rollback()
                raise ValueError(str(exc)) from exc
            if erratum is not None:
                erratum_result = _activity.erratum_out(erratum)
        await db.commit()
        out = _activity.activity_out(activity)
        if erratum_result is not None:
            out["erratum"] = erratum_result
        return out


@server.tool()
async def activity_list(
    task_id: str = "",
    doc_id: str = "",
    node_id: str = "",
    actor_agent: str = "",
    kind: str = "",
    reply_to: int | None = None,
    since: str = "",
    limit: int = 20,
) -> list[dict]:
    """List activities, newest first. Filter by task / doc / node / agent /
    kind / reply_to / since (ISO datetime). This reads the append-only
    ``activities`` log only; hand-off notes written with add_note live in a
    separate table. Use task_timeline(task_id=...) to see both together."""
    async with _session() as db:
        activities = await _activity.activity_list(
            db,
            task_id=task_id or None,
            doc_id=doc_id or None,
            node_id=node_id or None,
            actor_agent=actor_agent or None,
            kind=kind or None,
            reply_to=reply_to,
            since=since or None,
            limit=limit,
        )
        return [_activity.activity_out(a) for a in activities]


# ── curator tool surface (ADR-0002 §5) ──
#
# These wrap ``app.curation.dispatch`` — the same implementation the curator's
# in-process loop calls. Acceptance: a propose_ops call through MCP and
# through dispatch returns byte-identical results.


async def _curation_call(doc_id: str, name: str, args: dict | None = None) -> dict:
    from .curation.dispatch import Dispatcher

    settings = get_settings()
    async with _session() as db:
        dispatcher = Dispatcher(db, settings.tocmemex_data_dir, doc_id)
        try:
            result = await dispatcher.dispatch(name, args or {})
        except Exception as exc:  # DispatchError and tool-level errors
            raise ToolError(str(exc)) from exc
        await db.commit()
    return result


@server.tool()
async def get_pages_overview(doc_id: str, start: int = 0, limit: int = 60) -> dict:
    """Survey pages cheaply: per-page block count, label mix, chars,
    whether the page is a printed TOC page, and figure count."""
    return await _curation_call(doc_id, "get_pages_overview", {"start": start, "limit": limit})


@server.tool()
async def get_page_blocks(doc_id: str, pages: list[int]) -> dict:
    """Typed OCR blocks for specific pages. ``label`` is the OCR label
    verbatim; ``bbox`` is not a font-size proxy."""
    return await _curation_call(doc_id, "get_page_blocks", {"pages": pages})


@server.tool()
async def get_page_text(doc_id: str, pages: list[int]) -> dict:
    """Plain text of specific pages, cheaper than the block stream."""
    return await _curation_call(doc_id, "get_page_text", {"pages": pages})


@server.tool()
async def get_chunks(doc_id: str, node_id: str = "", limit: int = 100, offset: int = 0) -> dict:
    """Slice list: index, node, pages, char count, 80-char preview."""
    return await _curation_call(
        doc_id, "get_chunks", {"node_id": node_id, "limit": limit, "offset": offset}
    )


@server.tool()
async def get_quality(doc_id: str = "") -> dict:
    """Quality issues (the same enumeration the quality page shows)."""
    return await _curation_call(doc_id or "", "get_quality", {})


@server.tool()
async def list_quality_signals(days: int = 7, limit: int = 20) -> dict:
    """Library-wide read-only patrol signals.

    Quality issues grouped by kind, zero-hit queries, documents searched but
    never marked used, proposals waiting for a human, recent automatic
    rollbacks and errata counts. Use it to find what deserves attention; it
    changes nothing.
    """
    return await _curation_call(
        "", "list_quality_signals", {"days": days, "limit": limit}
    )


@server.tool()
async def get_search_signals(doc_id: str, days: int = 7) -> dict:
    """Zero-hit queries that may be about this document, with current keywords
    and the shared terms. A lead for ``set_keywords``, not a verdict."""
    return await _curation_call(
        doc_id, "get_search_signals", {"days": days}
    )


@server.tool()
async def get_usage(doc_id: str) -> dict:
    """Usage aggregates: queries that surfaced this document, mark_used rate."""
    return await _curation_call(doc_id, "get_usage", {})


@server.tool()
async def get_toc_nodes(doc_id: str) -> dict:
    """Flat TOC node list: id, parent, level, number, title, pages, source."""
    return await _curation_call(doc_id, "get_toc_nodes", {})


@server.tool()
async def get_printed_toc_llm(doc_id: str, use_cache: bool = True) -> dict:
    """An LLM reading of the printed TOC, to compare against the stored one.

    Returns entries with printed page, resolved page index and level. Use it
    when the stored TOC looks wrong and you want a second opinion on what the
    document's own contents page says. Requires a configured LLM endpoint;
    without one it reports available=false rather than failing.
    """
    return await _curation_call(doc_id, "get_printed_toc_llm", {"use_cache": use_cache})


@server.tool()
async def request_toc_rebuild(
    doc_id: str, reason: str, node_id: str = ""
) -> dict:
    """File a 纲目复审 work order when the TOC structure itself is wrong.

    Writes a task (kind ``gangmu-review``) plus a hand-off note; changes no
    content. 纲目 re-derives the subtree and files a proposal for human
    approval. Use this instead of patching a structural error with set_page.
    """
    return await _curation_call(
        doc_id,
        "request_toc_rebuild",
        {"node_id": node_id, "reason": reason},
    )


@server.tool()
async def propose_ops(doc_id: str, ops: list[dict], rationale: str = "") -> dict:
    """Submit a fix program. Each op is validated; the response says
    accepted / pending_human / dropped, with a reason for every drop."""
    return await _curation_call(doc_id, "propose_ops", {"ops": ops, "rationale": rationale})


@server.tool()
async def get_proposal(doc_id: str, proposal_id: str) -> dict:
    """Read a proposal's verdict and per-op outcomes."""
    return await _curation_call(doc_id, "get_proposal", {"proposal_id": proposal_id})


class _SessionCtx:
    """Shared engine across tool calls (unlike _with_db, keeps one engine)."""

    def __init__(self) -> None:
        self._engine = None

    async def __aenter__(self):
        bridged = _bridge()
        if bridged is not None:
            # The console owns this session; do not close or commit it here.
            self._engine = None
            self._session = bridged.session
            return self._session
        settings = get_settings()
        self._engine = make_engine(settings.tocmemex_db_path)
        await ensure_schema(self._engine)
        self._session = session_factory(self._engine)()
        return self._session

    async def __aexit__(self, *exc):
        if self._engine is None:
            return
        await self._session.close()
        await self._engine.dispose()


def _session() -> _SessionCtx:
    return _SessionCtx()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="toc-memex-mcp")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args(argv)

    if args.transport != "stdio":
        enforce_bind_host(args.host)
        server.run(
            "streamable-http", host=args.host, port=args.port
        )
    else:
        server.run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())