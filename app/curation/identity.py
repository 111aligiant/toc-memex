"""Content-anchored ``node_id`` (ADR-0002 §6.3).

``node_id`` used to be ``f"{doc_id}:{len(nodes):04d}"`` — a position. Insert
one node and every id after it shifts, so a note written yesterday points at
the wrong section today. The anchor is content instead::

    {doc_id}:h:{heading_num}          # numbered heading, number unique (96.6%)
    {doc_id}:h:{heading_num}@{page}   # number repeats (same-numbered appendices)
    {doc_id}:t:{sha1(title)[:8]}      # no number
    {doc_id}:p:{page}:{n}             # no number, no usable title

The fallback ``{doc_id}:root`` (single unparsed document) is kept verbatim:
it is unique per document and stable, and old data already references it.
"""

from __future__ import annotations

import hashlib
import re

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import NodeIdAlias

_SPACE_RE = re.compile(r"\s+")


def _canon_num(value: str | None) -> str:
    return _SPACE_RE.sub("", value or "").lower()


def _canon_title(value: str | None) -> str:
    return _SPACE_RE.sub("", (value or "").lower())


def _title_hash(title: str) -> str:
    return hashlib.sha1(_canon_title(title).encode("utf-8")).hexdigest()[:8]


def _base(doc_id: str, node) -> str:
    """The candidate id before de-duplication."""
    if node.node_id and node.node_id.endswith(":root") and node.parent_id is None:
        # Legacy fallback root: keep the exact id the old pipeline used.
        return f"{doc_id}:root"
    if node.heading_num and _canon_num(node.heading_num):
        return f"{doc_id}:h:{_canon_num(node.heading_num)}"
    title = (node.title or "").strip()
    if title:
        return f"{doc_id}:t:{_title_hash(title)}"
    page = node.page_start if node.page_start is not None else 0
    return f"{doc_id}:p:{page}:0"


def assign_anchored_ids(doc_id: str, nodes: list) -> list[str]:
    """Return the anchored id for each node, in the order given.

    ``nodes`` is anything with ``node_id`` / ``heading_num`` / ``title`` /
    ``page_start`` / ``parent_id`` attributes (``TocNodeData`` or ``TocNode``).
    Duplicates are resolved deterministically: first by page, then by a
    numeric suffix, so re-running on the same tree yields the same ids.
    """
    bases = [_base(doc_id, node) for node in nodes]
    counts: dict[str, int] = {}
    for base in bases:
        counts[base] = counts.get(base, 0) + 1

    used: set[str] = set()
    out: list[str] = []
    for node, base in zip(nodes, bases):
        if counts[base] == 1 and base not in used:
            out.append(base)
            used.add(base)
            continue
        if not base.endswith(":root"):
            candidate = f"{base}@{node.page_start}"
            if candidate not in used:
                out.append(candidate)
                used.add(candidate)
                continue
        # Same page too (a document can repeat a number twice on one page),
        # or a title hash collision: suffix with an occurrence counter.
        suffix = 2
        while f"{base}~{suffix}" in used:
            suffix += 1
        candidate = f"{base}~{suffix}"
        out.append(candidate)
        used.add(candidate)
    return out


def remap_ids(doc_id: str, nodes: list) -> dict[str, str]:
    """Map every old ``node_id`` to its anchored replacement."""
    new_ids = assign_anchored_ids(doc_id, nodes)
    return {
        node.node_id: new_id
        for node, new_id in zip(nodes, new_ids)
        if node.node_id != new_id
    }


async def resolve_node_id(db: AsyncSession, doc_id: str, node_id: str) -> str:
    """Follow ``node_id_aliases`` until the id is current.

    Readers (``read_section``, MCP tools, the activity log) call this so a
    note written before the id rewrite still lands on the right section.
    A database created before the alias table existed simply resolves to
    the id it was given.
    """
    seen: set[str] = set()
    current = node_id
    while current not in seen:
        seen.add(current)
        try:
            row = (
                await db.execute(
                    select(NodeIdAlias.new_id).where(
                        NodeIdAlias.doc_id == doc_id, NodeIdAlias.old_id == current
                    )
                )
            ).scalar_one_or_none()
        except Exception:  # table missing on a pre-P0 database
            return current
        if row is None:
            return current
        current = row
    return current


async def record_aliases(
    db: AsyncSession, doc_id: str, mapping: dict[str, str], reason: str = "anchored"
) -> int:
    """Write old→new rows, ignoring pairs already recorded."""
    if not mapping:
        return 0
    existing = set(
        (
            await db.execute(
                select(NodeIdAlias.old_id).where(NodeIdAlias.doc_id == doc_id)
            )
        ).scalars()
    )
    written = 0
    for old_id, new_id in mapping.items():
        if old_id in existing or old_id == new_id:
            continue
        db.add(
            NodeIdAlias(
                doc_id=doc_id, old_id=old_id, new_id=new_id, reason=reason
            )
        )
        written += 1
    return written


async def rewrite_references(db: AsyncSession, doc_id: str, mapping: dict[str, str]) -> dict:
    """Point activity-layer rows at the anchored ids.

    Every table that stores ``node_id`` is updated in place. ``chunks`` and
    ``toc_nodes`` are rebuilt by the caller; this only touches rows that
    survive a reindex.
    """
    if not mapping:
        return {}
    counts: dict[str, int] = {}
    for table in ("agent_notes", "activities", "errata", "usage_log", "resources"):
        for old_id, new_id in mapping.items():
            result = await db.execute(
                text(
                    f"UPDATE {table} SET node_id = :new WHERE doc_id = :doc AND node_id = :old"
                ),
                {"new": new_id, "doc": doc_id, "old": old_id},
            )
            if result.rowcount:
                counts[table] = counts.get(table, 0) + result.rowcount
    return counts
