"""Ingest provenance: what a document was built from, and by which components.

Every ingest and reindex appends one ``ingest_runs`` row. The newest row is
the document's current provenance; older rows are history. The web API
reads these rows instead of guessing from the filesystem, so the UI can
answer "which OCR did this use — or did it skip OCR?", "did an LLM build
the TOC, and did it win?", and "is the original file still there?".

Rows written for documents ingested before this table existed are marked
``recorded_by = 'backfill'``: they carry only what the filesystem and the
stored TOC can prove. Anything unprovable stays NULL — a backfilled row
never claims a provider or model it cannot show evidence for.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Document, IngestRun, utcnow_iso

# parse_channel -> human-facing OCR answer used when no run row exists yet.
_TEXT_CHANNELS = {"pdf-text", "docx-direct", "markdown", "xlsx"}


def _latest_toc_source(nodes: list) -> str | None:
    """The channel that owns the stored TOC: most nodes' source."""
    counts: dict[str, int] = {}
    for node in nodes:
        source = getattr(node, "source", None)
        if source:
            counts[source] = counts.get(source, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda item: item[1])[0]


def build_run(
    doc: Document,
    *,
    recorded_by: str = "ingest",
    parse: dict | None = None,
    toc_trace: dict | None = None,
    nodes: list | None = None,
    chunk_count: int | None = None,
    ocr_endpoint_name: str | None = None,
    ocr_elapsed_ms: int | None = None,
    ocr_cache: str | None = None,
    status: str = "ok",
    error: str | None = None,
) -> IngestRun:
    """Assemble an ``IngestRun`` row from parse/OCR/TOC evidence."""
    parse = parse or {}
    trace = toc_trace or {}
    path = Path(doc.file_path) if doc.file_path else None
    file_exists = bool(path and path.exists())
    try:
        file_size = path.stat().st_size if file_exists else None
    except OSError:
        file_exists, file_size = False, None

    ocr_used = bool(parse.get("ocr_used"))
    garbled = parse.get("garbled_pages") or []
    return IngestRun(
        doc_id=doc.id,
        recorded_by=recorded_by,
        file_path=doc.file_path,
        file_size=file_size,
        file_exists=int(file_exists),
        content_hash=doc.content_hash,
        parse_channel=parse.get("parse_channel"),
        ocr_used=int(ocr_used),
        ocr_provider=parse.get("ocr_provider"),
        ocr_model=parse.get("ocr_model"),
        ocr_endpoint_name=ocr_endpoint_name if ocr_used else None,
        ocr_job_id=parse.get("ocr_job_id"),
        ocr_elapsed_ms=ocr_elapsed_ms if ocr_used else None,
        ocr_cache=ocr_cache,
        llm_used=int(bool(trace.get("llm_used"))),
        llm_endpoint_name=trace.get("llm_endpoint_name"),
        llm_model=trace.get("llm_model"),
        llm_outcome=trace.get("llm_outcome"),
        llm_elapsed_ms=trace.get("llm_elapsed_ms"),
        llm_error=trace.get("llm_error"),
        toc_source=trace.get("channel") or _latest_toc_source(nodes or []),
        node_count=len(nodes) if nodes is not None else None,
        chunk_count=chunk_count,
        bookmark_count=parse.get("bookmark_count"),
        garbled_pages=json.dumps(sorted(garbled)) if garbled else None,
        status=status,
        error=error,
        created_at=utcnow_iso(),
    )


async def record_run(db: AsyncSession, run: IngestRun) -> IngestRun:
    """Append one provenance row (caller owns the transaction)."""
    db.add(run)
    await db.flush()
    return run


def run_out(row: IngestRun) -> dict:
    """JSON shape for the web API."""
    return {
        "id": row.id,
        "doc_id": row.doc_id,
        "recorded_by": row.recorded_by,
        "file": {
            "path": row.file_path,
            "size": row.file_size,
            "exists": bool(row.file_exists),
            "content_hash": row.content_hash,
        },
        "parse_channel": row.parse_channel,
        "ocr": {
            "used": bool(row.ocr_used),
            "provider": row.ocr_provider,
            "model": row.ocr_model,
            "endpoint": row.ocr_endpoint_name,
            "job_id": row.ocr_job_id,
            "elapsed_ms": row.ocr_elapsed_ms,
            "cache": row.ocr_cache,
        },
        "llm": {
            "used": bool(row.llm_used),
            "endpoint": row.llm_endpoint_name,
            "model": row.llm_model,
            "outcome": row.llm_outcome,
            "elapsed_ms": row.llm_elapsed_ms,
            "error": row.llm_error,
        },
        "toc_source": row.toc_source,
        "node_count": row.node_count,
        "chunk_count": row.chunk_count,
        "bookmark_count": row.bookmark_count,
        "garbled_pages": (
            json.loads(row.garbled_pages) if row.garbled_pages else []
        ),
        "status": row.status,
        "error": row.error,
        "created_at": row.created_at,
    }


def provenance_summary(row: IngestRun | None) -> dict:
    """Compact, list-friendly view used by /api/documents.

    ``ocr_label`` is the answer the archive wants at a glance: the provider
    when OCR ran, otherwise the text channel that was used instead.
    """
    if row is None:
        return {
            "ocr_label": None,
            "ocr_used": None,
            "llm_used": None,
            "llm_outcome": None,
            "file_exists": None,
            "recorded_by": None,
            "toc_source": None,
            "bookmark_count": None,
            "garbled_pages": [],
        }
    if row.ocr_used:
        ocr_label = row.ocr_provider or "ocr"
        if row.ocr_model:
            ocr_label = f"{ocr_label} · {row.ocr_model}"
    else:
        # Short enough for a table cell; the document page spells it out.
        ocr_label = {
            "pdf-text": "文本层",
            "docx-direct": "Word 直读",
            "markdown": "Markdown",
            "xlsx": "Excel",
        }.get(row.parse_channel or "", row.parse_channel)
    raw_garbled = getattr(row, "garbled_pages", None)
    if isinstance(raw_garbled, str) and raw_garbled:
        try:
            garbled_pages = json.loads(raw_garbled)
        except ValueError:
            garbled_pages = []
    elif isinstance(raw_garbled, list):
        garbled_pages = raw_garbled
    else:
        garbled_pages = []
    return {
        "ocr_label": ocr_label,
        "ocr_used": bool(row.ocr_used),
        "llm_used": bool(row.llm_used),
        "llm_outcome": row.llm_outcome,
        "file_exists": bool(row.file_exists),
        "recorded_by": row.recorded_by,
        "toc_source": row.toc_source,
        "bookmark_count": getattr(row, "bookmark_count", None),
        "garbled_pages": garbled_pages,
    }


async def backfill_runs(
    db: AsyncSession, settings, *, overwrite: bool = False
) -> dict:
    """Write ``backfill`` provenance rows for documents that have none.

    Evidence used, in order of strength:

    - ``pages.json`` ``_meta``: provider / model / job_id (exact);
    - legacy Paddle ``raw.jsonl``: proves Paddle ran, model unknown;
    - neither: the document was parsed from its text layer (PDF text layer,
      Word direct read, Markdown, Excel);
    - ``toc_nodes.source``: the channel that owns the stored TOC;
    - the original file: path, size, existence.

    LLM provenance is NOT reconstructable — ``data/llm`` caches do not
    record which endpoint produced them — so those columns stay NULL and
    ``llm_outcome`` is left unset rather than guessed.
    """
    from sqlalchemy import func, select

    from ..models import Document, IngestRun, TocNode
    from ..ocr.cache import load_pages

    if not overwrite:
        have = set(
            (
                await db.execute(select(IngestRun.doc_id).distinct())
            ).scalars().all()
        )
    else:
        have = set()

    docs = (await db.execute(select(Document))).scalars().all()
    written = 0
    skipped = 0
    for doc in docs:
        if doc.id in have:
            skipped += 1
            continue

        cache_dir = settings.ocr_dir / doc.id
        cached = load_pages(cache_dir)
        if cached is not None:
            meta = cached.metadata or {}
            parse = {
                "parse_channel": "ocr",
                "ocr_used": True,
                "ocr_provider": meta.get("ocr_provider"),
                "ocr_model": meta.get("ocr_model"),
                "ocr_job_id": meta.get("job_id"),
            }
            ocr_cache = "pages.json"
        elif (cache_dir / "raw.jsonl").exists():
            parse = {
                "parse_channel": "ocr",
                "ocr_used": True,
                "ocr_provider": "paddle",
                "ocr_model": None,
                "ocr_job_id": None,
            }
            ocr_cache = "raw.jsonl"
        else:
            parse = {
                "parse_channel": _text_channel_for(doc),
                "ocr_used": False,
                "ocr_provider": None,
                "ocr_model": None,
                "ocr_job_id": None,
            }
            ocr_cache = None

        sources = (
            await db.execute(
                select(TocNode.source, func.count())
                .where(TocNode.doc_id == doc.id)
                .group_by(TocNode.source)
            )
        ).all()
        toc_source = (
            max(sources, key=lambda item: item[1])[0] if sources else None
        )
        node_count = sum(count for _, count in sources)

        run = build_run(
            doc,
            recorded_by="backfill",
            parse=parse,
            toc_trace={"channel": toc_source},
            ocr_cache=ocr_cache,
            status="ok" if doc.status == "ready" else doc.status,
        )
        run.node_count = node_count
        db.add(run)
        written += 1

    await db.commit()
    return {"written": written, "skipped": skipped}


def _text_channel_for(doc: Document) -> str:
    suffix = Path(doc.filename or doc.file_path or "").suffix.lower()
    if suffix in {".docx", ".doc"}:
        return "docx-direct"
    if suffix in {".md", ".markdown", ".txt"}:
        return "markdown"
    if suffix in {".xlsx", ".xls"}:
        return "xlsx"
    return "pdf-text"
