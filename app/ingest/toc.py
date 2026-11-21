from __future__ import annotations

import re
from dataclasses import dataclass

from vendor.pageindex.flash.embedded_toc import bookmarks_to_structure, validate_bookmarks
from vendor.pageindex.page_index_md import (
    build_tree_from_nodes,
    extract_node_text_content,
    extract_nodes_from_markdown,
)

from ..config import Settings, get_settings
from ..ocr.models import OcrPage, ParsedDocument
from ..ocr.parser import _html_to_text, page_text
from .bookmarks import filter_bookmarks
from .heading import parse_heading
from .llm_toc import detect_printed_toc_llm
from .printed_toc import PrintedToc, TocEntry, detect_printed_toc, heading_sort_key

_SKIP_LABELS = {"header", "footer", "page_number", "aside_text"}
_PURE_NUM = re.compile(r"^\d+(?:\.\d+)*$")


def _merge_adjacent_titles(blocks: list) -> list:
    """Merge OCR blocks where a bare heading number is split from its title.

    PaddleOCR often emits ``3.1`` and ``最大负载循环 maximum duty cycle`` as two
    consecutive paragraph_title blocks. Joining them restores one heading.
    """
    out = []
    index = 0
    while index < len(blocks):
        current = blocks[index]
        merged = False
        if current.label == "paragraph_title" and _PURE_NUM.match(
            current.content.strip()
        ):
            # Prefer a paragraph_title within the next three blocks: the
            # title may be separated from its number by the section's
            # definition text (seen in PaddleOCR layout output).
            next_index = None
            for candidate_index in range(index + 1, min(index + 4, len(blocks))):
                candidate = blocks[candidate_index]
                if candidate.label != "paragraph_title":
                    continue
                candidate_text = candidate.content.strip()
                if not candidate_text or _PURE_NUM.match(candidate_text):
                    continue
                if (
                    abs(candidate.bbox[1] - current.bbox[1]) <= 70
                    and len(candidate_text) <= 120
                    and not candidate_text.endswith(("。", "；", "，", ".", ";"))
                ):
                    next_index = candidate_index
                    break

            # Text-layer/OCR output can label the split title as ``text``.
            # Accept only the immediately following short, title-like line.
            if next_index is None and index + 1 < len(blocks):
                candidate = blocks[index + 1]
                candidate_text = candidate.content.strip()
                if (
                    candidate.label == "text"
                    and candidate_text
                    and not _PURE_NUM.match(candidate_text)
                    and len(candidate_text) <= 60
                    and not candidate_text.endswith(("。", "；", "，", ".", ";"))
                    and not re.match(
                        r"^[本应该按如对在使将由其各这那下上用]", candidate_text
                    )
                    and abs(candidate.bbox[1] - current.bbox[1]) <= 70
                ):
                    next_index = index + 1

            if next_index is not None:
                candidate = blocks[next_index]
                candidate_text = candidate.content.strip()
                out.append(
                    type(current)(
                        label="paragraph_title",
                        content=f"{current.content.strip()} {candidate_text}",
                        bbox=(
                            current.bbox[0],
                            current.bbox[1],
                            max(current.bbox[2], candidate.bbox[2]),
                            max(current.bbox[3], candidate.bbox[3]),
                        ),
                        order=current.order,
                    )
                )
                out.extend(blocks[index + 1 : next_index])
                index = next_index + 1
                merged = True
        if merged:
            continue
        out.append(current)
        index += 1
    return out


@dataclass
class TocNodeData:
    node_id: str
    parent_id: str | None
    level: int
    heading_num: str | None
    title: str
    heading_path: str
    page_start: int | None
    page_end: int | None
    source: str
    confidence: float


_UNNUMBERED_TOP_LEVEL = {
    "前言",
    "引言",
    "参考文献",
    "索引",
    "缩略语",
    "目录",
    "目 录",
}


def _blocks_to_markdown(pages: list[OcrPage]) -> tuple[str, dict[int, int]]:
    """Render OCR blocks as Markdown headings + body, tracking line -> page."""
    lines: list[str] = []
    line_to_page: dict[int, int] = {}
    last_level = 1

    def add(line: str, page_index: int) -> None:
        lines.append(line)
        line_to_page[len(lines)] = page_index

    for page in pages:
        add(f"<!-- page:{page.page_index} -->", page.page_index)
        ordered = sorted(
            page.blocks,
            key=lambda b: (b.order is None, b.order or 0, b.bbox[1], b.bbox[0]),
        )
        ordered = _merge_adjacent_titles(ordered)
        for block in ordered:
            if block.label in _SKIP_LABELS or block.is_empty:
                continue
            content = _html_to_text(block.content).strip()
            if not content:
                continue
            if block.label == "paragraph_title":
                parsed = parse_heading(content)
                num = parsed[0] if parsed else None
                if num is not None:
                    level = max(1, min(6, parsed[2]))
                elif "".join(content.split()) in _UNNUMBERED_TOP_LEVEL:
                    level = 1
                elif last_level == 1:
                    # Appendix subtitle or an unnumbered section directly
                    # under a chapter: keep it as a child, not a new root.
                    level = 2
                else:
                    level = last_level
                last_level = level
                add(f"{'#' * level} {content}", page.page_index)
            else:
                for line in content.splitlines():
                    if line.strip():
                        add(line.strip(), page.page_index)
        add("", page.page_index)

    return "\n".join(lines), line_to_page


def _convert_page_tree(
    doc_id: str,
    roots: list[dict],
    n_pages: int,
    source: str,
    confidence: float,
) -> list[TocNodeData]:
    def walk(nodes: list[dict], parent_id: str | None, parent_path: str, depth: int):
        out: list[TocNodeData] = []
        for node in nodes:
            title = str(node.get("title") or "").strip()
            if not title:
                continue
            node_id = f"{doc_id}:{node.get('node_id') or len(out)}"
            path = f"{parent_path} > {title}" if parent_path else title
            start = max(0, int(node.get("start_index") or 1) - 1)
            end = max(start, int(node.get("end_index") or (start + 1)) - 1)
            num, _, _ = parse_heading(title) or (None, title, 1)
            out.append(
                TocNodeData(
                    node_id=node_id,
                    parent_id=parent_id,
                    level=depth,
                    heading_num=num,
                    title=title,
                    heading_path=path,
                    page_start=start,
                    page_end=min(end, n_pages - 1),
                    source=source,
                    confidence=confidence,
                )
            )
            out.extend(walk(node.get("nodes") or [], node_id, path, depth + 1))
        return out

    return walk(roots, None, "", 1)


def _fix_page_ranges(nodes: list[TocNodeData], n_pages: int) -> list[TocNodeData]:
    """Recompute end pages from pre-order TOC order.

    PageIndex's bookmark finalizer uses the next node's start as the end
    boundary, which overlaps by one page once converted to inclusive ranges.
    Nodes without a located page keep ``None`` (e.g. appendices listed in a
    master TOC but absent from the PDF); they are skipped as boundaries.
    """
    last_page = max(n_pages - 1, 0)
    by_id = {node.node_id: node for node in nodes}
    for index, node in enumerate(nodes):
        if node.page_start is None:
            node.page_end = None
            continue
        next_start = next(
            (
                later.page_start
                for later in nodes[index + 1 :]
                if later.level <= node.level and later.page_start is not None
            ),
            None,
        )
        node.page_end = max(
            node.page_start,
            (next_start - 1) if next_start is not None else last_page,
        )

    # A child never extends past its parent's boundary.
    for node in nodes:
        if node.page_start is None or node.page_end is None:
            continue
        parent = by_id.get(node.parent_id) if node.parent_id else None
        if parent is not None and parent.page_end is not None:
            node.page_end = min(node.page_end, parent.page_end)
        node.page_end = max(node.page_start, node.page_end)

    # A parent covers at least its children, including a child whose
    # mapped page is earlier than the parent's own start page.
    for node in reversed(nodes):
        children = [
            child
            for child in nodes
            if child.parent_id == node.node_id
            and child.page_start is not None
            and child.page_end is not None
        ]
        if children:
            starts = [child.page_start for child in children if child.page_start is not None]
            ends = [child.page_end for child in children if child.page_end is not None]
            if node.page_start is None:
                node.page_start = min(starts)
            else:
                node.page_start = min([node.page_start] + starts)
            node.page_end = max([node.page_end or 0] + ends)
    return nodes


def _from_bookmarks(
    doc_id: str,
    parsed: ParsedDocument,
    bookmarks: list | None = None,
) -> list[TocNodeData]:
    source = bookmarks if bookmarks is not None else parsed.bookmarks
    entries = [
        {"title": b.title, "level": b.level, "page": b.page_index + 1}
        for b in source
    ]
    entries = validate_bookmarks(entries, parsed.page_count)
    roots = bookmarks_to_structure(entries, parsed.page_count)
    nodes = _convert_page_tree(
        doc_id, roots, parsed.page_count, "bookmark", 1.0
    )
    return _fix_page_ranges(nodes, parsed.page_count)


def _heading_key(heading_num: str | None) -> tuple | None:
    if not heading_num:
        return None
    key = heading_sort_key(heading_num)
    return None if key == (9999,) else key


def _disambiguate_appendix_numbers(nodes: list[TocNodeData]) -> None:
    """Give same-numbered appendices a letter suffix from their titles.

    A document can print two appendices both numbered ``附录N``, told apart
    by a letter the heading regex leaves in the title::

        附录N A（资料性附录） 附录 A 参数
        附录N B（资料性附录） 附录 A 中规定…

    ``parse_heading`` reads both as ``附录N``, so the tree carries a duplicate
    sibling number and ``validate_toc`` warns. Rewrite the number to
    ``附录NA`` / ``附录NB`` after nesting, leaving titles untouched.

    Only entries that actually collide are rewritten, and only when the
    title starts with a lone letter. A suffix that another sibling already
    owns is skipped, so this can never create a new duplicate.
    """
    groups: dict[str | None, list[TocNodeData]] = {}
    for node in nodes:
        groups.setdefault(node.parent_id, []).append(node)

    for siblings in groups.values():
        by_number: dict[str, list[TocNodeData]] = {}
        for node in siblings:
            if node.heading_num:
                by_number.setdefault(node.heading_num, []).append(node)
        for number, duplicates in by_number.items():
            if len(duplicates) < 2:
                continue
            used = {node.heading_num for node in siblings}
            for node in duplicates:
                suffix = _appendix_title_suffix(node.title, number)
                if suffix is None:
                    continue
                candidate = f"{number}{suffix}"
                if candidate in used:
                    continue
                node.heading_num = candidate
                used.add(candidate)


_APPENDIX_SUFFIX = re.compile(r"^([A-Z])(?![A-Za-z0-9.])")


def _appendix_title_suffix(title: str, number: str) -> str | None:
    """The lone leading letter of an appendix title, if it has one.

    ``parse_heading`` strips the ``附录N`` prefix but keeps ``A（资料性附录）``
    in the title; that letter is the real discriminator. ``title`` here is
    the node's full title, so it is parsed first.
    """
    if not re.match(r"^(?:附录|Annex|Appendix)", number, re.IGNORECASE):
        return None
    parsed = parse_heading(title)
    rest = parsed[1] if parsed else title
    match = _APPENDIX_SUFFIX.match(rest.strip())
    return match.group(1) if match else None


def _nodes_from_printed_entries(
    doc_id: str,
    entries: list[TocEntry],
    n_pages: int,
    confidence: float,
) -> list[TocNodeData]:
    """Nest printed-TOC entries by their heading numbering.

    Page order alone is not a safe parent signal: a chapter's later
    subsections can be mapped to pages after the next chapter/appendix root
    (e.g. chapter 26 details on pages 291+ while appendices start on 290).
    Match the longest heading prefix instead: ``26.4`` → ``第26章``,
    ``A.1`` → ``附录A``.
    """
    keys = [_heading_key(entry.heading_num) for entry in entries]
    by_key: dict[tuple, int] = {}
    for index, key in enumerate(keys):
        if key is not None:
            by_key.setdefault(key, index)

    parents: list[int | None] = []
    for index, key in enumerate(keys):
        parent = None
        if key is not None:
            for size in range(len(key) - 1, 0, -1):
                candidate = by_key.get(key[:size])
                if candidate is not None and candidate != index:
                    parent = candidate
                    break
        parents.append(parent)

    children: dict[int, list[int]] = {}
    roots: list[int] = []
    for index, parent in enumerate(parents):
        if parent is None:
            roots.append(index)
        else:
            children.setdefault(parent, []).append(index)

    def sibling_key(index: int) -> tuple:
        entry = entries[index]
        return (
            keys[index] or (9999,),
            0 if entry.is_master else 1,
            entry.order,
        )

    def sort_siblings(indexes: list[int]) -> list[int]:
        """Reading order, unless the numbers contradict it.

        A printed TOC lists siblings in numeric order, and front matter
        (前言/摘要) sits before chapter 1 — reading order encodes both. But a
        two-column TOC read out of order (or one wrong page mapping) breaks
        that monotonicity; then the heading numbers are the only trustworthy
        order and the group is re-sorted by them.
        """
        numbered = [keys[i] for i in indexes if keys[i] is not None]
        monotonic = all(
            right >= left for left, right in zip(numbered, numbered[1:])
        )
        if numbered and not monotonic:
            return sorted(indexes, key=sibling_key)
        return sorted(indexes, key=lambda i: entries[i].order)

    nodes: list[TocNodeData] = []

    def walk(
        index: int, parent_id: str | None, parent_path: str, depth: int
    ) -> None:
        entry = entries[index]
        node_id = f"{doc_id}:{len(nodes):04d}"
        path = f"{parent_path} > {entry.title}" if parent_path else entry.title
        start = entry.page_index
        if start is not None and n_pages > 0:
            start = max(0, min(start, n_pages - 1))
        nodes.append(
            TocNodeData(
                node_id=node_id,
                parent_id=parent_id,
                level=depth,
                heading_num=entry.heading_num,
                title=entry.title,
                heading_path=path,
                page_start=start,
                page_end=start,
                source="toc_page",
                confidence=min(confidence, entry.confidence),
            )
        )
        for child in sort_siblings(children.get(index, [])):
            walk(child, node_id, path, depth + 1)

    for root in sort_siblings(roots):
        walk(root, None, "", 1)

    return nodes


def _from_printed_toc(doc_id: str, parsed: ParsedDocument) -> list[TocNodeData]:
    printed = detect_printed_toc(parsed)
    if printed is None or len(printed.entries) < 2:
        return []
    entries = printed.entries
    nodes = _nodes_from_printed_entries(
        doc_id, entries, parsed.page_count, printed.confidence
    )
    _disambiguate_appendix_numbers(nodes)
    return _fix_page_ranges(nodes, parsed.page_count)


def _from_markdown(doc_id: str, parsed: ParsedDocument) -> list[TocNodeData]:
    markdown, line_to_page = _blocks_to_markdown(parsed.pages)
    node_list, markdown_lines = extract_nodes_from_markdown(markdown)
    if not node_list:
        return []
    nodes = extract_node_text_content(node_list, markdown_lines)
    roots = build_tree_from_nodes(nodes)
    last_page = max(parsed.page_count - 1, 0)

    def walk(
        tree_nodes: list[dict],
        parent_id: str | None,
        parent_path: str,
        depth: int,
        boundary: int,
    ) -> list[TocNodeData]:
        out: list[TocNodeData] = []
        for index, node in enumerate(tree_nodes):
            title = str(node.get("title") or "").strip()
            if not title:
                continue
            start = line_to_page.get(int(node.get("line_num") or 0), 0)
            next_boundary = (
                line_to_page.get(
                    int(tree_nodes[index + 1].get("line_num") or 0), boundary
                )
                if index + 1 < len(tree_nodes)
                else boundary
            )
            node_id = f"{doc_id}:{node.get('node_id') or len(out)}"
            path = f"{parent_path} > {title}" if parent_path else title
            children = walk(
                node.get("nodes") or [], node_id, path, depth + 1, next_boundary
            )
            if children:
                end = max(start, max(child.page_end for child in children))
            else:
                end = max(start, next_boundary - 1) if next_boundary > start else start
            num, _, _ = parse_heading(title) or (None, title, 1)
            out.append(
                TocNodeData(
                    node_id=node_id,
                    parent_id=parent_id,
                    level=depth,
                    heading_num=num,
                    title=title,
                    heading_path=path,
                    page_start=start,
                    page_end=min(end, last_page),
                    source="pageindex_md",
                    confidence=0.7,
                )
            )
            out.extend(children)
        return out

    return walk(roots, None, "", 1, parsed.page_count)


@dataclass(frozen=True)
class TocIssue:
    severity: str  # "error" | "warning"
    message: str
    node_id: str | None = None


def validate_toc(nodes: list[TocNodeData], page_count: int | None = None) -> list[TocIssue]:
    """Check the structural invariants the TOC contract promises.

    Errors mean the tree is malformed (reversed range, missing parent, level
    jump, child outside parent). Warnings are quality signals: duplicate
    sibling numbering, large sibling range overlaps, or low page coverage.
    """
    issues: list[TocIssue] = []
    by_id = {node.node_id: node for node in nodes}

    for node in nodes:
        title = (node.title or "").strip()
        if not title:
            issues.append(TocIssue("error", "empty title", node.node_id))
        elif len(title) > 200:
            issues.append(
                TocIssue("warning", f"title too long ({len(title)} chars)", node.node_id)
            )

        if node.page_start is None or node.page_end is None:
            # A level-1 entry with no located page is normally a chapter the
            # source PDF does not contain (a contents list can name appendices
            # the excerpt omits). The tree faithfully records what the
            # document claims, so it is not a parsing failure. Deeper
            # nodes are expected to sit under a located parent.
            if node.level > 1:
                issues.append(
                    TocIssue("warning", "missing page range", node.node_id)
                )
            continue
        if node.page_end < node.page_start:
            issues.append(
                TocIssue(
                    "error",
                    f"reversed page range {node.page_start}-{node.page_end}",
                    node.node_id,
                )
            )

        if node.parent_id:
            parent = by_id.get(node.parent_id)
            if parent is None:
                issues.append(
                    TocIssue(
                        "error", f"missing parent {node.parent_id}", node.node_id
                    )
                )
                continue
            if node.level > parent.level + 1:
                issues.append(
                    TocIssue(
                        "error",
                        f"level jump {parent.level} -> {node.level}",
                        node.node_id,
                    )
                )
            if (
                parent.page_start is not None
                and parent.page_end is not None
                and (
                    node.page_start < parent.page_start
                    or node.page_end > parent.page_end
                )
            ):
                issues.append(
                    TocIssue(
                        "error",
                        f"range {node.page_start}-{node.page_end} "
                        f"outside parent {parent.page_start}-{parent.page_end}",
                        node.node_id,
                    )
                )

    groups: dict[str | None, list[TocNodeData]] = {}
    for node in nodes:
        groups.setdefault(node.parent_id, []).append(node)

    for siblings in groups.values():
        by_number: dict[str, list[TocNodeData]] = {}
        for node in siblings:
            if node.heading_num:
                by_number.setdefault(node.heading_num, []).append(node)
        for number, duplicates in by_number.items():
            if len(duplicates) > 1:
                titles = ", ".join(node.title for node in duplicates)
                issues.append(
                    TocIssue(
                        "warning",
                        f"duplicate sibling number {number}: {titles}",
                        duplicates[0].node_id,
                    )
                )

        ordered = sorted(
            (node for node in siblings if node.page_start is not None),
            key=lambda node: (node.page_start, node.page_end or node.page_start),
        )
        for left, right in zip(ordered, ordered[1:]):
            if left.page_end is None or right.page_end is None:
                continue
            overlap = min(left.page_end, right.page_end) - max(
                left.page_start, right.page_start
            ) + 1
            if overlap > 1:
                issues.append(
                    TocIssue(
                        "warning",
                        f"sibling ranges overlap by {overlap} pages: "
                        f"{left.title} / {right.title}",
                        right.node_id,
                    )
                )

    if nodes:
        covered_pages: set[int] = set()
        for node in nodes:
            if node.page_start is not None and node.page_end is not None:
                covered_pages.update(range(node.page_start, node.page_end + 1))
        denominator = page_count
        if denominator is None:
            starts = [n.page_start for n in nodes if n.page_start is not None]
            ends = [n.page_end for n in nodes if n.page_end is not None]
            denominator = (max(ends) - min(starts) + 1) if starts and ends else 0
        if denominator and len(covered_pages) / denominator < 0.5:
            issues.append(
                TocIssue(
                    "warning",
                    f"TOC covers {len(covered_pages)}/{denominator} pages (<50%)",
                )
            )

    return issues


def _roots_out_of_order(nodes: list[TocNodeData]) -> bool:
    """True when root pages contradict their heading-number order.

    A printed TOC that maps chapter 1 after an appendix (or chapter 7 after
    chapter 8) is unreliable; the PDF outline is a better skeleton.
    """
    placed = [
        node
        for node in nodes
        if node.parent_id is None
        and node.page_start is not None
        and node.heading_num
    ]
    if len(placed) < 3:
        return False
    ordered = sorted(placed, key=lambda node: (node.page_start or 0))
    keys = [heading_sort_key(node.heading_num) for node in ordered]
    return any(right < left for left, right in zip(keys, keys[1:]))


def score_nodes(
    nodes: list[TocNodeData], page_count: int | None
) -> tuple[int, int, int, float]:
    """Deterministic TOC quality score; higher tuples are better trees.

    Order of keys: fewer validation errors first (a malformed tree is never
    acceptable), then fewer warnings, then more nodes carrying chunks,
    then higher page coverage. Used by ``build_toc`` to arbitrate between
    the rule-based and the LLM proposal.
    """
    issues = validate_toc(nodes, page_count)
    errors = sum(1 for issue in issues if issue.severity == "error")
    warnings = sum(1 for issue in issues if issue.severity == "warning")
    with_chunks = sum(1 for node in nodes if node.page_start is not None)
    if page_count:
        covered = {
            page
            for node in nodes
            if node.page_start is not None and node.page_end is not None
            for page in range(node.page_start, node.page_end + 1)
        }
        coverage = len(covered) / max(1, page_count)
    else:
        coverage = 0.0
    return (-errors, -warnings, with_chunks, coverage)


def _from_printed_toc_llm(
    doc_id: str, parsed: ParsedDocument, settings: Settings
) -> list[TocNodeData]:
    """LLM printed-TOC proposal as TocNodeData; empty list on any failure."""
    printed = detect_printed_toc_llm(parsed, settings, doc_id=doc_id)
    if printed is None or len(printed.entries) < 2:
        return []
    nodes = _nodes_from_printed_entries(
        doc_id, printed.entries, parsed.page_count, printed.confidence
    )
    _disambiguate_appendix_numbers(nodes)
    for node in nodes:
        node.source = "printed_toc_llm"
    return _fix_page_ranges(nodes, parsed.page_count)


def _trace_channel(trace: dict | None, channel: str) -> None:
    if trace is not None:
        trace["channel"] = channel


def build_toc(
    doc_id: str,
    parsed: ParsedDocument,
    settings: Settings | None = None,
    trace: dict | None = None,
) -> list[TocNodeData]:
    """Build a page-indexed TOC with content-anchored node ids.

    The channel cascade lives in ``_build_toc_channels``; ids from every
    channel are positional, so they are rewritten here in one place (see
    ``app.curation.identity``). Anchoring once, at the boundary, means the
    chunker, FTS and every caller see the same stable ids.
    """
    nodes = _build_toc_channels(doc_id, parsed, settings, trace)
    return anchor_nodes(doc_id, nodes)


def anchor_nodes(doc_id: str, nodes: list[TocNodeData]) -> list[TocNodeData]:
    """Rewrite ``node_id`` / ``parent_id`` to content anchors, in place."""
    from ..curation.identity import assign_anchored_ids

    mapping = dict(zip((node.node_id for node in nodes), assign_anchored_ids(doc_id, nodes)))
    for node in nodes:
        node.node_id = mapping[node.node_id]
    for node in nodes:
        if node.parent_id:
            node.parent_id = mapping.get(node.parent_id, node.parent_id)
    return nodes


def _build_toc_channels(
    doc_id: str,
    parsed: ParsedDocument,
    settings: Settings | None = None,
    trace: dict | None = None,
) -> list[TocNodeData]:
    """Build a page-indexed TOC.

    A printed TOC page normally wins. A weak one (fewer than three
    top-level chapters) can be a misread table/cover, so a real PDF outline
    with three or more headings takes precedence instead. Otherwise:
    2+ bookmarks > printed TOC > OCR headings > single bookmark > fallback
    root. A single bookmark is usually just the document title, so it is
    kept as a last resort before the fallback.

    When the rule-based printed-TOC result is weak (or invalid) and an LLM
    is configured, the LLM proposal is built as well and the deterministic
    ``score_nodes`` comparison decides. An LLM failure falls back to the
    rule result; without an API key behaviour is identical to the
    rule-only pipeline.

    ``trace`` (optional dict) receives the provenance decisions so the
    caller can persist them: ``llm_outcome`` (accepted / rejected /
    no_proposal / error / not_needed / unavailable), ``llm_model``,
    ``llm_error`` and ``channel`` (the branch that produced the result).
    """
    settings = settings or get_settings()
    bookmarks = filter_bookmarks(parsed.bookmarks)
    if parsed.metadata.get("format") == "docx-direct" and len(bookmarks) >= 2:
        # Word paragraph pages are exact; a printed TOC read from the same
        # text needs printed-page mapping and is the weaker channel here.
        nodes = _from_bookmarks(doc_id, parsed, bookmarks)
        if nodes:
            _trace_channel(trace, "bookmark")
            return nodes

    nodes = _from_printed_toc(doc_id, parsed)
    if nodes:
        roots = sum(1 for node in nodes if node.parent_id is None)
        from .llm_toc import gangmu_available

        unordered = _roots_out_of_order(nodes)
        if gangmu_available(settings):
            # Arbitrate whenever the rule result is suspect: fewer than 3
            # roots (a misread table/cover or a truncated TOC), out-of-order
            # roots, or validation errors. A healthy rule tree is returned
            # as-is (no LLM cost). Independent of bookmark count: the
            # bookmark-based cascade below only runs when the rule tree is
            # unhealthy AND bookmarks can actually beat it.
            issues = validate_toc(nodes, parsed.page_count)
            has_errors = any(issue.severity == "error" for issue in issues)
            # A rule tree much smaller than the filtered outline is suspect
            # too: mangled text layers turn TOC blobs into a handful of junk
            # roots that still look "ordered", while the authored outline
            # holds the real tree.
            outline_richer = len(bookmarks) >= 3 and len(nodes) < len(bookmarks) // 2
            if roots < 3 or unordered or has_errors or outline_richer:
                if trace is not None:
                    trace["llm_used"] = True
                    trace["llm_model"] = settings.tocmemex_llm_model
                try:
                    llm_nodes = _from_printed_toc_llm(doc_id, parsed, settings)
                except Exception as exc:
                    llm_nodes = []  # any LLM failure falls back to the rule tree
                    if trace is not None:
                        trace["llm_outcome"] = "error"
                        trace["llm_error"] = str(exc) or type(exc).__name__
                if llm_nodes and score_nodes(
                    llm_nodes, parsed.page_count
                ) > score_nodes(nodes, parsed.page_count):
                    if trace is not None:
                        trace["llm_outcome"] = "accepted"
                    _trace_channel(trace, "printed_toc_llm")
                    return llm_nodes
                if trace is not None and "llm_outcome" not in trace:
                    trace["llm_outcome"] = "rejected" if llm_nodes else "no_proposal"
            elif trace is not None:
                trace["llm_outcome"] = "not_needed"
        elif trace is not None:
            trace["llm_outcome"] = "unavailable"
        outline_richer = len(bookmarks) >= 3 and len(nodes) < len(bookmarks) // 2
        weak = roots < 3 and len(bookmarks) >= 3
        if not (weak or (unordered and len(bookmarks) >= 3) or outline_richer):
            _trace_channel(trace, "printed_toc")
            return nodes

    if len(bookmarks) >= 2:
        nodes = _from_bookmarks(doc_id, parsed, bookmarks)
        if nodes:
            _trace_channel(trace, "bookmark")
            return nodes

    nodes = _from_markdown(doc_id, parsed)
    if nodes:
        _trace_channel(trace, "pageindex_md")
        return nodes

    if bookmarks:
        nodes = _from_bookmarks(doc_id, parsed, bookmarks)
        if nodes:
            _trace_channel(trace, "bookmark")
            return nodes

    _trace_channel(trace, "fallback")
    last_page = max(parsed.page_count - 1, 0)
    return [
        TocNodeData(
            node_id=f"{doc_id}:root",
            parent_id=None,
            level=1,
            heading_num=None,
            title="root",
            heading_path="root",
            page_start=0,
            page_end=last_page,
            source="fallback",
            confidence=0.3,
        )
    ]