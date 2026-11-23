from __future__ import annotations

import re
import sqlite3

import jieba
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..identifiers import DOCUMENT_ID_RE
from ..models import Chunk, Document
from ..textnorm import normalize_encoding_artifacts
from .collections import (
    UNASSIGNED,
    Scope,
    collections_by_doc,
    doc_ids_in_scope,
    resolve_scope,
)
from .sections import (
    doc_ids_from_query,
    extract_section_numbers,
    is_pure_section_query,
    section_hits,
    title_echo,
    title_hits,
)

STOP_WORDS = {
    "的", "了", "和", "是", "在", "我", "有", "就", "不", "人", "都", "一",
    "一个", "什么", "怎么", "如何", "这个", "那个", "可以", "需要", "进行",
    "the", "a", "an", "of", "to", "and", "or", "is", "are", "for", "in",
    "on", "with", "as", "by", "at", "be", "this", "that",
}

def term_synonyms(term: str) -> tuple[str, ...]:
    """Alternatives for a query term, from the optional configured map.

    The map is generic and empty by default: a library that wants
    cross-language or domain aliases supplies its own dictionary through
    ``TOCMEMEX_SEARCH_SYNONYMS``, a JSON object mapping a term to a list
    of alternatives.
    """
    synonyms = get_settings().tocmemex_search_synonyms or {}
    return tuple(synonyms.get((term or "").lower(), ()))


def _expand_synonyms(terms: list[str]) -> list[str]:
    """Add configured synonyms for each term; original terms come first."""
    expanded: list[str] = []
    for term in terms:
        if term not in expanded:
            expanded.append(term)
        for synonym in term_synonyms(term):
            if synonym not in expanded:
                expanded.append(synonym)
    return expanded


# Tokens so common in document prose that matching them alone says nothing
# about relevance. When a query's only strong matches are generic tokens,
# BM25 ranks whichever chunk repeats them most; the refinement pass below
# re-runs the distinctive terms. This list is deliberately document-generic.
_GENERIC_TOKENS = {
    "要求", "说明", "方法", "条件", "范围", "规定",
    "文件", "资料", "概述", "简介", "程序", "过程",
    "内容", "部分", "方面", "情况", "一般", "相关",
    "主要", "基本", "目的", "适用", "包括", "以及",
}

IDENT_RE = re.compile(
    rf"(?:{DOCUMENT_ID_RE.pattern})"
    r"|\d+(?:\.\d+){1,4}"
    r"|\d+(?:\.\d+)?\s*(?:℃|°C|%|mm|cm|m|s|min|h|Hz|kHz|MPa|kPa|V|A|W)",
    re.IGNORECASE,
)


def seg_index(value: str) -> str:
    """Index-time segmentation: search mode adds subwords for recall."""
    return " ".join(jieba.lcut_for_search(value or ""))


def count_tokens(value: str) -> int:
    """Token count using the same jieba dictionary as indexing."""
    return len(jieba.lcut(value or ""))


def _is_cjk(ch: str) -> bool:
    """True for a single CJK ideograph (Han block)."""
    return (
        len(ch) == 1
        and "\u4e00" <= ch <= "\u9fff"
    )


def seg_query(value: str) -> list[str]:
    words = jieba.lcut(value or "")
    kept: list[str] = []
    for word in words:
        token = word.strip()
        if not token or token in STOP_WORDS or token.isspace():
            continue
        # Keep multi-character tokens as before. Also keep single-character
        # CJK ideographs: in Chinese a lone character is a real query
        # ("泵", "阀", "水") and dropping it made such searches return
        # nothing (B7). Single ASCII/digit tokens stay filtered as noise.
        if len(token) >= 2 or _is_cjk(token):
            kept.append(token)
    return kept


def _quote(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def build_match(query: str) -> str:
    return _build_match(query, drop_identifiers=False)


def build_doc_match(query: str) -> str:
    """AND-of-groups match for document-level filtering.

    Chunk search ORs the terms and lets BM25 rank; a document *filter*
    must narrow instead, so every query term (with its synonyms as
    alternatives) has to appear.

    Identifiers are stripped: an identifier is indexed with its spaces
    removed, so it can never match as a phrase. Callers match identifiers
    against the compacted title instead.
    """
    query = normalize_encoding_artifacts((query or "").strip())
    if not query:
        return ""
    spans = [match.span() for match in IDENT_RE.finditer(query)]
    for start, end in sorted(spans, reverse=True):
        query = query[:start] + " " + query[end:]

    groups: list[list[str]] = []
    for term in seg_query(query):
        group = [term]
        for synonym in term_synonyms(term):
            if synonym not in group:
                group.append(synonym)
        groups.append(group)
    if not groups:
        return ""
    return " AND ".join(
        "(" + " OR ".join(_quote(term) for term in dict.fromkeys(group)) + ")"
        for group in groups
    )


def _build_match(query: str, drop_identifiers: bool = False) -> str:
    query = (query or "").strip()
    if not query:
        return ""
    terms: list[str] = []
    if drop_identifiers:
        # The identifier channel already routed the query to documents; keep
        # the identifier out of body ranking or foreword/reference chunks
        # that repeat the standard number outrank the real content.
        spans = [m.span() for m in IDENT_RE.finditer(query)]
        for start, end in sorted(spans, reverse=True):
            query = query[:start] + " " + query[end:]
        query = query.strip()
        if not query:
            return ""
    else:
        for ident in IDENT_RE.findall(query):
            terms.append(_quote(ident))
    words = _expand_synonyms(seg_query(query))
    for word in words:
        # A synonym is often a multi-word phrase ("锂离子电池组"). The index
        # is built with ``seg_index`` (lcut_for_search), which splits such a
        # phrase into adjacent subwords, so quoting the whole phrase as a
        # single FTS phrase never matches (B4). Segment the expansion the
        # same way the index is segmented and OR the resulting tokens, so a
        # synonym contributes its real tokens instead of an unmatchable
        # phrase. Single-token terms are unaffected (one token in, one out).
        part_tokens = [tok for tok in seg_index(word).split() if tok]
        for token in part_tokens or [word]:
            terms.append(_quote(token))
    return " OR ".join(dict.fromkeys(terms))


async def index_doc_row(db: AsyncSession, doc: Document) -> None:
    """(Re)write the document-level FTS row (title / summary / keywords).

    Kept separate from ``index_document`` so an op that changes only the
    document row (``set_keywords``, ``set_summary``) refreshes exactly what
    the document filter and the zero-hit fallback read, without rebuilding
    every chunk row.
    """
    await db.execute(
        text("DELETE FROM doc_fts WHERE doc_id = :doc_id"), {"doc_id": doc.id}
    )
    await db.execute(
        text(
            """
            INSERT INTO doc_fts(title, summary, keywords, doc_id)
            VALUES (:title, :summary, :keywords, :doc_id)
            """
        ),
        {
            "title": seg_index(doc.title),
            "summary": seg_index(doc.summary or ""),
            "keywords": seg_index(doc.keywords or ""),
            "doc_id": doc.id,
        },
    )
    await db.flush()


async def index_document(db: AsyncSession, doc: Document, chunks: list[Chunk]) -> None:
    await db.execute(
        text("DELETE FROM chunk_fts WHERE doc_id = :doc_id"), {"doc_id": doc.id}
    )

    for chunk in chunks:
        await db.execute(
            text(
                """
                INSERT INTO chunk_fts(title, heading, body, doc_id, node_id, chunk_id)
                VALUES (:title, :heading, :body, :doc_id, :node_id, :chunk_id)
                """
            ),
            {
                "title": seg_index(doc.title),
                "heading": seg_index(chunk.heading_path or ""),
                "body": seg_index(chunk.content),
                "doc_id": doc.id,
                "node_id": chunk.node_id or "",
                "chunk_id": chunk.id,
            },
        )

    await index_doc_row(db, doc)


def _row_to_hit(row, match_type: str) -> dict:
    """Shape a SQL row into a section-first citation result."""
    page_start = row.get("page_start")
    page_end = row.get("page_end")
    chunk_page_start = row.get("chunk_page_start")
    chunk_page_end = row.get("chunk_page_end")
    if page_start is None:
        page_start = chunk_page_start
    if page_end is None:
        page_end = chunk_page_end
    return {
        "chunk_id": row["chunk_id"],
        "chunk_ids": [row["chunk_id"]],
        "doc_id": row["doc_id"],
        "node_id": row.get("node_id"),
        "heading_num": row.get("heading_num"),
        "heading_path": row.get("heading_path") or "",
        "title": row.get("title"),
        "page_start": page_start,
        "page_end": page_end,
        "chunk_page_start": chunk_page_start,
        "chunk_page_end": chunk_page_end,
        "snippet": row.get("snippet") or "",
        "score": float(row.get("score") or 0.0),
        "match_type": match_type,
    }


async def search_doc_ids(
    db: AsyncSession, query: str, limit: int = 1000
) -> list[str]:
    """Document-level match over title and keywords (``doc_fts``).

    This is the document filter behind ``list_documents`` and the web
    library's 「筛选文档」. It answers "which documents are about X", so it
    covers the keywords the ingest step generates.

    The ``summary`` column is deliberately excluded. The summary is the
    first ~300 chars of the body, and documents in a series often open
    with the same preamble listing every part name — so a topic word
    appears in all of their summaries while only the part actually about
    it is relevant. Keywords are extracted from the same text but TF-IDF
    ranked, so the part's own topic wins over the shared boilerplate.
    """
    match = build_doc_match(query)
    if not match:
        return []
    # Column filter keeps summary out of the match itself, not just the score.
    sql = (
        "SELECT doc_id FROM doc_fts WHERE doc_fts MATCH :q "
        "ORDER BY bm25(doc_fts, 3.0, 1.0, 2.0) ASC LIMIT :k"
    )
    try:
        rows = (
            await db.execute(
                text(sql),
                {"q": f"{{title keywords}} : ({match})", "k": limit},
            )
        ).mappings().all()
    except (OperationalError, sqlite3.OperationalError):
        return []
    return [row["doc_id"] for row in rows]


async def _like_search(
    db: AsyncSession, query: str, top_k: int, doc_ids: list[str] | None
) -> list[dict]:
    sql = (
        "SELECT c.id AS chunk_id, c.doc_id, c.node_id, "
        "substr(c.content, 1, 240) AS snippet, 0.0 AS score, "
        "c.chunk_index, c.page_start AS chunk_page_start, "
        "c.page_end AS chunk_page_end, "
        "t.heading_num, t.heading_path, t.title, t.page_start, t.page_end "
        "FROM chunks c "
        "LEFT JOIN toc_nodes t "
        "ON t.doc_id = c.doc_id AND t.node_id = c.node_id "
        "WHERE c.content LIKE :like"
    )
    params: dict = {"like": f"%{query}%", "k": top_k}
    if doc_ids:
        placeholders = ", ".join(f":did_{i}" for i in range(len(doc_ids)))
        sql += f" AND c.doc_id IN ({placeholders})"
        for i, doc_id in enumerate(doc_ids):
            params[f"did_{i}"] = doc_id
    sql += " ORDER BY c.chunk_index LIMIT :k"
    rows = (await db.execute(text(sql), params)).mappings().all()
    return [_row_to_hit(row, "like") for row in rows]


async def _bm25_search(
    db: AsyncSession,
    query: str,
    match: str,
    top_k: int,
    doc_ids: list[str] | None,
) -> list[dict]:
    sql = (
        "SELECT chunk_fts.chunk_id, chunk_fts.doc_id, chunk_fts.node_id, "
        "snippet(chunk_fts, 2, '<b>', '</b>', '…', 24) AS snippet, "
        "bm25(chunk_fts, 3.0, 2.0, 1.0) AS score, "
        "c.chunk_index, c.page_start AS chunk_page_start, "
        "c.page_end AS chunk_page_end, "
        "t.heading_num, t.heading_path, t.title, t.page_start, t.page_end "
        "FROM chunk_fts "
        "LEFT JOIN chunks c ON c.id = chunk_fts.chunk_id "
        "LEFT JOIN toc_nodes t "
        "ON t.doc_id = chunk_fts.doc_id AND t.node_id = chunk_fts.node_id "
        "WHERE chunk_fts MATCH :q"
    )
    params: dict = {"q": match, "k": top_k}
    if doc_ids:
        placeholders = ", ".join(f":did_{i}" for i in range(len(doc_ids)))
        sql += f" AND chunk_fts.doc_id IN ({placeholders})"
        for i, doc_id in enumerate(doc_ids):
            params[f"did_{i}"] = doc_id
    sql += " ORDER BY score ASC LIMIT :k"

    try:
        rows = (await db.execute(text(sql), params)).mappings().all()
    except (OperationalError, sqlite3.OperationalError):
        return await _like_search(db, query, top_k, doc_ids)
    return [_row_to_hit(row, "bm25") for row in rows]


async def search_chunks(
    db: AsyncSession,
    query: str,
    top_k: int = 8,
    doc_ids: list[str] | None = None,
    scope_doc_ids: list[str] | None = None,
) -> list[dict]:
    """Search chunks, optionally narrowed to a topic collection.

    ``scope_doc_ids`` is the collection's document set: ``None`` means no
    collection filter, ``[]`` means the collection is empty and must return
    nothing (never fall back to a whole-library search). Every hit carries
    its ``collection`` so callers can see where it came from.
    """
    hits = await _search_chunks(
        db, query, top_k=top_k, doc_ids=doc_ids, scope_doc_ids=scope_doc_ids
    )
    if not hits:
        return hits
    mapping = await collections_by_doc(db, [hit["doc_id"] for hit in hits])
    for hit in hits:
        hit["collection"] = mapping.get(hit["doc_id"])
    return hits


FILTER_KEYS = {"doc_type", "source", "date_from", "date_to", "doc_ids", "doc_id"}


def _filter_string(filters: dict, key: str) -> str:
    value = filters[key]
    if not isinstance(value, str):
        raise ValueError(f"filter {key} 必须是字符串")
    return value


async def doc_ids_for_filters(
    db: AsyncSession, filters: dict
) -> list[str] | None:
    """Resolve document-metadata filters to a document-id list.

    ``None`` means "no filter"; ``[]`` means the filter matched no document,
    so the caller must return zero hits and never fall back to the whole
    library. Filters combine with AND: a document must satisfy every key.

    ``doc_id`` / ``doc_ids`` are the same selector — one singular, one
    plural — so the union of the ids they name is the allowed set.
    ``date_from`` / ``date_to`` compare ``Document.doc_date`` lexically
    (stored as ISO strings); a document with no date matches no date bound,
    because an unknown date is not "inside" any requested range.
    """
    if filters is None:
        return None
    if not isinstance(filters, dict):
        raise ValueError("filters 必须是对象（dict）")
    if not filters:
        return None
    unknown = set(filters) - FILTER_KEYS
    if unknown:
        raise ValueError(
            "未知 filter 键: "
            + "、".join(sorted(unknown))
            + "；可用: "
            + "、".join(sorted(FILTER_KEYS))
        )

    allowed: set[str] | None = None

    def _narrow(ids: list[str]) -> None:
        nonlocal allowed
        ids_set = set(ids)
        allowed = ids_set if allowed is None else allowed & ids_set

    for key in ("doc_type", "source"):
        if key in filters:
            value = _filter_string(filters, key)
            rows = await db.execute(
                select(Document.id).where(getattr(Document, key) == value)
            )
            _narrow(list(rows.scalars().all()))

    wanted: list[str] = []
    if "doc_id" in filters:
        wanted.append(_filter_string(filters, "doc_id"))
    if "doc_ids" in filters:
        value = filters["doc_ids"]
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise ValueError("filter doc_ids 必须是字符串列表")
        wanted.extend(value)
    if wanted:
        rows = await db.execute(
            select(Document.id).where(
                Document.id.in_(list(dict.fromkeys(wanted)))
            )
        )
        _narrow(list(rows.scalars().all()))

    for key, op in (("date_from", ">="), ("date_to", "<=")):
        if key in filters:
            value = _filter_string(filters, key).strip()
            # An empty/blank bound is not a bound: treat it as absent rather
            # than as an always-true comparison.
            if not value:
                continue
            column = Document.doc_date
            condition = column >= value if op == ">=" else column <= value
            rows = await db.execute(
                select(Document.id).where(column.is_not(None), condition)
            )
            _narrow(list(rows.scalars().all()))

    return None if allowed is None else sorted(allowed)


_FRONT_MATTER_RE = re.compile(
    r"参考文献|参考资料|引用文献|索引|目录|前言|封面|版权"
)


def _is_front_matter(hit: dict) -> bool:
    """A chapter that is apparatus, not body text (references, index, …)."""
    text = " ".join(
        [hit.get("heading_path") or "", hit.get("title") or ""]
    )
    return bool(_FRONT_MATTER_RE.search(text))


async def _search_chunks(
    db: AsyncSession,
    query: str,
    top_k: int = 8,
    doc_ids: list[str] | None = None,
    scope_doc_ids: list[str] | None = None,
) -> list[dict]:
    # Queries can carry the same font/width damage as the documents
    # ("犌犅犜３８９２４" / "４．１"); normalize both sides to meet in the middle.
    query = normalize_encoding_artifacts(query)
    match = build_match(query)
    numbers = extract_section_numbers(query)
    if not match and not numbers:
        return []

    # Identifier channel: a query that names a document scopes every channel
    # to it instead of letting BM25 rank similar clauses from other
    # documents above the right one. An identifier with no matching
    # document yields no results rather than whole-library noise.
    routed_doc_ids = doc_ids
    identifier_routed = False
    if doc_ids is None:
        query_doc_ids = await doc_ids_from_query(db, query)
        if query_doc_ids is not None:
            routed_doc_ids = query_doc_ids or []
            identifier_routed = True

    # Collection narrowing happens AFTER identifier routing: routing needs
    # to run with doc_ids is None to resolve an identifier into documents,
    # and passing the collection as doc_ids would silently disable it.
    if scope_doc_ids is not None:
        allowed = set(scope_doc_ids)
        if not allowed:
            return []
        if routed_doc_ids is None:
            routed_doc_ids = sorted(allowed)
        else:
            routed_doc_ids = [doc_id for doc_id in routed_doc_ids if doc_id in allowed]
            if not routed_doc_ids:
                return []

    if identifier_routed and not routed_doc_ids:
        return []

    # Section-number queries go straight to the TOC node. This is what makes
    # "8.3" resolve to "8.3 方法" instead of BM25 noise such as 26.6.2.8.3.
    exact_hits = (
        await section_hits(db, numbers, doc_ids=routed_doc_ids, limit=top_k)
        if numbers
        else []
    )
    pure_section = is_pure_section_query(query)
    if exact_hits and pure_section:
        return exact_hits[:top_k]

    title_channel_hits = (
        await title_hits(
            db, query, doc_ids=routed_doc_ids, limit=max(1, top_k - len(exact_hits))
        )
        if not pure_section
        else []
    )

    merged: list[dict] = []
    seen_nodes: set[tuple[str, str | None]] = set()
    for hit in [*exact_hits, *title_channel_hits]:
        key = (hit["doc_id"], hit.get("node_id"))
        if key in seen_nodes:
            continue
        seen_nodes.add(key)
        merged.append(hit)

    remaining = max(0, top_k - len(merged))
    body_match = (
        _build_match(query, drop_identifiers=True)
        if identifier_routed and routed_doc_ids
        else match
    )
    bm25_hits = (
        await _bm25_search(db, query, body_match, remaining, routed_doc_ids)
        if body_match and remaining
        else []
    )

    seen_chunks = {
        chunk_id
        for hit in merged
        for chunk_id in hit.get("chunk_ids", [])
        if chunk_id is not None
    }
    seen_node_ids = {hit["node_id"] for hit in merged if hit.get("node_id")}
    for hit in bm25_hits:
        if hit["chunk_id"] in seen_chunks:
            continue
        if hit.get("node_id") and hit["node_id"] in seen_node_ids:
            continue
        merged.append(hit)
        if len(merged) >= top_k:
            break

    merged = await _refine_generic_hits(
        db, query, merged, top_k, routed_doc_ids
    )

    # Reference/index/foreword pages are apparatus. Keep them, but put body
    # hits first unless the query itself asks for front matter.
    if not _FRONT_MATTER_RE.search(query):
        body_hits = [hit for hit in merged if not _is_front_matter(hit)]
        front_hits = [hit for hit in merged if _is_front_matter(hit)]
        merged = body_hits + front_hits

    # A query that repeats a section title names that section outright.
    # "4.2 薄膜介电性能…" is the section of the document that owns that
    # title, not the other document's unrelated "4.2"; an English heading
    # quoted inside a Chinese question points at that English section.
    # Promotion is stable, so channel order is otherwise preserved.
    echoed = [hit for hit in merged if title_echo(query, hit.get("title"))]
    if echoed:
        echoed_ids = {id(hit) for hit in echoed}
        merged = echoed + [hit for hit in merged if id(hit) not in echoed_ids]

    if not merged and identifier_routed and routed_doc_ids:
        return await _doc_fallback_hits(db, routed_doc_ids, top_k)
    if not merged and not identifier_routed and IDENT_RE.fullmatch(query.strip()):
        return await _like_search(db, query, top_k, routed_doc_ids)
    # Single-character CJK queries cannot match FTS: the character is a
    # subword inside a longer indexed token ("泵" lives inside the token
    # "水泵"), and FTS matches whole tokens, so the MATCH returns nothing.
    # Fall back to a substring LIKE so such searches still have recall (B7).
    # If the substring is absent too, fall through to the document channel:
    # a keyword added by curation may name the concept the body only spells
    # differently.
    if not merged and not identifier_routed and _is_short_cjk_query(query):
        like_hits = await _like_search(db, query, top_k, routed_doc_ids)
        if like_hits:
            return like_hits
    if not merged:
        # Document-level channel: a title or keyword set that contains every
        # query term is a real answer even when no chunk repeats them (the
        # body may be OCR-damaged, or the terms live in metadata). This is
        # also what lets ``set_keywords`` close the search-quality loop: the
        # keyword lands in doc_fts, so a query that missed before now returns
        # the document instead of an empty page.
        doc_hits = await search_doc_ids(db, query)
        if routed_doc_ids is not None:
            allowed = set(routed_doc_ids)
            doc_hits = [doc_id for doc_id in doc_hits if doc_id in allowed]
        if doc_hits:
            return await _doc_fallback_hits(db, doc_hits, top_k)
    return merged


def _is_short_cjk_query(query: str) -> bool:
    """True when the query is one or two CJK ideographs (a LIKE-only case)."""
    stripped = (query or "").strip()
    return (
        1 <= len(stripped) <= 2
        and all(_is_cjk(ch) for ch in stripped)
    )


async def _doc_fallback_hits(
    db: AsyncSession, doc_ids: list[str], limit: int
) -> list[dict]:
    """Doc-level hits when an identifier names documents but nothing more.

    An identifier routes to its document family, but after the identifier
    is dropped no body tokens remain for BM25 and no section number is
    given. Returning the documents themselves (title + first chunk) beats
    an empty results page; the web UI links them straight to the document.
    """
    docs = (
        (await db.execute(select(Document).where(Document.id.in_(doc_ids))))
        .scalars()
        .all()
    )
    hits: list[dict] = []
    for doc in docs[: max(1, limit)]:
        first = (
            await db.execute(
                select(Chunk)
                .where(Chunk.doc_id == doc.id)
                .order_by(Chunk.chunk_index)
                .limit(1)
            )
        ).scalar_one_or_none()
        snippet = (first.content if first else (doc.summary or "")).strip()[:240]
        page_start = first.page_start if first else None
        page_end = first.page_end if first else None
        hits.append(
            {
                "chunk_id": first.id if first else None,
                "chunk_ids": [first.id] if first else [],
                "doc_id": doc.id,
                "node_id": None,
                "heading_num": None,
                "heading_path": "",
                "title": doc.title,
                "page_start": page_start,
                "page_end": page_end,
                "chunk_page_start": page_start,
                "chunk_page_end": page_end,
                "snippet": snippet,
                "score": -50.0,
                "match_type": "doc",
            }
        )
    return hits


async def _refine_generic_hits(
    db: AsyncSession,
    query: str,
    hits: list[dict],
    top_k: int,
    doc_ids: list[str] | None,
) -> list[dict]:
    """Demote hits carried only by generic tokens.

    When every token that matched the top hits is a generic stand-by
    ("要求", "说明", …) and the query also had distinctive tokens, BM25
    ranked chunks that merely repeat the generic ones. Re-run the body
    match without the generic tokens; if that yields hits, put them first
    and demote the generic-only ones.
    """
    words = seg_query(query)
    distinctive = [
        word
        for word in _expand_synonyms(words)
        if word.lower() not in _GENERIC_TOKENS
    ]
    if not distinctive:
        return hits
    generic = [
        word
        for word in words
        if word.lower() in _GENERIC_TOKENS
    ]
    if not generic:
        return hits

    generic_set = {word.lower() for word in generic}

    def _matched_terms(hit: dict) -> set[str]:
        text = " ".join(
            (hit.get("snippet") or "", hit.get("heading_path") or "")
        ).lower()
        return {word.lower() for word in distinctive if word.lower() in text}

    bm25_hits = [hit for hit in hits if hit.get("match_type") == "bm25"]
    if not bm25_hits:
        return hits
    top_terms = _matched_terms(bm25_hits[0])
    if top_terms:
        # The top hit does match a distinctive term; nothing to fix.
        return hits

    refined_match = " OR ".join(_quote(word) for word in distinctive)
    refined = await _bm25_search(
        db, query, refined_match, top_k, doc_ids
    )
    if not refined:
        return hits

    for hit in refined:
        if hit["chunk_id"] in {
            existing.get("chunk_id") for existing in hits
        }:
            continue
        if hit.get("node_id") and hit.get("node_id") in {
            existing.get("node_id") for existing in hits
        }:
            continue
        hits.append(hit)

    # Generic-only bm25 hits sink below the refined ones.
    bm25_only = [hit for hit in hits if hit.get("match_type") == "bm25"]
    rest = [hit for hit in hits if hit.get("match_type") != "bm25"]
    bm25_only.sort(
        key=lambda hit: 0 if _matched_terms(hit) else 1
    )
    return rest + bm25_only[:top_k]

def _scope_out(scope: Scope) -> dict:
    return {
        "collection": (
            {"id": scope.collection.id, "name": scope.collection.name}
            if scope.collection is not None
            else None
        ),
        "unassigned": scope.unassigned,
    }


def _scope_label(scope: Scope) -> str:
    if scope.collection is not None:
        return scope.collection.name
    return UNASSIGNED


async def scoped_miss_status(
    db: AsyncSession, query: str, scope: Scope, top_k: int = 3
) -> tuple[str | None, bool]:
    """Return ``(hint, has_hits_elsewhere)`` for a zero-hit scoped search.

    ``has_hits_elsewhere`` distinguishes "wrong collection" (``scoped_miss``)
    from "the library has nothing" (``no_match``): the hint text alone is not
    enough to tell the two apart reliably.
    """
    if not scope.active:
        return None, False
    label = _scope_label(scope)
    elsewhere = await search_chunks(db, query, top_k=max(1, top_k))
    counts: dict[str, int] = {}
    for hit in elsewhere:
        collection = hit.get("collection")
        name = collection["name"] if collection else UNASSIGNED
        counts[name] = counts.get(name, 0) + 1
    if not counts:
        return (
            f"0 hits in collection '{label}'. No other collection has hits "
            "either; try a shorter query or a section number.",
            False,
        )
    listing = ", ".join(f"{name} ({count})" for name, count in counts.items())
    return (
        f"0 hits in collection '{label}'. Other collections with hits: "
        f"{listing}. Re-run without collection, or pick another.",
        True,
    )


async def scoped_miss_hint(
    db: AsyncSession, query: str, scope: Scope, top_k: int = 3
) -> str | None:
    """Explain a zero-hit scoped search by naming where the hits actually are.

    An agent that picked the wrong collection would otherwise read an empty
    result as "the library has nothing". This re-runs the same query
    unscoped (full merge, including doc-level fallback hits) and counts hits
    per collection, so the counts match what the agent sees after dropping
    the filter.
    """
    hint, _ = await scoped_miss_status(db, query, scope, top_k)
    return hint


_SEARCH_MODES = ("auto", "lexical", "semantic", "hybrid")
_SEMANTIC_MODES = frozenset({"semantic", "hybrid"})
_MODE_NOTE = "语义通道尚未启用，本次按 lexical 执行；结果仍带页码引用。"


def _decorate_pages(hits: list[dict]) -> list[dict]:
    """Add the agent-facing page fields to every hit, on every channel.

    ``page_start/page_end`` keep their internal 0-based PDF-index meaning.
    ``pdf_page_*`` are the 1-based pages an agent should cite; the printed
    labels are not stored yet, so ``printed_page_*`` stay null.
    """
    for hit in hits:
        page_start = hit.get("page_start")
        page_end = hit.get("page_end")
        hit["pdf_page_start"] = (
            page_start + 1 if isinstance(page_start, int) else None
        )
        hit["pdf_page_end"] = (
            page_end + 1 if isinstance(page_end, int) else None
        )
        hit["printed_page_start"] = None
        hit["printed_page_end"] = None
    return hits


def _channels_used(hits: list[dict]) -> list[str]:
    return list(
        dict.fromkeys(
            hit.get("match_type") for hit in hits if hit.get("match_type")
        )
    )


def _next_steps(hits: list[dict]) -> list[dict]:
    """Deterministic next calls: up to two section reads plus the TOC."""
    steps: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for hit in hits:
        node_id = hit.get("node_id")
        if not node_id:
            continue
        key = (hit["doc_id"], node_id)
        if key in seen:
            continue
        seen.add(key)
        steps.append(
            {
                "tool": "read_section",
                "args": {"doc_id": hit["doc_id"], "node_id": node_id},
            }
        )
        if len(steps) >= 2:
            break
    if hits and len(steps) < 3:
        steps.append({"tool": "get_toc", "args": {"doc_id": hits[0]["doc_id"]}})
    return steps[:3]


async def _miss_hints(
    db: AsyncSession,
    query: str,
    scope: Scope,
    filters: dict,
    results: list[dict],
) -> list[str]:
    """Deterministic zero-hit advice; no LLM, no retries."""
    if results:
        # A successful search gets no forced advice, so ``hint`` stays null
        # for callers that rely on the legacy contract.
        return []
    hints: list[str] = []
    if scope.active:
        hint = await scoped_miss_hint(db, query, scope)
        if hint:
            hints.append(hint)
    if filters:
        rendered = ", ".join(f"{key}={value!r}" for key, value in filters.items())
        hints.append(
            f"filters 把结果过滤空了：{rendered}；去掉 filters 或放宽日期。"
        )
    if not scope.active and not filters:
        if extract_section_numbers(query):
            hints.append(
                "章节号没命中；先 get_toc(doc_id) 确认编号，或改用标题关键词。"
            )
        else:
            hints.append("换更短的词、同义词，或用文档里的英文/缩写再试。")
    return hints


async def scoped_search(
    db: AsyncSession,
    query: str,
    top_k: int = 8,
    collection: str = "",
    doc_ids: list[str] | None = None,
    filters: dict | None = None,
    mode: str = "auto",
) -> dict:
    """Search, optionally narrowed by collection and metadata filters.

    ``mode`` accepts ``auto|lexical|semantic|hybrid`` (an unknown value is a
    ``ValueError``; the MCP layer turns it into a ToolError). Only lexical
    retrieval exists this round, so every mode runs the lexical pass:
    ``semantic`` / ``hybrid`` report ``degraded=True`` and explain in
    ``mode_note`` instead of pretending to be semantic.

    Returns ``results`` / ``hint`` / ``scope`` (backward compatible) plus
    ``hints`` / ``next`` / ``empty_reason`` / ``mode_*`` / ``filters`` /
    ``channels_used``. ``hint`` is ``hints[0]`` when there is one. Every
    hit carries ``pdf_page_*`` (1-based PDF pages) alongside the unchanged
    0-based internal ``page_start/page_end``; ``printed_page_*`` are null.

    Metadata ``filters`` resolve to a document set (``doc_ids_for_filters``)
    and are intersected with any explicit ``doc_ids``. Supplying either one
    means the caller already fixed the scope, so identifier routing is
    disabled (``search_chunks`` sees ``doc_ids`` not None). A collection
    alone keeps routing working through ``scope_doc_ids``.
    """
    requested = str(mode or "auto").strip().lower()
    if requested not in _SEARCH_MODES:
        raise ValueError("mode 必须是 auto|lexical|semantic|hybrid")
    degraded = requested in _SEMANTIC_MODES
    mode_note = _MODE_NOTE if degraded else None

    if filters is not None and not isinstance(filters, dict):
        raise ValueError("filters 必须是对象（dict）")
    filters = filters or {}
    echo_filters = dict(filters)

    scope = await resolve_scope(db, collection)
    scope_ids = await doc_ids_in_scope(db, scope)
    filter_ids = await doc_ids_for_filters(db, filters)

    def _envelope(
        results: list[dict], empty_reason: str | None, hints: list[str]
    ) -> dict:
        results = _decorate_pages(results)
        return {
            "results": results,
            "hint": hints[0] if hints else None,
            "hints": hints,
            "next": _next_steps(results),
            "empty_reason": empty_reason,
            "mode_requested": requested,
            "mode_effective": "lexical",
            "degraded": degraded,
            "mode_note": mode_note,
            "filters": echo_filters,
            "channels_used": _channels_used(results),
            "scope": _scope_out(scope),
        }

    async def _empty(reason: str) -> dict:
        hints = await _miss_hints(db, query, scope, filters, [])
        return _envelope([], reason, hints)

    # An empty collection is a real dead end: return nothing, never the
    # whole library.
    if scope.active and not scope_ids:
        return await _empty("collection_empty")
    # A filter that matched no document is likewise a dead end.
    if filter_ids == []:
        return await _empty("filtered_out")

    has_explicit = doc_ids is not None or filter_ids is not None
    combined: list[str] | None = None
    if doc_ids is not None:
        combined = list(dict.fromkeys(doc_ids))
    if filter_ids is not None:
        allowed = set(filter_ids)
        combined = (
            filter_ids
            if combined is None
            else [doc_id for doc_id in combined if doc_id in allowed]
        )
    if has_explicit and scope_ids is not None:
        allowed = set(scope_ids)
        combined = [doc_id for doc_id in (combined or []) if doc_id in allowed]
    # Empty intersection: the caller's own scope excluded everything, so
    # short-circuit here rather than passing [] (which flips identifier
    # routing off and could silently widen a section query).
    if has_explicit and not combined:
        return await _empty("filtered_out")

    if has_explicit:
        results = await search_chunks(
            db, query, top_k=top_k, doc_ids=combined, scope_doc_ids=None
        )
    else:
        results = await search_chunks(
            db, query, top_k=top_k, doc_ids=None, scope_doc_ids=scope_ids
        )

    empty_reason: str | None = None
    if not results:
        if not seg_query(normalize_encoding_artifacts(query or "")):
            empty_reason = "no_terms"
        elif scope.active:
            # "Wrong collection" only when the same query does hit somewhere
            # else; otherwise the library genuinely has nothing.
            _hint, has_elsewhere = await scoped_miss_status(db, query, scope)
            empty_reason = "scoped_miss" if has_elsewhere else "no_match"
        else:
            empty_reason = "no_match"
    hints = await _miss_hints(db, query, scope, filters, results)
    return _envelope(results, empty_reason, hints)
