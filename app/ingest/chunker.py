from __future__ import annotations

import re
from dataclasses import dataclass

from ..ocr.models import OcrBlock, OcrPage, ParsedDocument
from ..ocr.parser import SKIP_LABELS, _html_to_text
from .heading import parse_heading
from .printed_toc import toc_page_indices
from .toc import TocNodeData, _merge_adjacent_titles

# PaddleOCR labels footer page numbers as ``number``; they are furniture for
# chunk purposes (``page_number`` is already in SKIP_LABELS).
_SKIP_LABELS = set(SKIP_LABELS) | {"number"}

_HEADING_LABELS = {"paragraph_title", "doc_title"}
_TITLE_STRIP = re.compile(r"[\s:：,，。.、;；\-—–]+")


@dataclass
class ChunkData:
    chunk_index: int
    node_id: str | None
    content: str
    heading_path: str
    page_start: int
    page_end: int


def _sort_key(block: OcrBlock) -> tuple:
    y = block.bbox[1] if len(block.bbox) > 1 else 0.0
    x = block.bbox[0] if block.bbox else 0.0
    return (block.order is None, block.order or 0, y, x)


def _canon_num(value: str | None) -> str:
    return re.sub(r"\s+", "", value or "").lower()


def _canon_title(value: str | None) -> str:
    return _TITLE_STRIP.sub("", value or "").lower()


def _index_nodes(
    toc: list[TocNodeData],
) -> tuple[dict[str, list[tuple[int, TocNodeData]]], dict[str, list[tuple[int, TocNodeData]]]]:
    by_num: dict[str, list[tuple[int, TocNodeData]]] = {}
    by_title: dict[str, list[tuple[int, TocNodeData]]] = {}
    for index, node in enumerate(toc):
        if node.heading_num:
            by_num.setdefault(_canon_num(node.heading_num), []).append((index, node))
        # Index both the stored title and its number-stripped form: an
        # authored outline titles a section "Introduction" while the body
        # line prints "1 Introduction" (and vice versa).
        keys = {_canon_title(node.title)}
        parsed = parse_heading(node.title)
        if parsed is not None and parsed[1]:
            keys.add(_canon_title(parsed[1]))
        for key in keys:
            by_title.setdefault(key, []).append((index, node))
    return by_num, by_title


def _covers(node: TocNodeData, page_index: int) -> bool:
    return (
        node.page_start is not None
        and node.page_end is not None
        and node.page_start <= page_index <= node.page_end
    )


def _is_descendant(
    node: TocNodeData, ancestor: TocNodeData, by_id: dict[str, TocNodeData]
) -> bool:
    current = node
    seen: set[str] = set()
    while current.parent_id:
        if current.parent_id == ancestor.node_id:
            return True
        if current.node_id in seen:
            return False
        seen.add(current.node_id)
        parent = by_id.get(current.parent_id)
        if parent is None:
            return False
        current = parent
    return False


def _first_starting_node(
    toc: list[TocNodeData], page_index: int
) -> TocNodeData | None:
    """First section that starts on this page (TOC order = reading order)."""
    for node in toc:
        if node.page_start is not None and node.page_start == page_index:
            return node
    return None


def _active_node(toc: list[TocNodeData], page_index: int) -> TocNodeData | None:
    candidates = [node for node in toc if _covers(node, page_index)]
    if not candidates:
        return None
    latest = max(node.page_start for node in candidates if node.page_start is not None)
    for node in toc:
        if node.page_start == latest and _covers(node, page_index):
            return node
    return candidates[0]


def _match_heading(
    text: str,
    page_index: int,
    by_num: dict[str, list[tuple[int, TocNodeData]]],
    by_title: dict[str, list[tuple[int, TocNodeData]]],
) -> TocNodeData | None:
    """Find the TOC node a body heading block belongs to."""
    parsed = parse_heading(text)
    num = parsed[0] if parsed else None
    full_key = _canon_title(text)
    pool: list[tuple[int, TocNodeData]] = []
    if num:
        pool = by_num.get(_canon_num(num), [])
    if not pool:
        pool = by_title.get(full_key, [])
    short = _canon_title(parsed[1]) if parsed else ""
    if not pool and short:
        pool = by_title.get(short, [])
    if not pool:
        return None

    def score(item: tuple[int, TocNodeData]) -> tuple:
        _, node = item
        node_key = _canon_title(node.title)
        covers = 1 if _covers(node, page_index) else 0
        exact = 1 if node_key == full_key else 0
        contains = (
            1
            if short and (short in node_key or node_key in short)
            else 0
        )
        width = (
            node.page_end - node.page_start
            if node.page_end is not None and node.page_start is not None
            else 0
        )
        return (covers, exact, contains, node.confidence, -width)

    return max(pool, key=score)[1]


def chunk_ranges_by_node(
    toc: list[TocNodeData], chunks: list[ChunkData]
) -> dict[str, tuple[int, int]]:
    """Inclusive chunk-index range for every node, including its subtree.

    Parent chapters often own no direct chunk (their text lives under child
    sections), so navigation needs the range aggregated over descendants.
    """
    direct: dict[str, tuple[int, int]] = {}
    for chunk in chunks:
        if chunk.node_id is None:
            continue
        start, end = direct.get(
            chunk.node_id, (chunk.chunk_index, chunk.chunk_index)
        )
        direct[chunk.node_id] = (
            min(start, chunk.chunk_index),
            max(end, chunk.chunk_index),
        )

    children: dict[str, list[str]] = {}
    for node in toc:
        if node.parent_id:
            children.setdefault(node.parent_id, []).append(node.node_id)

    ranges: dict[str, tuple[int, int]] = {}

    def visit(node_id: str) -> tuple[int, int] | None:
        if node_id in ranges:
            return ranges[node_id]
        bounds = [direct[node_id]] if node_id in direct else []
        for child in children.get(node_id, []):
            child_range = visit(child)
            if child_range is not None:
                bounds.append(child_range)
        if not bounds:
            return None
        result = (min(start for start, _ in bounds), max(end for _, end in bounds))
        ranges[node_id] = result
        return result

    for node in toc:
        if node.parent_id is None:
            visit(node.node_id)
    return ranges


def _page_span(offsets: list[tuple[int, int]], start: int, end: int) -> tuple[int, int]:
    if not offsets:
        return 0, 0
    page_start = offsets[0][1]
    page_end = offsets[0][1]
    for offset, page_index in offsets:
        if offset <= start:
            page_start = page_index
        if offset < end:
            page_end = page_index
    return page_start, max(page_start, page_end)


def _split_segment(
    chunks: list[ChunkData],
    node: TocNodeData | None,
    parts: list[tuple[int, str]],
    max_chars: int,
    overlap: int,
    chunk_index: int,
) -> int:
    if not parts:
        return chunk_index

    full_parts: list[str] = []
    offsets: list[tuple[int, int]] = []
    pos = 0
    for page_index, text in parts:
        if full_parts:
            full_parts.append("\n\n")
            pos += 2
        offsets.append((pos, page_index))
        full_parts.append(text)
        pos += len(text)
    full = "".join(full_parts)

    start = 0
    while start < len(full):
        end = min(len(full), start + max_chars)
        if end < len(full):
            cut = full.rfind("\n", start, end)
            if cut <= start:
                cut = full.rfind(" ", start, end)
            if cut > start:
                end = cut
        content = full[start:end].strip()
        if content:
            page_start, page_end = _page_span(offsets, start, end)
            chunks.append(
                ChunkData(
                    chunk_index=chunk_index,
                    node_id=node.node_id if node else None,
                    content=content,
                    heading_path=node.heading_path if node else "",
                    page_start=page_start,
                    page_end=page_end,
                )
            )
            chunk_index += 1
        if end >= len(full):
            break
        start = max(start + 1, end - overlap)
    return chunk_index


def non_content_pages(
    parsed: ParsedDocument, toc: list[TocNodeData]
) -> set[int]:
    """Pages that must never become content chunks.

    Body content starts at the first located TOC node; everything before it
    is cover, copyright or blank front matter. Printed TOC pages anywhere in
    the document (including per-chapter detail TOCs) are navigation only.
    """
    starts = [node.page_start for node in toc if node.page_start is not None]
    body_start = min(starts) if starts else 0
    body_start = max(0, min(body_start, parsed.page_count))
    return set(range(body_start)) | toc_page_indices(parsed.pages)


def chunk_document(
    parsed: ParsedDocument,
    toc: list[TocNodeData],
    max_chars: int = 1200,
    overlap: int = 100,
) -> list[ChunkData]:
    """Split content at body headings, then by size.

    A page can contain several sections; assigning the whole page to the
    deepest TOC node loses the earlier sections. Instead, walk each page's
    blocks in reading order and switch nodes whenever a ``paragraph_title``
    matches a TOC entry. Text before the first heading on a page stays with
    the section carried over from the previous page.

    Front matter and TOC pages are skipped entirely: they are navigation
    material, not body content, and must not enter the chunk index.
    """
    max_chars = max(1, max_chars)
    overlap = max(0, min(overlap, max_chars - 1))

    by_id = {node.node_id: node for node in toc}
    by_num, by_title = _index_nodes(toc)
    if (parsed.metadata or {}).get("format") in ("xlsx", "csv"):
        # A sheet full of rows can resemble a printed TOC to the dot-leader
        # heuristic; tables have no front matter or TOC pages, so skip none.
        skip_pages: set[int] = set()
    else:
        skip_pages = non_content_pages(parsed, toc)
    segments: list[tuple[TocNodeData | None, list[tuple[int, str]]]] = []
    current_node: TocNodeData | None = None
    current_parts: list[tuple[int, str]] = []

    def flush() -> None:
        nonlocal current_parts
        if not current_parts:
            return
        if segments and segments[-1][0] is current_node:
            segments[-1][1].extend(current_parts)
        else:
            segments.append((current_node, current_parts))
        current_parts = []

    for page in parsed.pages:
        if page.page_index in skip_pages:
            continue
        blocks = _merge_adjacent_titles(sorted(page.blocks, key=_sort_key))
        if not blocks:
            continue

        starting = _first_starting_node(toc, page.page_index)
        # A page that opens with the new section's heading switches there,
        # block by block. Switching at the page boundary instead would hand
        # a section's tail (the lines above the next heading) to the next
        # section. If the new section's heading is missing from the text
        # (printed-TOC page mapping, OCR miss), keep the old page-boundary
        # switch as the fallback.
        starting_heading_on_page = False
        if starting is not None:
            for block in blocks:
                if block.label not in _HEADING_LABELS:
                    continue
                matched = _match_heading(
                    block.content, page.page_index, by_num, by_title
                )
                if matched is not None and (
                    matched is starting
                    or _is_descendant(matched, starting, by_id)
                ):
                    starting_heading_on_page = True
                    break
        if starting is not None and (
            current_node is None
            or not _covers(current_node, page.page_index)
            or _is_descendant(starting, current_node, by_id)
        ):
            if not starting_heading_on_page and starting is not current_node:
                flush()
                current_node = starting
        elif current_node is None or not _covers(current_node, page.page_index):
            fallback = _active_node(toc, page.page_index)
            if fallback is not current_node:
                flush()
                current_node = fallback

        for block in blocks:
            if block.label in _SKIP_LABELS or block.is_empty:
                continue
            text = _html_to_text(block.content).strip()
            if not text:
                continue
            if block.label in _HEADING_LABELS:
                matched = _match_heading(
                    text, page.page_index, by_num, by_title
                )
                if matched is not None and matched is not current_node:
                    flush()
                    current_node = matched
            current_parts.append((page.page_index, text))
    flush()

    chunks: list[ChunkData] = []
    chunk_index = 0
    for node, parts in segments:
        chunk_index = _split_segment(
            chunks, node, parts, max_chars, overlap, chunk_index
        )
    return chunks