from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AgentNote, Chunk, Document, TocNode
from .collections import (
    collections_by_doc,
    doc_ids_in_scope,
    list_collections,
    resolve_scope,
    unassigned_doc_count,
)


def _doc_dict(
    doc: Document, collection: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "doc_id": doc.id,
        "title": doc.title,
        "filename": doc.filename,
        "file_path": doc.file_path,
        "doc_type": doc.doc_type,
        "doc_date": doc.doc_date,
        "status": doc.status,
        "page_count": doc.page_count,
        "char_count": doc.char_count,
        "summary": doc.summary,
        "keywords": doc.keywords,
        "collection": collection,
        "parse_plan": doc.parse_plan,
        "created_at": doc.created_at,
        "updated_at": doc.updated_at,
    }


def _node_dict(node: TocNode) -> dict[str, Any]:
    return {
        # "id" mirrors node_id: MCP clients naively reach for "id", and the
        # bare node_id spelling is easy to mis-parse as a missing field.
        "id": node.node_id,
        "node_id": node.node_id,
        "parent_id": node.parent_id,
        "level": node.level,
        "heading_num": node.heading_num,
        "title": node.title,
        "heading_path": node.heading_path,
        "page_start": node.page_start,
        "page_end": node.page_end,
        "chunk_start": node.chunk_start,
        "chunk_end": node.chunk_end,
        "summary": node.summary,
        "source": node.source,
        "confidence": node.confidence,
    }


def _node_sort_key(node: TocNode) -> tuple:
    """Reading order: page first, then chunk span, then level, then id.

    ``page_start`` is the PDF page a section begins on; ``chunk_start`` is
    the chunk order the ingester cut, which is more stable than page labels
    for sections whose printed page is ambiguous. Sections with no known
    page sort last, so a ``root`` fallback node never precedes real body.
    """
    return (
        node.page_start if node.page_start is not None else 10**9,
        node.chunk_start if node.chunk_start is not None else 10**9,
        node.level,
        node.node_id,
    )


async def memex_overview(db: AsyncSession) -> dict[str, Any]:
    documents = (
        await db.execute(select(func.count()).select_from(Document))
    ).scalar() or 0
    toc_nodes = (
        await db.execute(select(func.count()).select_from(TocNode))
    ).scalar() or 0
    chunks = (
        await db.execute(select(func.count()).select_from(Chunk))
    ).scalar() or 0
    type_rows = (
        await db.execute(
            select(Document.doc_type, func.count()).group_by(Document.doc_type)
        )
    ).all()
    recent = (
        await db.execute(
            select(Document).order_by(Document.updated_at.desc()).limit(5)
        )
    ).scalars().all()
    recent_collections = await collections_by_doc(db, [doc.id for doc in recent])

    return {
        "documents": documents,
        "toc_nodes": toc_nodes,
        "chunks": chunks,
        "doc_types": {
            str(doc_type or "unknown"): count for doc_type, count in type_rows
        },
        "collections": await list_collections(db),
        "unassigned_doc_count": await unassigned_doc_count(db),
        "recent": [
            _doc_dict(doc, recent_collections.get(doc.id)) for doc in recent
        ],
    }


async def _toc_title_doc_ids(
    db: AsyncSession, query: str, limit: int = 1000
) -> list[str]:
    """Documents whose TOC has a heading naming every query term.

    The strongest "this document is about X" signal available. It exists
    because summaries are unreliable as a topical signal: the summary is
    the first ~300 chars of the body, and documents in a series often open
    with the same preamble listing every part name, so a topic word appears
    in all of their summaries. A heading actually titled with that topic
    does not.
    """
    from .fts import seg_query  # noqa: PLC0415  # keep import light

    terms = [term for term in seg_query(query) if len(term) >= 2]
    if not terms:
        return []
    stmt = select(TocNode.doc_id).distinct()
    for term in terms:
        # A term may contain % or _ (e.g. an OCR artifact); escape them so
        # the filter stays a literal substring match.
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(TocNode.title.like(f"%{escaped}%", escape="\\"))
    rows = (await db.execute(stmt.limit(limit))).scalars().all()
    return list(rows)


async def ranked_document_ids(
    db: AsyncSession, query: str, limit: int = 1000
) -> list[str]:
    """Ids of documents matching ``query``, most relevant first.

    Signals are summed rather than tiered, because tiers let one stray TOC
    heading outrank a document whose title *is* the query:

    - title says it (literal)                 +3
    - keywords say it (TF-IDF, so topical)    +3
    - a TOC heading names it                  +2
    - ``doc_fts`` matched (title/keywords)    +1
    - document identifier resolved            +4

    The summary is not a signal (see ``search_doc_ids``).
    """
    from .fts import search_doc_ids  # noqa: PLC0415  # keep import light
    from .sections import doc_ids_from_query  # noqa: PLC0415

    query = (query or "").strip()
    if not query:
        return []

    scores: dict[str, float] = {}

    def _bump(doc_id: str, weight: float) -> None:
        scores[doc_id] = scores.get(doc_id, 0.0) + weight

    for doc_id in await search_doc_ids(db, query, limit=limit):
        _bump(doc_id, 1.0)
    for doc_id in await _toc_title_doc_ids(db, query):
        _bump(doc_id, 2.0)
    for doc_id in await doc_ids_from_query(db, query) or []:
        _bump(doc_id, 4.0)

    like = f"%{query}%"
    rows = (
        await db.execute(
            select(Document.id, Document.title, Document.filename, Document.keywords)
            .where(
                Document.title.like(like)
                | Document.filename.like(like)
                | Document.keywords.like(like)
            )
        )
    ).all()
    lowered = query.lower()
    for doc_id, title, filename, keywords in rows:
        if lowered in (title or "").lower() or lowered in (filename or "").lower():
            _bump(doc_id, 3.0)
        if lowered in (keywords or "").lower():
            _bump(doc_id, 3.0)

    ordered = sorted(scores.items(), key=lambda item: -item[1])
    return [doc_id for doc_id, _ in ordered][:limit]


async def list_documents(
    db: AsyncSession,
    query: str = "",
    doc_type: str = "",
    limit: int = 20,
    collection: str = "",
) -> list[dict[str, Any]]:
    """List documents, optionally filtered by a text query or collection.

    A query ranks documents by title, keywords and TOC headings through
    ``ranked_document_ids`` — the same signals the web library's
    「筛选文档」 uses, so both agree on what a document is about.
    """
    limit = max(1, min(limit, 100))

    scope_ids: list[str] | None = None
    if collection:
        scope_ids = await doc_ids_in_scope(db, await resolve_scope(db, collection))
        if scope_ids is not None and not scope_ids:
            return []

    if not query.strip():
        stmt = select(Document)
        if doc_type:
            stmt = stmt.where(Document.doc_type == doc_type)
        if scope_ids is not None:
            stmt = stmt.where(Document.id.in_(scope_ids))
        stmt = stmt.order_by(Document.updated_at.desc()).limit(limit)
        documents = (await db.execute(stmt)).scalars().all()
        mapping = await collections_by_doc(db, [doc.id for doc in documents])
        return [_doc_dict(doc, mapping.get(doc.id)) for doc in documents]

    ids = await ranked_document_ids(db, query)
    if scope_ids is not None:
        allowed = set(scope_ids)
        ids = [doc_id for doc_id in ids if doc_id in allowed]
    if not ids:
        return []
    stmt = select(Document).where(Document.id.in_(ids))
    if doc_type:
        stmt = stmt.where(Document.doc_type == doc_type)
    documents = (await db.execute(stmt)).scalars().all()

    # Relevance order, not recency: ranked_document_ids put the topical
    # matches first, and a filter that returns the newest documents
    # regardless of the query is the bug this replaces.
    order = {doc_id: index for index, doc_id in enumerate(ids)}
    documents.sort(key=lambda doc: order[doc.id])
    documents = documents[:limit]
    mapping = await collections_by_doc(db, [doc.id for doc in documents])
    return [_doc_dict(doc, mapping.get(doc.id)) for doc in documents]


async def get_document(db: AsyncSession, doc_id: str) -> dict[str, Any]:
    doc = await db.get(Document, doc_id)
    if doc is None:
        raise ValueError(f"document not found: {doc_id}")

    toc_nodes = (
        await db.execute(
            select(func.count())
            .select_from(TocNode)
            .where(TocNode.doc_id == doc_id)
        )
    ).scalar() or 0
    chunks = (
        await db.execute(
            select(func.count())
            .select_from(Chunk)
            .where(Chunk.doc_id == doc_id)
        )
    ).scalar() or 0

    nodes = (
        (await db.execute(select(TocNode).where(TocNode.doc_id == doc_id)))
        .scalars()
        .all()
    )
    known = {node.node_id for node in nodes}
    roots = [
        node
        for node in nodes
        if node.parent_id is None or node.parent_id not in known
    ]
    roots.sort(key=_node_sort_key)
    collection = (await collections_by_doc(db, [doc_id])).get(doc_id)
    ingest_note = (
        (
            await db.execute(
                select(AgentNote)
                .where(AgentNote.doc_id == doc_id, AgentNote.kind == "ingest")
                .order_by(AgentNote.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )

    return {
        **_doc_dict(doc, collection),
        # Human-written 入库备注: agents may read it, never write it.
        "ingest_note": (
            {
                "id": ingest_note.id,
                "body": ingest_note.body,
                "author": ingest_note.author,
                "created_at": ingest_note.created_at,
            }
            if ingest_note is not None
            else None
        ),
        "toc_nodes": toc_nodes,
        "chunks": chunks,
        "toc_roots": [_node_dict(node) for node in roots],
    }


async def get_toc(
    db: AsyncSession, doc_id: str, depth: int = 2
) -> dict[str, Any]:
    doc = await db.get(Document, doc_id)
    if doc is None:
        raise ValueError(f"document not found: {doc_id}")

    nodes = (
        (await db.execute(select(TocNode).where(TocNode.doc_id == doc_id)))
        .scalars()
        .all()
    )
    by_parent: dict[str | None, list[TocNode]] = {}
    known = set()
    for node in nodes:
        known.add(node.node_id)
        by_parent.setdefault(node.parent_id, []).append(node)

    max_depth = max(1, depth)

    def build(node: TocNode, level: int) -> dict[str, Any]:
        item = _node_dict(node)
        if level < max_depth:
            children = by_parent.get(node.node_id, [])
            children.sort(key=_node_sort_key)
            item["children"] = [
                build(child, level + 1) for child in children
            ]
        else:
            item["children"] = []
        return item

    roots = [
        node
        for node in nodes
        if node.parent_id is None or node.parent_id not in known
    ]
    roots.sort(key=_node_sort_key)

    return {
        "doc_id": doc_id,
        "title": doc.title,
        "depth": depth,
        "collection": (await collections_by_doc(db, [doc_id])).get(doc_id),
        "roots": [build(root, 1) for root in roots],
    }