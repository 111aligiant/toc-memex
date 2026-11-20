"""One-time migration: rewrite positional ``node_id``s to content anchors.

Before ADR-0002, ``node_id`` was ``doc:0007`` — a position. This walks every
document, computes the anchored ids from the stored nodes, rewrites
``toc_nodes`` and ``chunks``, records ``node_id_aliases`` so old references
keep resolving, and updates the activity tables in place.

It is idempotent: a document whose ids are already anchored is skipped.
"""

from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Chunk, Document, TocNode
from .identity import assign_anchored_ids, record_aliases, rewrite_references


def _is_anchored(doc_id: str, node_id: str) -> bool:
    if node_id.endswith(":root"):
        return True
    marker = f"{doc_id}:"
    if not node_id.startswith(marker):
        return False
    rest = node_id[len(marker):]
    return rest.startswith(("h:", "t:", "p:"))


async def anchor_document(db: AsyncSession, doc_id: str) -> dict:
    """Anchor one document's ids. Caller commits."""
    doc = await db.get(Document, doc_id)
    if doc is None:
        raise ValueError(f"document not found: {doc_id}")

    nodes = (
        (await db.execute(select(TocNode).where(TocNode.doc_id == doc_id).order_by(TocNode.id)))
        .scalars()
        .all()
    )
    if not nodes:
        return {"doc_id": doc_id, "skipped": "no nodes"}
    if all(_is_anchored(doc_id, node.node_id) for node in nodes):
        return {"doc_id": doc_id, "skipped": "already anchored"}

    new_ids = assign_anchored_ids(doc_id, nodes)
    mapping = {
        node.node_id: new_id for node, new_id in zip(nodes, new_ids) if node.node_id != new_id
    }

    # Two-pass rewrite so a swap (a→b, b→a) cannot collide mid-way.
    for index, node in enumerate(nodes):
        await db.execute(
            text("UPDATE toc_nodes SET node_id = :tmp WHERE id = :pk"),
            {"tmp": f"__migrate__{index}", "pk": node.id},
        )
    for node, new_id in zip(nodes, new_ids):
        await db.execute(
            text("UPDATE toc_nodes SET node_id = :new WHERE id = :pk"),
            {"new": new_id, "pk": node.id},
        )
    for node in nodes:
        if node.parent_id:
            parent = mapping.get(node.parent_id)
            if parent:
                await db.execute(
                    text("UPDATE toc_nodes SET parent_id = :new WHERE id = :pk"),
                    {"new": parent, "pk": node.id},
                )

    chunks = (
        (await db.execute(select(Chunk).where(Chunk.doc_id == doc_id))).scalars().all()
    )
    for chunk in chunks:
        if chunk.node_id and chunk.node_id in mapping:
            chunk.node_id = mapping[chunk.node_id]
    await db.flush()

    aliases = await record_aliases(db, doc_id, mapping, reason="anchored")
    refs = await rewrite_references(db, doc_id, mapping)
    await db.flush()
    return {
        "doc_id": doc_id,
        "renamed": len(mapping),
        "aliases": aliases,
        "references": refs,
    }


async def anchor_all(db: AsyncSession, *, commit: bool = True) -> list[dict]:
    doc_ids = (await db.execute(select(Document.id))).scalars().all()
    results = []
    for doc_id in doc_ids:
        results.append(await anchor_document(db, doc_id))
        if commit:
            await db.commit()
    return results
