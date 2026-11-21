"""Read tools for the curator and any MCP client (ADR-0002 §5.2).

Primitives, not scenarios: no ``get_digest_for_toc_fixing``. The agent
decides which pages to read and how deep. Each response carries a
``budget`` block so the caller knows how many calls it has left.

``get_page_blocks`` returns typed blocks with the OCR ``label`` intact and
never returns markdown: markdown loses the label, and a label is the only
honest signal for "this is a heading" — bbox height does not separate
headings from body text in this corpus (ADR-0002 §1.3).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Chunk, Document, TocNode
from ..ocr.cache import OcrCacheError, load_cached_pages
from ..ocr.parser import _html_to_text, page_text

_BLOCK_PREVIEW = 1200


class ToolError(ValueError):
    """A tool call that cannot be answered (bad doc, bad page, …)."""


def _budget(calls_left: int | None, tokens_left: int | None) -> dict:
    return {"calls_left": calls_left, "tokens_left": tokens_left}


async def _document(db: AsyncSession, doc_id: str) -> Document:
    doc = await db.get(Document, doc_id)
    if doc is None:
        raise ToolError(f"document not found: {doc_id}")
    return doc


def _cached_pages(data_dir, doc_id: str):
    try:
        return load_cached_pages(data_dir / "ocr" / doc_id) or []
    except OcrCacheError:
        return []


async def get_pages_overview(
    db: AsyncSession, data_dir, doc_id: str, start: int = 0, limit: int = 60, **budget
) -> dict:
    """One line per page: block count, label mix, chars, TOC-page flag, figures.

    ~40 characters per page, so an agent can survey a 500-page document for
    the price of a couple of pages of text.
    """
    doc = await _document(db, doc_id)
    pages = _cached_pages(data_dir, doc_id)
    from ..ingest.printed_toc import toc_page_indices

    toc_pages = set(toc_page_indices(pages)) if pages else set()
    window = [page for page in pages if start <= page.page_index < start + limit]
    rows = []
    for page in window:
        labels: dict[str, int] = {}
        for block in page.blocks:
            labels[block.label] = labels.get(block.label, 0) + 1
        rows.append(
            {
                "page": page.page_index,
                "blocks": len(page.blocks),
                "labels": labels,
                "chars": len(page_text(page)),
                "toc_page": page.page_index in toc_pages,
                "figures": len(page.images or {}),
            }
        )
    return {
        "doc_id": doc_id,
        "page_count": doc.page_count,
        "pages": rows,
        "budget": _budget(budget.get("calls_left"), budget.get("tokens_left")),
    }


async def get_page_blocks(
    db: AsyncSession, data_dir, doc_id: str, pages: list[int], **budget
) -> dict:
    """Typed block stream for the requested pages.

    ``label`` is the OCR label verbatim (paragraph_title / text / number /
    table / image / header / footer …). ``bbox`` is given as-is and is *not*
    a font-size proxy. ``order`` is reading order and may be null.
    """
    await _document(db, doc_id)
    cached = _cached_pages(data_dir, doc_id)
    by_index = {page.page_index: page for page in cached}
    missing = [page for page in pages if page not in by_index]
    out = []
    for page_index in pages:
        page = by_index.get(page_index)
        if page is None:
            continue
        blocks = []
        ordered = sorted(
            page.blocks, key=lambda block: (block.order is None, block.order or 0)
        )
        for position, block in enumerate(ordered):
            blocks.append(
                {
                    "i": position,
                    "label": block.label,
                    "text": _html_to_text(block.content)[:_BLOCK_PREVIEW],
                    "bbox": list(block.bbox),
                    "order": block.order,
                }
            )
        out.append(
            {
                "page": page.page_index,
                "width": page.width,
                "height": page.height,
                "blocks": blocks,
            }
        )
    return {
        "doc_id": doc_id,
        "pages": out,
        "missing_pages": missing,
        "note": "bbox is not a font-size proxy; use label",
        "budget": _budget(budget.get("calls_left"), budget.get("tokens_left")),
    }


async def get_page_text(
    db: AsyncSession, data_dir, doc_id: str, pages: list[int], **budget
) -> dict:
    """Plain text for the requested pages, cheaper than the block stream."""
    await _document(db, doc_id)
    cached = _cached_pages(data_dir, doc_id)
    by_index = {page.page_index: page for page in cached}
    return {
        "doc_id": doc_id,
        "pages": [
            {"page": page, "text": page_text(by_index[page])}
            for page in pages
            if page in by_index
        ],
        "missing_pages": [page for page in pages if page not in by_index],
        "budget": _budget(budget.get("calls_left"), budget.get("tokens_left")),
    }


async def get_chunks(
    db: AsyncSession,
    data_dir,
    doc_id: str,
    node_id: str = "",
    limit: int = 100,
    offset: int = 0,
    **budget,
) -> dict:
    """Slice list with index / node / pages / size / first 80 characters."""
    await _document(db, doc_id)
    stmt = select(Chunk).where(Chunk.doc_id == doc_id)
    if node_id:
        stmt = stmt.where(Chunk.node_id == node_id)
    stmt = stmt.order_by(Chunk.chunk_index).offset(offset).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    total = len(
        (await db.execute(select(Chunk.id).where(Chunk.doc_id == doc_id))).scalars().all()
    )
    return {
        "doc_id": doc_id,
        "total": total,
        "chunks": [
            {
                "id": chunk.id,
                "index": chunk.chunk_index,
                "node_id": chunk.node_id,
                "pages": [chunk.page_start, chunk.page_end],
                "chars": len(chunk.content or ""),
                "preview": (chunk.content or "")[:80],
            }
            for chunk in rows
        ],
        "budget": _budget(budget.get("calls_left"), budget.get("tokens_left")),
    }


async def get_quality(db: AsyncSession, data_dir, doc_id: str = "", **budget) -> dict:
    """The same issue enumeration the quality page shows."""
    from pathlib import Path

    from sqlalchemy import text as sql_text

    from . import issues as issues_mod

    # The issue helpers read SQLite directly; the session's engine knows the file.
    path = Path((await db.execute(sql_text("PRAGMA database_list"))).first()[2])
    groups = issues_mod.quality_groups(path, data_dir)
    if doc_id:
        groups = [group for group in groups if group["docId"] == doc_id]
    return {
        "doc_id": doc_id or None,
        "groups": groups,
        "count": len(groups),
        "budget": _budget(budget.get("calls_left"), budget.get("tokens_left")),
    }


async def list_quality_signals(
    db: AsyncSession,
    data_dir,
    doc_id: str = "",
    days: int = 7,
    limit: int = 20,
    **budget,
) -> dict:
    """Library-wide patrol signals, read-only (self-evolution §5.4.1).

    Quality issues by kind, zero-hit queries, documents searched but never
    marked used, proposals waiting for a human, recent automatic rollbacks
    and errata counts. The agent uses this to name a problem worth a new job
    kind; it changes nothing.
    """
    from .signals import collect_signals

    result = await collect_signals(db, data_dir, days=days, limit=limit)
    result["budget"] = _budget(budget.get("calls_left"), budget.get("tokens_left"))
    return result


async def get_usage(db: AsyncSession, data_dir, doc_id: str, **budget) -> dict:
    """Usage aggregates for one document: queries, hits, mark_used rate."""
    from sqlalchemy import text as sql_text

    rows = (
        await db.execute(
            sql_text(
                """
                SELECT query, node_id, chunk_id, used, created_at
                FROM usage_log WHERE doc_id = :doc ORDER BY id DESC LIMIT 200
                """
            ),
            {"doc": doc_id},
        )
    ).all()
    by_query: dict[str, dict] = {}
    for query, node_id, chunk_id, used, created_at in rows:
        bucket = by_query.setdefault(
            query, {"query": query, "hits": 0, "used": 0, "last": created_at}
        )
        bucket["hits"] += 1
        bucket["used"] += 1 if used else 0
    return {
        "doc_id": doc_id,
        "queries": sorted(by_query.values(), key=lambda item: item["last"], reverse=True),
        "budget": _budget(budget.get("calls_left"), budget.get("tokens_left")),
    }


async def get_toc_nodes(db: AsyncSession, data_dir, doc_id: str, **budget) -> dict:
    """Flat node list: id, parent, level, number, title, pages, source."""
    await _document(db, doc_id)
    rows = (
        (await db.execute(select(TocNode).where(TocNode.doc_id == doc_id).order_by(TocNode.id)))
        .scalars()
        .all()
    )
    return {
        "doc_id": doc_id,
        "nodes": [
            {
                "node_id": node.node_id,
                "parent_id": node.parent_id,
                "level": node.level,
                "heading_num": node.heading_num,
                "title": node.title,
                "pages": [node.page_start, node.page_end],
                "source": node.source,
            }
            for node in rows
        ],
        "budget": _budget(budget.get("calls_left"), budget.get("tokens_left")),
    }


async def get_printed_toc_llm(
    db: AsyncSession, data_dir, doc_id: str, use_cache: bool = True, **budget
) -> dict:
    """An LLM proposal for the printed TOC, as data the agent can compare.

    ADR-0002 §12: the printed-TOC LLM helpers stay available as a *tool*,
    not as the only entry point. The agent decides whether the stored TOC
    looks wrong and whether a fresh proposal is worth a call; this function
    only reports what the LLM thinks the printed TOC says.

    Returns entries with the printed page string, the resolved 0-based page
    index, level and heading number — the same shape the rule channel
    produces, so the two can be compared entry by entry. A document with no
    readable printed TOC returns ``entries: []`` rather than an error.
    """
    from ..config import get_settings
    from ..ingest.llm_toc import detect_printed_toc_llm
    from ..ingest.reindex import load_parsed
    from ..llm.purpose import resolve_runtime

    doc = await _document(db, doc_id)
    # Same resolution ingest uses: 纲目's endpoint → DB default endpoint →
    # .env. Checking only ``Settings()`` made this tool report "LLM is not
    # configured" while ingest was happily calling the endpoint stored in
    # the database.
    settings, _endpoint, _cfg = await resolve_runtime(db, "gangmu", get_settings())
    if settings is None:
        return {
            "doc_id": doc_id,
            "available": False,
            "reason": "LLM is not configured",
            "entries": [],
            "budget": _budget(budget.get("calls_left"), budget.get("tokens_left")),
        }
    try:
        parsed = load_parsed(doc, settings)
    except (FileNotFoundError, ValueError) as exc:
        raise ToolError(f"parse cache unavailable for {doc_id}: {exc}") from exc
    proposal = detect_printed_toc_llm(parsed, settings, doc_id if use_cache else "")
    if proposal is None:
        return {
            "doc_id": doc_id,
            "available": True,
            "entries": [],
            "note": "no printed TOC found on the first pages",
            "budget": _budget(budget.get("calls_left"), budget.get("tokens_left")),
        }
    return {
        "doc_id": doc_id,
        "available": True,
        "source_page": proposal.source_page,
        "confidence": proposal.confidence,
        "entries": [
            {
                "title": entry.title,
                "heading_num": entry.heading_num,
                "level": entry.level,
                "printed_page": entry.printed_page,
                "page_index": entry.page_index,
                "confidence": entry.confidence,
            }
            for entry in proposal.entries
        ],
        "budget": _budget(budget.get("calls_left"), budget.get("tokens_left")),
    }
