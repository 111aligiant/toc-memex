from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Chunk, Document
from .fts import scoped_search
from .sections import read_section


def _page_fields(
    page_start: int | None, page_end: int | None
) -> dict[str, Any]:
    """Citation pages: 0-based internal plus the 1-based pages agents cite.

    Search/read_section already expose ``pdf_page_*``; ask must speak the
    same language or an agent citing an ask answer reports the wrong PDF
    page (the 0-based ``page_start``). Printed labels are not stored yet.
    """
    return {
        "page_start": page_start,
        "page_end": page_end,
        "pdf_page_start": page_start + 1 if isinstance(page_start, int) else None,
        "pdf_page_end": page_end + 1 if isinstance(page_end, int) else None,
        "printed_page_start": None,
        "printed_page_end": None,
    }


def _citation(
    hit: dict[str, Any],
    doc_title: str,
    section: dict[str, Any] | None,
    max_snippet: int = 240,
) -> dict[str, Any]:
    if section is not None:
        citation = {
            "doc_id": section["doc_id"],
            "doc_title": doc_title,
            "node_id": section["node_id"],
            "heading_num": section["heading_num"],
            "heading_path": section["heading_path"],
            "chunk_ids": section["chunk_ids"],
            "snippet": (section.get("content") or "")[:max_snippet],
            "collection": hit.get("collection"),
        }
        citation.update(
            _page_fields(section["page_start"], section["page_end"])
        )
        return citation
    citation = {
        "doc_id": hit["doc_id"],
        "doc_title": doc_title,
        "node_id": hit.get("node_id"),
        "heading_num": hit.get("heading_num"),
        "heading_path": hit.get("heading_path", ""),
        "chunk_ids": hit.get("chunk_ids", []),
        "snippet": (hit.get("snippet") or "")[:max_snippet],
        "collection": hit.get("collection"),
    }
    citation.update(_page_fields(hit.get("page_start"), hit.get("page_end")))
    return citation


async def ask_question(
    db: AsyncSession,
    question: str,
    doc_ids: list[str] | None = None,
    top_k: int = 3,
    max_chars: int = 1200,
    collection: str = "",
) -> dict[str, Any]:
    """Extractive answer with section-first citations.

    The answer is the full text of the top section (truncated), not an LLM
    synthesis. This keeps the loop honest and dependency-free: retrieve ->
    read section -> return answer + citation.

    ``collection`` narrows retrieval to one topic collection; a zero-hit
    scoped search explains where the hits actually are, ahead of the usual
    "try a section number" advice.
    """
    scoped = await scoped_search(
        db, question, top_k=top_k, collection=collection, doc_ids=doc_ids
    )
    hits = scoped["results"]
    if not hits:
        hint = "没有找到相关章节；可以尝试章节号、标题关键词或更短的问题。"
        if scoped.get("hint"):
            hint = f"{scoped['hint']} {hint}"
        return {
            "question": question,
            "answer": "",
            "mode": "extractive",
            "citations": [],
            "hits": [],
            "scope": scoped["scope"],
            "hint": hint,
        }

    titles: dict[str, str] = {}
    for hit in hits:
        doc_id = hit["doc_id"]
        if doc_id in titles:
            continue
        doc = await db.get(Document, doc_id)
        titles[doc_id] = doc.title if doc is not None else doc_id

    primary = hits[0]
    section: dict[str, Any] | None = None
    node_id = primary.get("node_id")
    if node_id:
        try:
            section = await read_section(
                db, primary["doc_id"], node_id=node_id, max_chars=max_chars
            )
        except ValueError:
            section = None

    if section is not None and section.get("content"):
        answer = section["content"]
    else:
        answer = primary.get("snippet") or ""

    # A flat document (one 全文 node, or a very long chapter) starts at its
    # first paragraphs, so a section answer never reaches a match that sits
    # in "项目经历" halfway down. When the matched chunk is well inside the
    # node, answer from that chunk instead; the citation narrows to it.
    use_chunk = False
    matched_chunk_id = primary.get("chunk_id")
    section_chunks = (section or {}).get("chunk_ids") or []
    if matched_chunk_id is not None:
        try:
            position = section_chunks.index(matched_chunk_id)
        except ValueError:
            position = -1
        if section is None or position >= 2:
            chunk = await db.get(Chunk, matched_chunk_id)
            if chunk is not None and (chunk.content or "").strip():
                answer = chunk.content
                use_chunk = True

    if len(answer) > max_chars:
        answer = answer[:max_chars].rstrip() + "…"

    citations = [
        _citation(
            primary,
            titles.get(primary["doc_id"], primary["doc_id"]),
            None if use_chunk else section,
        )
    ]
    seen = {(primary["doc_id"], primary.get("node_id"))}
    for hit in hits[1:]:
        key = (hit["doc_id"], hit.get("node_id"))
        if key in seen:
            continue
        seen.add(key)
        citations.append(
            _citation(
                hit,
                titles.get(hit["doc_id"], hit["doc_id"]),
                None,
            )
        )

    return {
        "question": question,
        "answer": answer,
        "mode": "extractive",
        "citations": citations,
        "hits": hits,
        "scope": scoped["scope"],
        "hint": None,
    }