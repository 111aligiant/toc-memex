"""Compact multi-document overview shared by the MCP tool and the console.

Cross-document questions ("which documents cover X, compare them") made the
agent call get_toc and read_section once per document; the TOC text then
stayed in context and was re-sent every later turn. One compact overview
gives the same orientation for a fraction of the tokens.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Document, TocNode
from .collections import collections_by_doc
from .documents import list_documents


async def overview(
    db: AsyncSession,
    *,
    query: str = "",
    doc_ids: list[str] | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Summary, keywords and top headings for several documents at once."""
    limit = max(1, min(int(limit or 10), 30))
    doc_ids = [
        str(item).strip() for item in (doc_ids or []) if str(item).strip()
    ]
    try:
        if doc_ids:
            rows = (
                await db.execute(
                    select(Document).where(Document.id.in_(doc_ids))
                )
            ).scalars().all()
            order = {doc_id: index for index, doc_id in enumerate(doc_ids)}
            rows.sort(key=lambda doc: order.get(doc.id, 10**6))
            rows = rows[:limit]
            mapping = await collections_by_doc(db, [doc.id for doc in rows])
            documents = [
                {
                    "doc_id": doc.id,
                    "title": doc.title,
                    "page_count": doc.page_count,
                    "summary": doc.summary,
                    "keywords": doc.keywords,
                    "collection": mapping.get(doc.id),
                }
                for doc in rows
            ]
        else:
            documents = await list_documents(
                db, query=str(query or "").strip(), limit=limit
            )
    except Exception as exc:
        return {"error": str(exc)}

    ids = [doc.get("doc_id") for doc in documents if doc.get("doc_id")]
    headings: dict[str, list[dict]] = {}
    if ids:
        nodes = (
            await db.execute(
                select(TocNode)
                .where(TocNode.doc_id.in_(ids), TocNode.parent_id.is_(None))
                .order_by(TocNode.page_start, TocNode.level, TocNode.node_id)
            )
        ).scalars().all()
        for node in nodes:
            bucket = headings.setdefault(node.doc_id, [])
            if len(bucket) < 8:
                bucket.append(
                    {
                        "node_id": node.node_id,
                        "heading": node.heading_num,
                        "title": node.title,
                        "page": node.page_start,
                    }
                )
    return {
        "count": len(documents),
        "documents": [
            {
                "doc_id": doc.get("doc_id"),
                "title": doc.get("title"),
                "page_count": doc.get("page_count"),
                "collection": (
                    (doc.get("collection") or {}).get("name")
                    if isinstance(doc.get("collection"), dict)
                    else doc.get("collection")
                ),
                "summary": (doc.get("summary") or "")[:300],
                "keywords": (doc.get("keywords") or "")[:150],
                "headings": headings.get(doc.get("doc_id"), []),
            }
            for doc in documents
        ],
    }
