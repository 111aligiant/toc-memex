"""Baseline snapshots: a document's derived state before the curator wrote.

ADR-0002 §4. A baseline is created for every document up front (so
"before the agent touched it" has no ambiguity), never overwritten, and
never reclaimed. Restoring is a first-class product action — a Web button
and a CLI command, not an ops script.

Storage is the database, not files: the baseline then shares the
transaction and the backup with the data it describes, and cannot be
swept up by a cache-cleaning script.
"""

from __future__ import annotations

import gzip
import hashlib
import json

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Chunk, DocBaseline, Document, TocNode, utcnow_iso

REASONS = ("adoption", "pre_curator_write", "manual")
_DOC_FIELDS = ("summary", "keywords", "page_count", "char_count", "status")


def _dumps(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _node_row(node: TocNode) -> dict:
    return {
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


def _chunk_row(chunk: Chunk) -> dict:
    return {
        "node_id": chunk.node_id,
        "chunk_index": chunk.chunk_index,
        "content": chunk.content,
        "heading_path": chunk.heading_path,
        "page_start": chunk.page_start,
        "page_end": chunk.page_end,
        "token_count": chunk.token_count,
        "ref_ids": chunk.ref_ids,
    }


def _checksum(toc_blob: bytes, chunks_blob: bytes, doc_fields: str) -> str:
    digest = hashlib.sha256()
    digest.update(toc_blob)
    digest.update(chunks_blob)
    digest.update(doc_fields.encode("utf-8"))
    return digest.hexdigest()


async def latest_baseline(db: AsyncSession, doc_id: str) -> DocBaseline | None:
    return (
        await db.execute(
            select(DocBaseline)
            .where(DocBaseline.doc_id == doc_id)
            .order_by(DocBaseline.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def create_baseline(
    db: AsyncSession,
    doc_id: str,
    reason: str = "pre_curator_write",
    *,
    overwrite: bool = False,
) -> DocBaseline | None:
    """Snapshot the document's current derived state.

    Returns ``None`` when a baseline for this ``(doc_id, content_hash,
    reason)`` already exists and ``overwrite`` is false — the caller wanted
    "make sure one exists", and it does. Caller commits.
    """
    if reason not in REASONS:
        raise ValueError(f"invalid baseline reason: {reason}")
    doc = await db.get(Document, doc_id)
    if doc is None:
        raise ValueError(f"document not found: {doc_id}")

    existing = (
        await db.execute(
            select(DocBaseline).where(
                DocBaseline.doc_id == doc_id,
                DocBaseline.content_hash == doc.content_hash,
                DocBaseline.reason == reason,
            )
        )
    ).scalar_one_or_none()
    if existing is not None and not overwrite:
        return None

    nodes = (
        (await db.execute(select(TocNode).where(TocNode.doc_id == doc_id).order_by(TocNode.id)))
        .scalars()
        .all()
    )
    chunks = (
        (
            await db.execute(
                select(Chunk).where(Chunk.doc_id == doc_id).order_by(Chunk.chunk_index)
            )
        )
        .scalars()
        .all()
    )
    toc_blob = gzip.compress(_dumps([_node_row(node) for node in nodes]), mtime=0)
    chunks_blob = gzip.compress(_dumps([_chunk_row(chunk) for chunk in chunks]), mtime=0)
    doc_fields = json.dumps(
        {field: getattr(doc, field) for field in _DOC_FIELDS}, ensure_ascii=False
    )

    if existing is not None:
        row = existing
    else:
        row = DocBaseline(doc_id=doc_id, content_hash=doc.content_hash, reason=reason)
        db.add(row)
    row.toc_json = toc_blob
    row.chunks_json = chunks_blob
    row.doc_fields = doc_fields
    row.sha256 = _checksum(toc_blob, chunks_blob, doc_fields)
    row.node_count = len(nodes)
    row.chunk_count = len(chunks)
    row.doc_updated_at = doc.updated_at
    row.created_at = utcnow_iso()
    await db.flush()
    return row


def _load_blob(blob: bytes) -> list[dict]:
    return json.loads(gzip.decompress(blob).decode("utf-8"))


def verify_baseline(row: DocBaseline) -> bool:
    """True when the stored blobs still hash to the recorded checksum."""
    return _checksum(row.toc_json, row.chunks_json, row.doc_fields) == row.sha256


async def restore_baseline(
    db: AsyncSession, doc_id: str, baseline_id: int | None = None, *, force: bool = False
) -> dict:
    """Replace a document's derived rows with a baseline snapshot.

    Steps (ADR-0002 §4.3): snapshot the current state first, clear derived
    rows, write the baseline back, rebuild FTS, restore document fields.
    Provenance rows are the caller's job. Caller commits.

    ``content_hash`` mismatch is refused unless ``force`` — the baseline then
    describes a file version the document no longer is.
    """
    from ..index.fts import index_document
    from .snapshots import write_snapshot

    doc = await db.get(Document, doc_id)
    if doc is None:
        raise ValueError(f"document not found: {doc_id}")

    if baseline_id is None:
        row = await latest_baseline(db, doc_id)
    else:
        row = await db.get(DocBaseline, baseline_id)
        if row is not None and row.doc_id != doc_id:
            raise ValueError("baseline belongs to another document")
    if row is None:
        raise LookupError(f"no baseline for document: {doc_id}")
    if not verify_baseline(row):
        raise ValueError(f"baseline {row.id} failed its checksum")

    hash_mismatch = row.content_hash != doc.content_hash
    if hash_mismatch and not force:
        raise ValueError(
            "content hash mismatch: the document was re-ingested, "
            "this baseline describes the old version (pass force to override)"
        )

    current_nodes = (
        (await db.execute(select(TocNode).where(TocNode.doc_id == doc_id))).scalars().all()
    )
    current_chunks = (
        (await db.execute(select(Chunk).where(Chunk.doc_id == doc_id))).scalars().all()
    )
    snapshot_id = await write_snapshot(
        db,
        doc_id,
        proposal_id=f"restore:{row.id}",
        nodes=[_node_row(node) for node in current_nodes],
        chunks=[_chunk_row(chunk) for chunk in current_chunks],
    )

    for statement in (
        "DELETE FROM chunk_fts WHERE doc_id = :doc_id",
        "DELETE FROM doc_fts WHERE doc_id = :doc_id",
        "DELETE FROM chunks WHERE doc_id = :doc_id",
        "DELETE FROM toc_nodes WHERE doc_id = :doc_id",
    ):
        await db.execute(text(statement), {"doc_id": doc_id})
    # The raw DELETEs bypass the ORM, so the loaded rows stay in the
    # identity map; SQLite reuses their rowids and the re-insert would
    # collide. Detach them first.
    for obj in [*current_nodes, *current_chunks]:
        db.expunge(obj)
    await db.flush()

    new_nodes = _load_blob(row.toc_json)
    new_chunks = _load_blob(row.chunks_json)
    for node in new_nodes:
        db.add(TocNode(doc_id=doc_id, **node))
    for chunk in new_chunks:
        db.add(Chunk(doc_id=doc_id, **chunk))

    fields = json.loads(row.doc_fields)
    for field in _DOC_FIELDS:
        if field in fields:
            setattr(doc, field, fields[field])
    doc.status = "ready"
    doc.updated_at = utcnow_iso()
    await db.flush()

    # Re-adding chunks assigns new autoincrement ids. Activity rows still
    # hold the old ids; match them through chunk_index so a note keeps
    # pointing at the same slice.
    await _remap_chunk_ids(db, doc_id, current_chunks)

    stored_chunks = (
        (
            await db.execute(
                select(Chunk).where(Chunk.doc_id == doc_id).order_by(Chunk.chunk_index)
            )
        )
        .scalars()
        .all()
    )
    await index_document(db, doc, stored_chunks)
    await flush_curator_jobs(db, doc_id, reason="restored_by_human")
    await db.flush()

    return {
        "doc_id": doc_id,
        "baseline_id": row.id,
        "snapshot_id": snapshot_id,
        "restored_nodes": len(new_nodes),
        "restored_chunks": len(new_chunks),
        "forced": bool(hash_mismatch and force),
        "hash_mismatch": hash_mismatch,
    }


async def _remap_chunk_ids(db: AsyncSession, doc_id: str, old_chunks: list[Chunk]) -> None:
    """Point activity rows at the re-inserted chunk with the same index."""
    if not old_chunks:
        return
    rows = (
        await db.execute(
            text("SELECT id, chunk_index FROM chunks WHERE doc_id = :doc"),
            {"doc": doc_id},
        )
    ).all()
    new_by_index = {index: chunk_id for chunk_id, index in rows}
    old_to_new = {
        chunk.id: new_by_index[chunk.chunk_index]
        for chunk in old_chunks
        if chunk.chunk_index in new_by_index and chunk.id != new_by_index[chunk.chunk_index]
    }
    for table in ("agent_notes", "activities", "errata", "usage_log"):
        for old_id, new_id in old_to_new.items():
            await db.execute(
                text(
                    f"UPDATE {table} SET chunk_id = :new WHERE doc_id = :doc AND chunk_id = :old"
                ),
                {"new": new_id, "doc": doc_id, "old": old_id},
            )


async def flush_curator_jobs(db: AsyncSession, doc_id: str, reason: str) -> int:
    """Settle this document's open jobs so the next run does not redo them.

    Without this, a restore is followed by the curator re-applying the exact
    same fix on the next pass — the "restore → changed again" tug of war
    ADR-0002 §15 calls out. Marking jobs rejected moves their fingerprint
    state, and the cooldown then keeps them from re-opening.
    """
    from ..models import CurationJob

    rows = (
        (
            await db.execute(
                select(CurationJob).where(
                    CurationJob.doc_id == doc_id,
                    CurationJob.status.in_(("queued", "running", "proposed", "applied")),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.status = "rolled_back"
        row.last_error = reason
        row.updated_at = utcnow_iso()
    await db.flush()
    return len(rows)


async def baseline_out(db: AsyncSession, row: DocBaseline, doc: Document | None = None) -> dict:
    """API shape for one baseline row (no blobs)."""
    doc = doc or await db.get(Document, row.doc_id)
    return {
        "id": row.id,
        "doc_id": row.doc_id,
        "reason": row.reason,
        "content_hash": row.content_hash,
        "matches_current": bool(doc and doc.content_hash == row.content_hash),
        "node_count": row.node_count,
        "chunk_count": row.chunk_count,
        "doc_updated_at": row.doc_updated_at,
        "created_at": row.created_at,
        "sha256": row.sha256,
    }
