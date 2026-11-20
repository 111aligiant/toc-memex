"""Pre-write snapshots: the last N derived states per document.

ADR-0002 §6.2 (snapshot). Before any write the affected rows are stored so
a rollback is a restore, not a reindex. Retention is per document, newest
``KEEP`` rows.
"""

from __future__ import annotations

import gzip
import json

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import CurationSnapshot, utcnow_iso

KEEP = 5


def _dumps(value) -> bytes:
    return gzip.compress(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"), mtime=0
    )


def _loads(blob: bytes):
    return json.loads(gzip.decompress(blob).decode("utf-8"))


async def write_snapshot(
    db: AsyncSession,
    doc_id: str,
    proposal_id: str,
    *,
    nodes: list[dict],
    chunks: list[dict],
) -> int:
    """Store one snapshot and trim the document's history to ``KEEP``."""
    row = CurationSnapshot(
        doc_id=doc_id,
        proposal_id=proposal_id,
        toc_json=_dumps(nodes),
        chunks_json=_dumps(chunks),
        created_at=utcnow_iso(),
    )
    db.add(row)
    await db.flush()
    stale = (
        (
            await db.execute(
                select(CurationSnapshot.id)
                .where(CurationSnapshot.doc_id == doc_id)
                .order_by(CurationSnapshot.id.desc())
                .offset(KEEP)
            )
        )
        .scalars()
        .all()
    )
    if stale:
        await db.execute(delete(CurationSnapshot).where(CurationSnapshot.id.in_(stale)))
    return row.id


async def latest_snapshot(db: AsyncSession, doc_id: str) -> CurationSnapshot | None:
    return (
        await db.execute(
            select(CurationSnapshot)
            .where(CurationSnapshot.doc_id == doc_id)
            .order_by(CurationSnapshot.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def snapshot_for_proposal(
    db: AsyncSession, proposal_id: str
) -> CurationSnapshot | None:
    """The pre-write state recorded for one proposal, if it still exists.

    Retention keeps the newest ``KEEP`` snapshots per document, so an old
    proposal may have been trimmed; the caller treats that as "undo falls
    back to the baseline" rather than silently doing nothing.
    """
    return (
        await db.execute(
            select(CurationSnapshot)
            .where(CurationSnapshot.proposal_id == proposal_id)
            .order_by(CurationSnapshot.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def list_snapshots(db: AsyncSession, doc_id: str) -> list[dict]:
    rows = (
        (
            await db.execute(
                select(CurationSnapshot)
                .where(CurationSnapshot.doc_id == doc_id)
                .order_by(CurationSnapshot.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": row.id,
            "proposal_id": row.proposal_id,
            "nodes": len(_loads(row.toc_json)),
            "chunks": len(_loads(row.chunks_json)),
            "created_at": row.created_at,
        }
        for row in rows
    ]


def snapshot_payload(row: CurationSnapshot) -> tuple[list[dict], list[dict]]:
    return _loads(row.toc_json), _loads(row.chunks_json)
