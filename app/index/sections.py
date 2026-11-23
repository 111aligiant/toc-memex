from __future__ import annotations

import re
from typing import Any

import jieba
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..identifiers import DOCUMENT_ID_RE, normalize_identifier
from ..models import Chunk, Document, TocNode
from .activity import activity_write
from .collections import collections_by_doc

# Section numbers we are willing to route directly:
#   8.3 / 26.6.4.7.1 / 第8章 / 附录A / Annex B / §8.3
_SECTION_RE = re.compile(
    r"第\s*[0-9一二三四五六七八九十百零〇]+\s*章"
    r"|(?:附录|Annex|Appendix)\s*[A-Za-z]"
    r"|\d+(?:\.\d+){1,4}"
    r"|§\s*\d+(?:\.\d+)*",
    re.IGNORECASE,
)

_PURE_SECTION_RE = re.compile(
    r"^[§\s]*(?:"
    r"第\s*[0-9一二三四五六七八九十百零〇]+\s*章"
    r"|(?:附录|Annex|Appendix)\s*[A-Za-z]"
    r"|\d+(?:\.\d+)*"
    r")\s*[条节]?$",
    re.IGNORECASE,
)

_TITLE_PREFIX_RE = re.compile(
    r"^(?:"
    r"第\s*[0-9一二三四五六七八九十百零〇]+\s*章"
    r"|(?:附录|Annex|Appendix)\s*[A-Za-z]"
    r"|\d+(?:\.\d+)*"
    r")\s*",
    re.IGNORECASE,
)


def _title_body(value: str) -> str:
    """Strip a leading section number: ``4.5 温度记录`` -> ``温度记录``."""
    return _TITLE_PREFIX_RE.sub("", (value or "").strip()).strip()


def title_echo(query: str, title: str | None, min_chars: int = 4) -> bool:
    """True when the query repeats a section's title verbatim.

    A query that spells out a heading — "4.2 薄膜介电性能与压电响应的研究",
    or an English heading followed by a Chinese question — names that
    section itself. This is a language-neutral signal: it says nothing about
    which document or language the rest of the query uses. Short titles
    ("实验", "目录") are ignored; they are too common to tell sections apart.
    """
    body = _normalize_text(_title_body(title or "")).lower()
    if len(body) < min_chars:
        return False
    return body in _normalize_text(query).lower()


def normalize_section_number(value: str | None) -> str | None:
    """Normalize ``§8.3`` / ``第 8 章`` / ``8.3条`` to a comparable key."""
    if not value:
        return None
    text = value.strip().replace("．", ".").replace("。", ".")
    text = re.sub(r"^[§\s]+", "", text)
    text = re.sub(r"\s+", "", text)

    match = re.match(r"^第([0-9一二三四五六七八九十百零〇]+)章", text)
    if match:
        return f"第{match.group(1)}章"

    match = re.match(r"^(附录|Annex|Appendix)([A-Za-z])", text, re.IGNORECASE)
    if match:
        prefix, letter = match.group(1), match.group(2).upper()
        if prefix.lower() in {"annex", "appendix"}:
            return f"{prefix.capitalize()}{letter}"
        return f"附录{letter}"

    match = re.match(r"^(\d+(?:\.\d+)*)", text)
    if match:
        return match.group(1)

    match = re.match(r"^([A-Z](?:\.\d+)*)", text)
    if match:
        return match.group(1)

    return None


def extract_section_numbers(query: str) -> list[str]:
    numbers: list[str] = []
    for candidate in _SECTION_RE.findall(query or ""):
        normalized = normalize_section_number(candidate)
        if normalized and normalized not in numbers:
            numbers.append(normalized)
    return numbers


def is_pure_section_query(query: str) -> bool:
    """True for queries like ``8.3`` or ``第8章``, false for ``8.3 方法``."""
    return bool(_PURE_SECTION_RE.match((query or "").strip()))


# Document identifier in a query: "ACME 2024", "RPT-17",
# "ACME/TS 100". The pattern is generic (see ``app.identifiers``); it is
# distinguished from section numbers by the Latin prefix.
DOC_ID_RE = DOCUMENT_ID_RE


async def doc_ids_from_query(
    db: AsyncSession, query: str
) -> list[str] | None:
    """Resolve document identifiers in the query to document IDs.

    Returns None when the query names no document (search the whole library);
    an empty list when it names one but no document matches (a miss should
    not silently degrade to a whole-library search full of wrong-doc noise).
    """
    identifiers = DOC_ID_RE.findall(query or "")
    if not identifiers:
        return None

    candidates = set()
    for ident in identifiers:
        norm = normalize_identifier(ident)
        candidates.add(norm)
        # Drop a trailing letter suffix so "150.16A" also matches "150.16"
        # titles, and a leading letter prefix so "ACME-100" also matches
        # titles that write "ACME-100".
        stripped = re.sub(r"[A-Z]+$", "", norm)
        if stripped:
            candidates.add(stripped)
        no_prefix = re.sub(r"^[A-Z]+", "", norm)
        if len(no_prefix) >= 5:
            candidates.add(no_prefix)

    docs = (await db.execute(select(Document))).scalars().all()
    # Longest-candidate-wins: "ACME 100.1" also yields candidate "ACME100.1",
    # which is a substring of the ACME100.16 title; only documents matching
    # the longest candidate ("ACME100.1") survive.
    best_len = 0
    matched: list[str] = []
    for doc in docs:
        title_norm = normalize_identifier(doc.title)
        doc_best = 0
        for candidate in candidates:
            if len(candidate) < 5:
                continue
            pos = title_norm.find(candidate)
            while pos != -1:
                end = pos + len(candidate)
                # "ACME100.1" must not match inside "ACME100.17": the char
                # right after the candidate must not continue the number.
                if end >= len(title_norm) or not title_norm[end].isdigit():
                    doc_best = max(doc_best, len(candidate))
                    break
                pos = title_norm.find(candidate, pos + 1)
        if doc_best == 0:
            continue
        if doc_best > best_len:
            best_len = doc_best
            matched = [doc.id]
        elif doc_best == best_len:
            matched.append(doc.id)
    return matched


async def find_sections_by_number(
    db: AsyncSession,
    number: str,
    doc_ids: list[str] | None = None,
) -> list[TocNode]:
    stmt = select(TocNode)
    if doc_ids:
        stmt = stmt.where(TocNode.doc_id.in_(doc_ids))
    nodes = (await db.execute(stmt)).scalars().all()

    matches: list[TocNode] = []
    for node in nodes:
        if normalize_section_number(node.heading_num) == number:
            matches.append(node)
            continue
        if normalize_section_number(node.title) == number:
            matches.append(node)

    matches.sort(
        key=lambda node: (
            node.chunk_start is None,
            node.chunk_start if node.chunk_start is not None else 10**9,
            node.level,
            node.node_id,
        )
    )
    return matches


def _node_order_key(node: TocNode) -> tuple[int, int]:
    """Reading order: page first, then the autoincrement id (same-page tie)."""
    page = node.page_start if node.page_start is not None else 10**9
    return (page, node.id)


def _filter_page_range(
    node: TocNode,
    chunks: list[Chunk],
    all_nodes: list[TocNode],
    subtree_ids: set[str],
) -> list[Chunk]:
    """Keep only chunks that can plausibly belong to ``node``.

    Page-range fallback is a heuristic: when a heading sits on the first
    line of a page, its text was often assigned to the previous section.
    Allow unassigned chunks, the target subtree, ancestors, preceding
    siblings, and unknown nodes that start no later than the target page.
    Reject following siblings, neighbouring chapters, and unknown nodes
    that start after this section.
    """
    by_node_id = {item.node_id: item for item in all_nodes}
    target_order = _node_order_key(node)

    ancestors: set[str] = set()
    parent_id = node.parent_id
    seen: set[str] = set()
    while parent_id and parent_id not in seen:
        seen.add(parent_id)
        ancestors.add(parent_id)
        parent = by_node_id.get(parent_id)
        parent_id = parent.parent_id if parent is not None else None

    preceding_ids = {
        item.node_id
        for item in all_nodes
        if item.parent_id == node.parent_id
        and item.node_id != node.node_id
        and _node_order_key(item) < target_order
    }

    allowed: list[Chunk] = []
    for chunk in chunks:
        chunk_node_id = chunk.node_id
        if chunk_node_id is None:
            allowed.append(chunk)
            continue
        if (
            chunk_node_id in subtree_ids
            or chunk_node_id in ancestors
            or chunk_node_id in preceding_ids
        ):
            allowed.append(chunk)
            continue
        if chunk_node_id in by_node_id:
            # A known node we did not accept above: following sibling or
            # neighbouring chapter. Never treat its body as this section's.
            continue
        # Unknown node (no TOC entry). The common cause is a heading at the
        # page top whose text was cut to the previous section, so it starts
        # no later than this section. Starting after it is more likely the
        # next content.
        if (
            node.page_start is not None
            and chunk.page_start is not None
            and chunk.page_start <= node.page_start
        ):
            allowed.append(chunk)
    return allowed


async def chunks_for_node_with_fallback(
    db: AsyncSession, doc_id: str, node: TocNode
) -> tuple[list[Chunk], str | None]:
    """Return ``(chunks, fallback)`` for a section.

    ``fallback`` is ``"chunk_range"``, ``"subtree"``, ``"page_range"`` or
    ``None``. The page range covers TOC nodes whose text was assigned to a
    neighbouring section during chunking; see ``_filter_page_range`` for
    which chunks that fallback accepts.
    """
    if node.chunk_start is not None and node.chunk_end is not None:
        stmt = (
            select(Chunk)
            .where(
                Chunk.doc_id == doc_id,
                Chunk.chunk_index >= node.chunk_start,
                Chunk.chunk_index <= node.chunk_end,
            )
            .order_by(Chunk.chunk_index)
        )
        chunks = list((await db.execute(stmt)).scalars().all())
        if chunks:
            return chunks, "chunk_range"

    all_nodes = (
        (await db.execute(select(TocNode).where(TocNode.doc_id == doc_id)))
        .scalars()
        .all()
    )
    children: dict[str, list[str]] = {}
    for item in all_nodes:
        if item.parent_id:
            children.setdefault(item.parent_id, []).append(item.node_id)

    subtree_ids: set[str] = set()
    stack = [node.node_id]
    while stack:
        current = stack.pop()
        if current in subtree_ids:
            continue
        subtree_ids.add(current)
        stack.extend(children.get(current, []))

    stmt = (
        select(Chunk)
        .where(Chunk.doc_id == doc_id, Chunk.node_id.in_(subtree_ids))
        .order_by(Chunk.chunk_index)
    )
    chunks = list((await db.execute(stmt)).scalars().all())
    if chunks:
        return chunks, "subtree"

    if node.page_start is None or node.page_end is None:
        return [], None

    stmt = (
        select(Chunk)
        .where(
            Chunk.doc_id == doc_id,
            Chunk.page_start.is_not(None),
            Chunk.page_end.is_not(None),
            Chunk.page_start <= node.page_end,
            Chunk.page_end >= node.page_start,
        )
        .order_by(Chunk.chunk_index)
    )
    candidates = list((await db.execute(stmt)).scalars().all())
    return _filter_page_range(node, candidates, all_nodes, subtree_ids), "page_range"


async def chunks_for_node(
    db: AsyncSession, doc_id: str, node: TocNode
) -> list[Chunk]:
    """Return chunks belonging to a section, including its subtree.

    Thin wrapper over :func:`chunks_for_node_with_fallback` that drops the
    fallback label.
    """
    chunks, _ = await chunks_for_node_with_fallback(db, doc_id, node)
    return chunks


def _page_bounds(
    node: TocNode | None, chunks: list[Chunk]
) -> tuple[int | None, int | None]:
    page_start = node.page_start if node is not None else None
    page_end = node.page_end if node is not None else None

    chunk_starts = [chunk.page_start for chunk in chunks if chunk.page_start is not None]
    chunk_ends = [chunk.page_end for chunk in chunks if chunk.page_end is not None]
    if page_start is None and chunk_starts:
        page_start = min(chunk_starts)
    if page_end is None and chunk_ends:
        page_end = max(chunk_ends)
    return page_start, page_end


def _node_to_hit(
    node: TocNode, chunks: list[Chunk], match_type: str, score: float
) -> dict[str, Any]:
    page_start, page_end = _page_bounds(node, chunks)
    if chunks:
        chunk_id = chunks[0].id
        chunk_ids = [chunk.id for chunk in chunks]
        snippet = chunks[0].content.strip()[:240]
        chunk_page_start = chunks[0].page_start
        chunk_page_end = chunks[-1].page_end
    else:
        chunk_id = None
        chunk_ids = []
        snippet = node.title
        chunk_page_start = None
        chunk_page_end = None

    return {
        "chunk_id": chunk_id,
        "chunk_ids": chunk_ids,
        "doc_id": node.doc_id,
        "node_id": node.node_id,
        "heading_num": node.heading_num,
        "heading_path": node.heading_path,
        "title": node.title,
        "page_start": page_start,
        "page_end": page_end,
        "chunk_page_start": chunk_page_start,
        "chunk_page_end": chunk_page_end,
        "snippet": snippet,
        "score": score,
        "match_type": match_type,
    }


async def section_hits(
    db: AsyncSession,
    numbers: list[str],
    doc_ids: list[str] | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Exact section hits, used before BM25 for ``8.3`` style queries."""
    hits: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for number in numbers:
        for node in await find_sections_by_number(db, number, doc_ids=doc_ids):
            key = (node.doc_id, node.node_id)
            if key in seen:
                continue
            seen.add(key)

            chunks = await chunks_for_node(db, node.doc_id, node)
            hits.append(_node_to_hit(node, chunks, "section", -1000.0))
            if len(hits) >= limit:
                return hits

    return hits


def _normalize_text(value: str) -> str:
    """Normalize for TOC title/path matching; keep dots for section numbers."""
    return re.sub(r"[\s>›·:：,，。、;；\-—–_/\\|]+", "", (value or "").lower())


_QUERY_STOPWORDS = {
    "什么", "哪些", "哪个", "哪里", "怎么", "怎样", "如何", "多少", "是否",
    "包括", "进行", "可以", "需要", "应该", "不应", "超过", "以上", "以下",
    "请问", "一下", "相关", "关于", "对于", "以及", "还有", "几类", "几种",
    "几个", "条件",
}


def _query_tokens(query: str) -> list[str]:
    tokens: list[str] = []
    for token in jieba.lcut(query or ""):
        token = token.strip().lower()
        if len(token) < 2 or token.isdigit():
            continue
        if token in _QUERY_STOPWORDS:
            continue
        if token not in tokens:
            tokens.append(token)
    return tokens


def _title_query_groups(query: str) -> list[list[str]]:
    """Query token groups for title matching: a group matches when ANY of
    its members appears in the heading path.

    A translated document may title its chapters in another language, so an
    English query otherwise matches no title at all. Each token brings its
    configured synonyms as alternatives; the default map is empty.
    """
    from .fts import term_synonyms  # noqa: PLC0415  # avoid import cycle

    groups: list[list[str]] = []
    for token in _query_tokens(query):
        group = [token]
        for synonym in term_synonyms(token):
            group.append(synonym.lower())
        groups.append(group)
    return groups


async def title_hits(
    db: AsyncSession,
    query: str,
    doc_ids: list[str] | None = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """TOC-title channel: every query token group must hit the heading path.

    This is what makes ``温度记录`` resolve to ``4.5 温度记录`` even when
    BM25 prefers shorter chunks that mention the words separately.
    """
    groups = _title_query_groups(query)
    if len(groups) < 2:
        return []

    stmt = select(TocNode)
    if doc_ids:
        stmt = stmt.where(TocNode.doc_id.in_(doc_ids))
    nodes = (await db.execute(stmt)).scalars().all()

    query_text = _normalize_text(_title_body(query))
    by_id = {node.node_id: node for node in nodes}
    scored: list[tuple[int, int, int, float, float, int, int, int, TocNode]] = []
    for node in nodes:
        path = _normalize_text(f"{node.heading_path} {node.title}").lower()
        if not path:
            continue
        title_body = _normalize_text(_title_body(node.title))
        prefix = 1 if title_body and query_text.lower().startswith(title_body) else 0
        prefix_len = len(title_body) if prefix else 0
        matched = sum(
            1
            for group in groups
            if any(token in path for token in group)
        )
        coverage = matched / len(groups)
        if coverage < 1.0 and not (prefix and coverage >= 0.66):
            continue
        parent = by_id.get(node.parent_id) if node.parent_id else None
        parent_text = _normalize_text(parent.title) if parent is not None else ""
        parent_coverage = (
            sum(
                1
                for group in groups
                if any(token in parent_text for token in group)
            )
            / len(groups)
            if parent_text
            else 0.0
        )
        exact = 1 if query_text == title_body else 0
        scored.append(
            (
                exact,
                prefix,
                prefix_len,
                round(coverage, 2),
                round(parent_coverage, 2),
                -len(path),
                node.level,
                1 if node.chunk_start is not None else 0,
                node,
            )
        )

    # Prefer the highest matching ancestor. A child section repeats its
    # parent's heading path, so "6.3.4 说明" would otherwise outrank
    # "6.3 方法" for the query "方法说明".
    candidate_by_id = {item[8].node_id: item for item in scored}
    kept: list[tuple[int, int, int, float, float, int, int, int, TocNode]] = []
    for item in scored:
        node = item[8]
        parent_id = node.parent_id
        redundant = False
        seen_ids: set[str] = set()
        while parent_id and parent_id not in seen_ids:
            seen_ids.add(parent_id)
            ancestor = candidate_by_id.get(parent_id)
            if (
                ancestor is not None
                and ancestor[3] >= item[3]
                and ancestor[1] >= item[1]
            ):
                redundant = True
                break
            parent = by_id.get(parent_id)
            parent_id = parent.parent_id if parent is not None else None
        if not redundant:
            kept.append(item)
    scored = kept

    scored.sort(key=lambda item: item[:8], reverse=True)

    # Ambiguous prefix titles are common in technical documents ("概述"
    # appears in both definitions and requirements). Let BM25 decide instead
    # of forcing the first path-order candidate.
    if scored and scored[0][0] == 0:
        top_prefix_len = scored[0][2]
        same_prefix = sum(
            1 for item in scored if item[1] == 1 and item[2] == top_prefix_len
        )
        if same_prefix >= 2:
            return []

    hits: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for *_, node in scored:
        key = (node.doc_id, node.node_id)
        if key in seen:
            continue
        seen.add(key)
        chunks = await chunks_for_node(db, node.doc_id, node)
        hits.append(_node_to_hit(node, chunks, "title", -500.0))
        if len(hits) >= limit:
            break

    return hits


async def _fallback_node(
    db: AsyncSession, doc_id: str, node_id: str
) -> TocNode | None:
    """Recover a node for an id the agent built from a section number.

    Agents sometimes write ``{doc_id}:h:{number}`` themselves instead of
    copying an id from search/get_toc, and a number that only appears as a
    paragraph inside a bigger section (``4.2.1`` under ``4.2``) has no node.
    Two recoveries, cheapest first: an exact number match (also covers ids
    de-duplicated to ``...:h:4.2.1@6``), then the deepest numbered ancestor
    — reading ``4.2`` still contains the ``4.2.1`` paragraph.
    """
    prefix = f"{doc_id}:h:"
    if not node_id.startswith(prefix):
        return None
    number = normalize_section_number(node_id[len(prefix):])
    if not number:
        return None

    matches = await find_sections_by_number(db, number, doc_ids=[doc_id])
    if matches:
        return matches[0]

    nodes = (
        await db.execute(select(TocNode).where(TocNode.doc_id == doc_id))
    ).scalars().all()
    best: TocNode | None = None
    best_len = 0
    for node in nodes:
        ancestor = normalize_section_number(node.heading_num)
        if not ancestor or not number.startswith(f"{ancestor}."):
            continue
        if len(ancestor) > best_len:
            best, best_len = node, len(ancestor)
    return best


async def read_section(
    db: AsyncSession,
    doc_id: str,
    node_id: str | None = None,
    page_start: int | None = None,
    page_end: int | None = None,
    max_chars: int = 8000,
    cursor: int = 0,
    reader_agent: str | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Read a whole section, with section-first citation metadata.

    Every successful read is auto-logged as a "read" activity (with
    reader_agent when the caller identifies itself), so "which agent used
    which material, when, under which task" is answerable from the log.
    """
    node: TocNode | None = None
    resolved_from: str | None = None
    fallback: str | None = None
    page_mode = False

    if node_id:
        from ..curation.identity import resolve_node_id

        node_id = await resolve_node_id(db, doc_id, node_id)
        node = (
            await db.execute(
                select(TocNode).where(
                    TocNode.doc_id == doc_id, TocNode.node_id == node_id
                )
            )
        ).scalar_one_or_none()
        if node is None:
            requested = node_id
            node = await _fallback_node(db, doc_id, node_id)
            if node is None:
                raise ValueError(
                    f"section not found: {doc_id}/{node_id}"
                    "（node_id 必须原样取自 search/get_toc 的返回；"
                    "可用 get_toc 查看现有节点）"
                )
            resolved_from = requested
        chunks, fallback = await chunks_for_node_with_fallback(db, doc_id, node)
        heading_num = node.heading_num
        title = node.title
        heading_path = node.heading_path
    elif page_start is not None and page_end is not None:
        stmt = (
            select(Chunk)
            .where(
                Chunk.doc_id == doc_id,
                Chunk.page_start.is_not(None),
                Chunk.page_end.is_not(None),
                Chunk.page_start <= page_end,
                Chunk.page_end >= page_start,
            )
            .order_by(Chunk.chunk_index)
        )
        chunks = list((await db.execute(stmt)).scalars().all())
        heading_num = None
        title = f"pages {page_start}-{page_end}"
        heading_path = title
        fallback = "page_range"
        page_mode = True
    else:
        raise ValueError("node_id or page_start/page_end is required")

    if page_mode:
        citation_confidence: str | None = "page"
        citation_note: str | None = None
    elif fallback == "page_range":
        citation_confidence = "low"
        if chunks:
            citation_note = (
                "该节没有归属切片，已按页码兜底读取；只包含前文或同页切片，"
                "可能不完整，引用前请核对原文页码。"
            )
        else:
            citation_note = "该节没有可归属切片，页码兜底也未找到前文内容。"
    elif fallback in {"chunk_range", "subtree"}:
        citation_confidence = "high"
        citation_note = None
    else:
        citation_confidence = None
        citation_note = None

    # Keep each chunk's offset in the concatenated text so a truncated read
    # can still say which chunk each visible slice came from. The separator
    # is part of the concatenation but belongs to no chunk.
    parts: list[str] = []
    spans: list[tuple[Chunk, int, int]] = []
    offset = 0
    for chunk in chunks:
        text = chunk.content.strip()
        if not text:
            continue
        if parts:
            offset += 2
        start = offset
        offset += len(text)
        parts.append(text)
        spans.append((chunk, start, offset))
    full_text = "\n\n".join(parts)
    safe_start = max(0, min(cursor, len(full_text)))
    content = full_text[safe_start : safe_start + max(1, max_chars)]
    next_cursor = safe_start + len(content)
    if next_cursor >= len(full_text):
        next_cursor_value = None
    else:
        next_cursor_value = next_cursor

    window_end = safe_start + len(content)
    chunk_segments: list[dict[str, Any]] = []
    for chunk, start, end in spans:
        if end <= safe_start or start >= window_end:
            continue
        left = max(0, safe_start - start)
        right = min(end - start, window_end - start)
        chunk_segments.append(
            {
                "chunk_id": chunk.id,
                "node_id": chunk.node_id,
                "chunk_index": chunk.chunk_index,
                "page_start": chunk.page_start,
                "page_end": chunk.page_end,
                "heading_path": chunk.heading_path,
                "text": full_text[start + left : start + right],
            }
        )

    page_start_value, page_end_value = _page_bounds(node, chunks)
    if page_start_value is None:
        page_start_value = page_start
    if page_end_value is None:
        page_end_value = page_end

    if reader_agent:
        try:
            await activity_write(
                db,
                "read",
                reader_agent,
                task_id=task_id,
                doc_id=doc_id,
                node_id=node.node_id if node is not None else None,
                chunk_id=chunks[0].id if chunks else None,
                session_id=session_id,
            )
        except ValueError:
            # Logging must never break reading; a bad task_id etc. is the
            # caller's mistake to see elsewhere, not here.
            pass

    doc = await db.get(Document, doc_id)
    return {
        "doc_id": doc_id,
        "doc_title": doc.title if doc is not None else None,
        "node_id": node.node_id if node is not None else None,
        "resolved_from": resolved_from,
        "heading_num": heading_num,
        "title": title,
        "heading_path": heading_path,
        "level": node.level if node is not None else None,
        "page_start": page_start_value,
        "page_end": page_end_value,
        # R18: agent-facing page numbers are 1-based PDF pages; internal
        # page_start/page_end stay 0-based. Printed-page labels are not
        # stored yet, so they are always null this round.
        "pdf_page_start": (
            None if page_start_value is None else page_start_value + 1
        ),
        "pdf_page_end": None if page_end_value is None else page_end_value + 1,
        "printed_page_start": None,
        "printed_page_end": None,
        "fallback": fallback,
        "citation_confidence": citation_confidence,
        "citation_note": citation_note,
        "chunk_ids": [chunk.id for chunk in chunks],
        "chunks": chunk_segments,
        "char_count": len(full_text),
        "token_count": sum(chunk.token_count or 0 for chunk in chunks),
        "content": content,
        "next_cursor": next_cursor_value,
        "collection": (await collections_by_doc(db, [doc_id])).get(doc_id),
    }