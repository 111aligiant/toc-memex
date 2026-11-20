"""Deterministic curation strategies (ADR-0002 §7).

These are the default path: they run with no LLM configured and are what the
curator falls back to whenever the model is unavailable, over budget or
disabled. Each returns a list of ops in the same shape the agent writes, so
validation, risk gating and snapshots are shared.

Document summaries are deliberately not generated here. An extractive
"first 300 chars" summary just repeats the document's boilerplate
preamble, and a TOC-derived one only restates ``get_toc`` — both measured
as noise. The field stays empty until an LLM-written or hand-written
summary exists for documents whose filename does not describe their
content.

Op shapes::

    {"op": "merge_chunks", "chunk_ids": [12, 13], "evidence": "..."}
    {"op": "set_page", "node_id": "...", "page_start": 3, "page_end": 7,
     "evidence": "3.1 范围"}
"""

from __future__ import annotations

import re

from ..models import Chunk, TocNode

SHORT_CHUNK_CHARS = 50

_NUMBER_RE = re.compile(r"^\s*(\d+(?:\.\d+)*|[一二三四五六七八九十]+)\s*$")
_NUM_PREFIX_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)")


def _is_title_fragment(content: str, heading_num: str | None) -> bool:
    """The chunk is just a section number, or a number plus its title."""
    text = (content or "").strip()
    if not text or len(text) > SHORT_CHUNK_CHARS:
        return False
    if _NUMBER_RE.match(text):
        return True
    if heading_num:
        match = _NUM_PREFIX_RE.match(text)
        if match and match.group(1) == re.sub(r"\s+", "", heading_num):
            return True
    return False


def _is_symbols_only(content: str) -> bool:
    text = (content or "").strip()
    if not text:
        return True
    return not any(char.isalnum() or "一" <= char <= "鿿" for char in text)


def _is_duplicate_of_neighbour(chunk: Chunk, group: list[Chunk], position: int) -> bool:
    """The slice repeats text its same-node neighbour already contains.

    The chunker carries a 100-character overlap between consecutive slices,
    so a slice whose whole body appears inside the previous or next slice
    adds no information — merging it back loses nothing.
    """
    text = (chunk.content or "").strip()
    if len(text) < 4:
        return False
    for offset in (-1, 1):
        pos = position + offset
        if not 0 <= pos < len(group):
            continue
        other = group[pos]
        if other.id == chunk.id:
            continue
        if text in (other.content or ""):
            return True
    return False


def _canon(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def is_heading_only(content: str, node: TocNode | None) -> bool:
    """The slice's entire body is the node's own heading.

    The chunker starts a slice at every heading, so a parent section with no
    text before its first child yields a slice that is just ``"3.0 一般说明"``.
    That heading is already in the child's ``heading_path``; the slice itself
    carries no content of its own.
    """
    if node is None:
        return False
    text = (content or "").strip()
    if not text or len(text) > SHORT_CHUNK_CHARS:
        return False
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    title = _canon(node.title)
    num = _canon(node.heading_num)
    first = _canon(lines[0])
    heading = num and (first == num or first.startswith(num))
    if not heading:
        return False
    # A heading line plus at most a short tail of punctuation is still just
    # the heading; real body text would be longer than the heading itself.
    body = _canon("".join(lines[1:]))
    return len(body) <= 8

def plan_short_chunk_merges(
    chunks: list[Chunk],
    nodes: dict[str, TocNode],
    max_chars: int,
) -> list[dict]:
    """Merge short slices that have a structural reason to merge (§7.1).

    Only structural evidence counts — the rules never read meaning:

    a) a slice that is just a heading number (or number + title) joins the
       next slice of the same node;
    b) a slice of pure numbers/symbols (page numbers, table remnants) joins
       a same-node neighbour;
    c) consecutive short slices of one node pack into groups that reach the
       short threshold where possible, and never exceed the size cap.

    A node with one short slice and no same-node neighbour is left alone:
    that is a genuinely tiny section, and merging it would cross a TOC
    boundary. This is the deliberate "否则：不动" branch.
    """
    ordered = sorted(chunks, key=lambda chunk: chunk.chunk_index)
    by_node: dict[str | None, list[Chunk]] = {}
    for chunk in ordered:
        by_node.setdefault(chunk.node_id, []).append(chunk)

    consumed: set[int] = set()
    ops: list[dict] = []

    def emit(group: list[Chunk], reason: str) -> bool:
        total = sum(len(chunk.content or "") for chunk in group)
        if len(group) < 2 or total > max_chars:
            return False
        consumed.update(chunk.id for chunk in group)
        ops.append(
            {
                "op": "merge_chunks",
                "chunk_ids": [chunk.id for chunk in group],
                "evidence": reason,
            }
        )
        return True

    for node_id, group in by_node.items():
        node = nodes.get(node_id or "")
        heading_num = node.heading_num if node else None
        has_children = node is not None and any(
            other.parent_id == node.node_id for other in nodes.values()
        )

        # (a)/(b): a heading fragment or a symbols-only slice takes its
        # same-node neighbour, whatever the neighbour's size.
        for position, chunk in enumerate(group):
            if chunk.id in consumed:
                continue
            content = chunk.content or ""
            if len(content) >= SHORT_CHUNK_CHARS:
                continue
            reason = None
            if _is_title_fragment(content, heading_num):
                reason = "heading fragment"
            elif _is_symbols_only(content):
                reason = "symbols only"
            if reason is None:
                continue
            neighbours = [
                other
                for other in group[max(0, position - 1) : position + 2]
                if other.id != chunk.id and other.id not in consumed
            ]
            if not neighbours:
                continue
            # Prefer the following slice: a heading introduces what comes next.
            neighbour = next(
                (other for other in neighbours if other.chunk_index > chunk.chunk_index),
                neighbours[0],
            )
            pair = sorted([chunk, neighbour], key=lambda item: item.chunk_index)
            emit(pair, reason)

        # (f): a slice whose whole body is its own heading, on a node that has
        # children, is a heading the chunker split off a parent section. The
        # children carry the content and the heading is already in their
        # heading_path. A childless node is never dropped: its heading is the
        # only trace that section exists.
        # Grounding is checked against the chunk rather than the page: some
        # documents have no OCR cache (text-layer PDFs), and the chunk itself
        # is the material being judged.
        for position, chunk in enumerate(group):
            if chunk.id in consumed or len(chunk.content or "") >= SHORT_CHUNK_CHARS:
                continue
            if len(group) != 1 or not has_children:
                continue
            if not is_heading_only(chunk.content, node):
                continue
            consumed.add(chunk.id)
            ops.append(
                {
                    "op": "drop_chunk",
                    "chunk_id": chunk.id,
                    "evidence": (chunk.content or "").strip()[:40],
                    "evidence_in_chunk": True,
                }
            )

        # (c)/(d): pack consecutive short slices into groups of ≥ 2 that
        # reach the threshold when the text allows it.
        run: list[Chunk] = []
        for chunk in group + [None]:
            is_short = chunk is not None and len(chunk.content or "") < SHORT_CHUNK_CHARS
            if is_short and chunk.id not in consumed:
                run.append(chunk)
                continue
            if len(run) >= 2:
                _pack_run(run, max_chars, emit)
            run = []

        # (e): a short slice that is wholly contained in a same-node
        # neighbour is an overlap artifact; fold it into that neighbour.
        for position, chunk in enumerate(group):
            if chunk.id in consumed or len(chunk.content or "") >= SHORT_CHUNK_CHARS:
                continue
            if not _is_duplicate_of_neighbour(chunk, group, position):
                continue
            neighbours = [
                other
                for other in group[max(0, position - 1) : position + 2]
                if other.id != chunk.id and other.id not in consumed
            ]
            if not neighbours:
                continue
            neighbour = neighbours[0]
            pair = sorted([chunk, neighbour], key=lambda item: item.chunk_index)
            emit(pair, "overlap duplicate")

    return ops


def _pack_run(run: list[Chunk], max_chars: int, emit) -> None:
    """Merge a run of short slices of one node into as few slices as possible.

    ADR-0002 §7.1(d): three or more consecutive short slices of one node
    become one slice. A whole run that fits under the cap becomes exactly
    one; a longer run is packed greedily, each group filling up to the cap.
    """
    total = sum(len(chunk.content or "") for chunk in run)
    if total <= max_chars:
        emit(run, "short run")
        return
    group: list[Chunk] = []
    size = 0
    for chunk in run:
        chunk_size = len(chunk.content or "")
        if group and size + chunk_size > max_chars:
            emit(group, "short run")
            group, size = [], 0
        group.append(chunk)
        size += chunk_size
    if len(group) >= 2:
        emit(group, "short run")


def plan_page_range_fixes(nodes: list[TocNode]) -> list[dict]:
    """Parents must span their subtree (§7.3).

    ``pageindex_md`` gives 81–92% of nodes ``page_start == page_end``; a
    parent that claims one page while its children span ten is provably
    wrong without reading the text. Only parents are widened; a leaf that
    looks short may genuinely be a short section, so it is left alone.
    """
    by_id = {node.node_id: node for node in nodes}
    children: dict[str, list[TocNode]] = {}
    for node in nodes:
        if node.parent_id:
            children.setdefault(node.parent_id, []).append(node)

    ops: list[dict] = []
    for node in nodes:
        kids = children.get(node.node_id, [])
        if not kids:
            continue
        if node.page_start is None or node.page_end is None:
            continue
        starts = [kid.page_start for kid in kids if kid.page_start is not None]
        ends = [kid.page_end for kid in kids if kid.page_end is not None]
        if not starts or not ends:
            continue
        new_start = min([node.page_start, *starts])
        new_end = max([node.page_end, *ends])
        if new_start == node.page_start and new_end == node.page_end:
            continue
        if new_end < new_start:
            continue
        ops.append(
            {
                "op": "set_page",
                "node_id": node.node_id,
                "page_start": new_start,
                "page_end": new_end,
                "evidence": f"{node.heading_num or ''} {node.title}".strip(),
            }
        )
    return ops
