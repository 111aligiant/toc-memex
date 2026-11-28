"""Durable ingest jobs: one row per ingest, shared by Web, CLI and MCP.

Why this exists
---------------
The web upload endpoint used to keep progress in ``app.state.uploads`` — a
plain dict, gone on restart while the document itself could be ``ready``.
The MCP server had no ingest entry point at all, so an agent had to reach
the REST API or the CLI, losing its identity: every 入库备注 was written
with ``author='human'``.

This module replaces the memory dict with an ``ingest_jobs`` table. A child
process (``python -m app.cli ingest --job-id ...``) does the parsing *and*
the post-processing (collection, ingest note, near-duplicate hint) and
writes the job's terminal state itself. That matters because an MCP client
connects per call: if the parent process dies, the job must still finish.

Concurrency
-----------
A job is claimed with a conditional ``UPDATE ... WHERE status='queued'``,
so the cap in ``TOCMEMEX_INGEST_CONCURRENCY`` holds across every process
sharing the database. When a child finishes it calls ``pump`` again, so the
queue drains as a chain of detached children rather than needing a daemon.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ..config import get_settings
from ..db import session_factory
from ..models import Document, IngestJob, utcnow_iso

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("queued", "parsing", "indexing")
TERMINAL_STATUSES = frozenset({"ready", "failed", "duplicate"})
DEFAULT_NAMESPACE = "shared"

# Repository root: children run with this as cwd so ``-m app.cli`` and the
# project ``.env`` resolve exactly as they do for the web server.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# ── path allowlist ────────────────────────────────────────────────────────


def allowed_roots(data_dir: Path, extra_roots: Sequence[Path] = ()) -> list[Path]:
    """Directories an ingest may read from: the data dir plus opt-ins."""
    roots = [Path(data_dir)]
    roots.extend(Path(root) for root in extra_roots)
    resolved: list[Path] = []
    for root in roots:
        try:
            resolved.append(root.expanduser().resolve())
        except OSError:
            continue
    return resolved


def resolve_ingest_path(
    raw: str | Path,
    *,
    data_dir: Path,
    extra_roots: Sequence[Path] = (),
    db_path: Path | None = None,
) -> Path:
    """Resolve a caller-supplied path and refuse anything outside the roots.

    ``strict=True`` follows symlinks/junctions first, so an escape is
    rejected after resolution rather than being hidden by it. The database
    file itself is refused: ingesting it would be nonsense and could lock
    the library.
    """
    try:
        path = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"路径不存在或不可访问：{raw}") from exc
    if not path.is_file():
        raise ValueError(f"不是一个文件：{raw}")
    if db_path is not None:
        try:
            if path == Path(db_path).expanduser().resolve():
                raise ValueError("不能入库数据库文件本身")
        except OSError:
            pass
    roots = allowed_roots(data_dir, extra_roots)
    if not any(path.is_relative_to(root) for root in roots):
        raise ValueError(
            "路径不在允许的入库目录内；可入库的根目录："
            + "、".join(str(root) for root in roots)
        )
    return path


# ── hashing ───────────────────────────────────────────────────────────────


# ── serialization ─────────────────────────────────────────────────────────


def _similar_list(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def job_out(row: IngestJob, doc: Document | None = None) -> dict[str, Any]:
    """Canonical snake_case job payload, with the live document overlaid.

    While a job is active the document row is the freshest source for
    status/stage/progress, so a reader sees the same words in ``/api/jobs``,
    ``/api/uploads/{id}`` and ``get_ingest``.
    """
    status = row.status
    stage = row.stage
    progress = row.progress
    error = row.error
    doc_id = row.doc_id
    if doc is not None:
        doc_id = doc.id
        if status in ACTIVE_STATUSES and doc.status in (
            "parsing",
            "indexing",
            "ready",
            "failed",
        ):
            status = doc.status
            stage = doc.stage or stage
            progress = doc.progress if doc.progress is not None else progress
            if doc.status == "failed" and doc.error:
                error = doc.error
    similar = _similar_list(row.similar_json)
    return {
        "id": row.id,
        "filename": row.filename,
        "file_path": row.file_path,
        "size": row.size,
        "status": status,
        "stage": stage,
        "progress": progress,
        "collection_id": row.collection_id,
        "image_mode": row.image_mode,
        "ingest_note": row.ingest_note or "",
        "namespace": row.namespace,
        "title": row.title,
        "doc_type": row.doc_type,
        "doc_date": row.doc_date,
        "doc_id": doc_id,
        "content_hash": row.content_hash,
        "actor": row.actor,
        "session_id": row.session_id,
        "source": row.source,
        "error": error,
        "message": row.message or "",
        "similar_documents": similar,
        "duplicate_hint": bool(similar and any(s.get("strong") for s in similar)),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def upload_record(job: dict[str, Any]) -> dict[str, Any]:
    """Map a canonical job payload onto the web upload record's camelCase.

    Only adds keys, never removes: existing clients and tests keep working
    while the record gains actor/similar fields.
    """
    return {
        "id": job["id"],
        "filename": job["filename"],
        "filePath": job["file_path"],
        "size": job["size"],
        "status": job["status"],
        "stage": job["stage"],
        "progress": job["progress"],
        "collectionId": job["collection_id"],
        "imageMode": job["image_mode"],
        "ingestNote": job["ingest_note"],
        "docId": job["doc_id"],
        "contentHash": job["content_hash"],
        "actor": job["actor"],
        "sessionId": job["session_id"],
        "source": job["source"],
        "error": job["error"],
        "message": job["message"],
        "similarDocuments": job["similar_documents"],
        "duplicateHint": job["duplicate_hint"],
        "createdAt": job["created_at"],
        "updatedAt": job["updated_at"],
    }


# ── lifecycle ─────────────────────────────────────────────────────────────


async def create_job(
    engine: AsyncEngine,
    *,
    file_path: Path,
    filename: str | None = None,
    size: int | None = None,
    collection_id: str | None = None,
    image_mode: str = "structured",
    ingest_note: str = "",
    actor: str = "human",
    session_id: str | None = None,
    source: str = "web",
    namespace: str = DEFAULT_NAMESPACE,
    title: str | None = None,
    doc_type: str | None = None,
    doc_date: str | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Insert a queued job. Does not start anything; call ``pump`` next."""
    path = Path(file_path)
    message: str | None = None
    resolved_collection = collection_id or None
    if resolved_collection:
        # A stale or mistyped collection id must not fail the ingest: the
        # document is still worth having. Drop the filing, say so, ingest.
        from ..models import Collection

        async with session_factory(engine)() as db:
            if await db.get(Collection, resolved_collection) is None:
                message = f"分区不存在：{resolved_collection}"
                resolved_collection = None
    row = IngestJob(
        id=job_id or None,
        filename=filename or path.name,
        file_path=str(path),
        size=int(size if size is not None else (path.stat().st_size if path.exists() else 0)),
        status="queued",
        stage="排队等待解析",
        progress=0,
        collection_id=resolved_collection,
        image_mode=image_mode or "structured",
        ingest_note=(ingest_note or "").strip() or None,
        namespace=namespace or DEFAULT_NAMESPACE,
        title=title or None,
        doc_type=doc_type or None,
        doc_date=doc_date or None,
        actor=actor or "human",
        session_id=session_id or None,
        source=source or "web",
        message=message,
    )
    async with session_factory(engine)() as db:
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return job_out(row)


async def _document_for(
    db: AsyncSession, job: IngestJob, *, active_only: bool = False
) -> Document | None:
    """The document this job is building, if its row exists yet.

    The child creates the document row before parsing, but the job's
    ``doc_id`` is only written at the end. Matching by ``file_path`` in the
    meantime lets a poll show the live stage/progress instead of a frozen
    "准备解析".

    ``active_only`` excludes already-ready documents: a *queued* duplicate
    job must not borrow the existing document's ``ready`` status before the
    child has decided that it is a duplicate.
    """
    if job.doc_id:
        doc = await db.get(Document, job.doc_id)
        if doc is not None:
            return doc
    if job.content_hash:
        doc = (
            await db.execute(
                select(Document).where(
                    Document.namespace_id == job.namespace,
                    Document.content_hash == job.content_hash,
                )
            )
        ).scalar_one_or_none()
        if doc is not None:
            return doc
    if job.file_path:
        stmt = select(Document).where(
            Document.namespace_id == job.namespace,
            Document.file_path == job.file_path,
        )
        if active_only:
            stmt = stmt.where(Document.status.in_(("parsing", "indexing")))
        return (
            await db.execute(
                stmt.order_by(Document.created_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
    return None


async def get_job(
    engine: AsyncEngine, job_id: str
) -> dict[str, Any] | None:
    async with session_factory(engine)() as db:
        row = await db.get(IngestJob, job_id)
        if row is None:
            return None
        doc = await _document_for(db, row, active_only=True)
        return job_out(row, doc)


async def list_jobs(
    engine: AsyncEngine,
    *,
    statuses: Sequence[str] = ACTIVE_STATUSES,
    limit: int = 50,
) -> list[dict[str, Any]]:
    async with session_factory(engine)() as db:
        stmt = (
            select(IngestJob)
            .where(IngestJob.status.in_(tuple(statuses)))
            .order_by(IngestJob.created_at.desc())
            .limit(max(1, limit))
        )
        rows = (await db.execute(stmt)).scalars().all()
        return [job_out(row) for row in rows]


# ── claiming and spawning ─────────────────────────────────────────────────


def _concurrency() -> int:
    try:
        value = int(get_settings().tocmemex_ingest_concurrency)
    except Exception:
        value = 2
    return max(1, value)


async def claim(engine: AsyncEngine, limit: int | None = None) -> list[IngestJob]:
    """Move up to *limit* queued jobs to ``parsing``, atomically per job.

    The capacity check and the per-row conditional UPDATE share one
    transaction, and the UPDATE only succeeds while the row is still
    ``queued``. Two processes calling ``claim`` at once therefore cannot
    claim the same row, and the sum of their claims cannot exceed the cap.
    """
    cap = _concurrency()
    take = cap if limit is None else max(0, min(limit, cap))
    if take <= 0:
        return []
    claimed: list[IngestJob] = []
    async with session_factory(engine)() as db:
        active = (
            await db.execute(
                select(text("COUNT(*)"))
                .select_from(IngestJob)
                .where(IngestJob.status.in_(("parsing", "indexing")))
            )
        ).scalar() or 0
        free = cap - int(active)
        if free <= 0:
            return []
        candidates = (
            (
                await db.execute(
                    select(IngestJob)
                    .where(IngestJob.status == "queued")
                    .order_by(IngestJob.created_at.asc())
                    .limit(min(take, free))
                )
            )
            .scalars()
            .all()
        )
        for candidate in candidates:
            now = utcnow_iso()
            result = await db.execute(
                update(IngestJob)
                .where(IngestJob.id == candidate.id, IngestJob.status == "queued")
                .values(status="parsing", stage="准备解析", progress=5, heartbeat_at=now)
            )
            if result.rowcount == 1:
                claimed.append(candidate)
        await db.commit()
        for row in claimed:
            await db.refresh(row)
    return claimed


def _popen_flags() -> dict[str, Any]:
    """Make the child independent of this process, on both platforms.

    ``asyncio`` subprocess transports terminate their child when the event
    loop closes, which killed the next link of the self-pump chain (the
    spawning child exits right after pumping). A plain ``Popen`` with the
    process detached has no such tie.
    """
    if os.name == "nt":
        return {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
        }
    return {"start_new_session": True}


def child_argv(job: IngestJob) -> list[str]:
    argv = [
        sys.executable,
        "-m",
        "app.cli",
        "ingest",
        job.file_path,
        "--job-id",
        job.id,
        "--image-mode",
        job.image_mode or "structured",
        "--namespace",
        job.namespace or DEFAULT_NAMESPACE,
        "--actor",
        job.actor or "human",
    ]
    if job.title:
        argv += ["--title", job.title]
    if job.doc_type:
        argv += ["--type", job.doc_type]
    if job.doc_date:
        argv += ["--date", job.doc_date]
    return argv


def child_env(db_path: Path | None = None, data_dir: Path | None = None) -> dict[str, str]:
    """Environment for the child: the same DB and data dir this process uses.

    Callers that were started with an explicit database (``toc-memex-web
    --db``, or a test app) must pass it in; falling back to settings would
    make the child parse into a different library.
    """
    settings = get_settings()
    db = Path(db_path) if db_path is not None else Path(settings.tocmemex_db_path)
    data = (
        Path(data_dir) if data_dir is not None else Path(settings.tocmemex_data_dir)
    )
    env = os.environ.copy()
    env["TOCMEMEX_DB_PATH"] = str(db.resolve())
    env["TOCMEMEX_DATA_DIR"] = str(data.resolve())
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _spawn_sync(
    job: IngestJob,
    db_path: Path | None = None,
    data_dir: Path | None = None,
) -> int | None:
    process = subprocess.Popen(
        child_argv(job),
        cwd=str(_PROJECT_ROOT),
        env=child_env(db_path, data_dir),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **_popen_flags(),
    )
    return process.pid


async def _spawn(
    job: IngestJob,
    db_path: Path | None = None,
    data_dir: Path | None = None,
) -> int | None:
    # Popen blocks for a few milliseconds; keep it off the event loop.
    return await asyncio.to_thread(_spawn_sync, job, db_path, data_dir)


async def pump(
    engine: AsyncEngine,
    *,
    db_path: Path | None = None,
    data_dir: Path | None = None,
) -> int:
    """Claim queued jobs and start their children. Returns how many started.

    Idempotent and safe to call from any process. Children call it again
    when they finish, which is what drains a queue longer than the cap.
    ``db_path``/``data_dir`` are the library the children must use; omit
    them only when the process-wide settings are the right ones.
    """
    claimed = await claim(engine)
    started = 0
    for job in claimed:
        try:
            pid = await _spawn(job, db_path, data_dir)
        except Exception as exc:  # spawn failure must not strand the row
            logger.exception("could not spawn ingest child for %s", job.id)
            async with session_factory(engine)() as db:
                await db.execute(
                    update(IngestJob)
                    .where(IngestJob.id == job.id, IngestJob.status == "parsing")
                    .values(
                        status="failed",
                        error=f"无法启动解析进程：{exc}",
                        finished_at=utcnow_iso(),
                    )
                )
                await db.commit()
            continue
        started += 1
        if pid is not None:
            async with session_factory(engine)() as db:
                await db.execute(
                    update(IngestJob)
                    .where(IngestJob.id == job.id)
                    .values(child_pid=pid, updated_at=utcnow_iso())
                )
                await db.commit()
    return started


async def heartbeat(engine: AsyncEngine, job_id: str) -> None:
    """Refresh a job's heartbeat; best effort (lock contention is fine)."""
    try:
        async with session_factory(engine)() as db:
            await db.execute(
                update(IngestJob)
                .where(IngestJob.id == job_id, IngestJob.status.in_(("parsing", "indexing")))
                .values(heartbeat_at=utcnow_iso())
            )
            await db.commit()
    except Exception:
        logger.debug("heartbeat write skipped for %s", job_id, exc_info=True)


async def finish_job(
    engine: AsyncEngine,
    job_id: str,
    *,
    status: str,
    doc_id: str | None = None,
    error: str | None = None,
    message: str | None = None,
    similar: list[dict] | None = None,
    post_processed: bool = False,
) -> None:
    """Write a job's terminal state. Only an active job is updated.

    The conditional WHERE keeps a late writer from overwriting a job that
    another process already reconciled to a terminal state.
    """
    now = utcnow_iso()
    values: dict[str, Any] = {
        "status": status,
        "finished_at": now,
        "updated_at": now,
        "stage": "完成" if status == "ready" else ("解析失败" if status == "failed" else "已存在"),
        "progress": 100,
    }
    if status == "ready":
        values["stage"] = "完成"
    if doc_id is not None:
        values["doc_id"] = doc_id
    if error is not None:
        values["error"] = error[:4000]
    if message is not None:
        values["message"] = message
    if similar is not None:
        values["similar_json"] = json.dumps(similar, ensure_ascii=False) if similar else None
    if post_processed:
        values["post_processed_at"] = now
    async with session_factory(engine)() as db:
        await db.execute(
            update(IngestJob)
            .where(IngestJob.id == job_id, IngestJob.status.in_(ACTIVE_STATUSES))
            .values(**values)
        )
        await db.commit()


# ── restart recovery ──────────────────────────────────────────────────────


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    from ..curation.lock import _pid_alive as probe

    try:
        return bool(probe(int(pid)))
    except (OSError, ValueError):
        return False


def _stale(heartbeat_at: str | None, lease_s: float) -> bool:
    if not heartbeat_at:
        return True
    from datetime import datetime, timezone

    try:
        seen = datetime.fromisoformat(heartbeat_at)
    except ValueError:
        return True
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - seen).total_seconds() > lease_s


async def reconcile_jobs(
    engine: AsyncEngine,
    *,
    db_path: Path | None = None,
    data_dir: Path | None = None,
) -> dict[str, int]:
    """Bring jobs left active by a previous process to a truthful state.

    Never re-runs an already-claimed job and never spawns anything: that is
    the only way two servers sharing one database cannot double-parse. A job
    that was queued but never claimed is left queued for ``pump``.
    """
    settings = get_settings()
    lease = float(settings.tocmemex_ingest_lease_s)
    counts = {"checked": 0, "recovered": 0, "failed": 0, "alive": 0, "queued": 0}
    async with session_factory(engine)() as db:
        rows = (
            (
                await db.execute(
                    select(IngestJob).where(IngestJob.status.in_(ACTIVE_STATUSES))
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            counts["checked"] += 1
            doc = await _document_for(db, row)
            if doc is not None and doc.status == "ready":
                row.status = "ready"
                row.stage = doc.stage or "完成"
                row.progress = 100
                row.doc_id = doc.id
                row.finished_at = row.finished_at or utcnow_iso()
                counts["recovered"] += 1
                continue
            if doc is not None and doc.status == "failed":
                row.status = "failed"
                row.error = doc.error or row.error
                row.doc_id = doc.id
                row.finished_at = row.finished_at or utcnow_iso()
                counts["failed"] += 1
                continue
            if row.status == "queued" and row.child_pid is None:
                counts["queued"] += 1
                continue
            # Alive if the child is still heartbeating (covers the brief
            # window between claim and spawn, when child_pid is not yet
            # written) or its pid still exists. A dead child stops
            # heartbeating, so the lease turns it into "failed" within
            # ``lease`` seconds.
            if not _stale(row.heartbeat_at, lease) or _pid_alive(row.child_pid):
                counts["alive"] += 1
                continue
            row.status = "failed"
            row.error = (
                row.error
                or "服务重启或解析进程已退出，任务可能已完成；"
                "可按 content_hash 在文档列表里检索结果"
            )
            row.finished_at = utcnow_iso()
            if doc is not None and doc.status not in ("ready", "failed"):
                doc.status = "failed"
                doc.stage = "解析失败"
                doc.error = row.error[:1000]
            counts["failed"] += 1
        await db.commit()
    await pump(engine, db_path=db_path, data_dir=data_dir)
    return counts
