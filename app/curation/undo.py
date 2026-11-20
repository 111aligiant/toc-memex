"""Undo one curator proposal (ADR-0002 §6.2).

A snapshot is written before the first applied op of every proposal, keyed
by the proposal id. Undoing restores exactly that state, so a single change
can be rolled back without touching anything the curator did before or
after it.

Historical proposals written before snapshots were wired into the runner
have no row. For those (and for a snapshot already trimmed by retention)
the only pre-change state available is the document's adoption baseline;
the caller restores that instead and the result says so explicitly — an
undo that silently undoes more than asked is worse than a clear fallback.
"""

from __future__ import annotations

import gzip
import json
from collections import Counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    CurationJob,
    CurationProposal,
    CurationSnapshot,
    DocBaseline,
    Document,
    utcnow_iso,
)
from .baselines import latest_baseline, restore_baseline
from .ops import build_context, restore_rows
from .outcomes import record_outcome
from .snapshots import snapshot_for_proposal, snapshot_payload, write_snapshot


async def undo_proposal(
    db: AsyncSession, data_dir, proposal_id: str
) -> dict:
    """Restore the state before ``proposal_id``; caller commits.

    Returns ``{"proposal_id", "doc_id", "mode", "baseline_id", ...}`` where
    ``mode`` is ``"snapshot"`` (exact, stepwise) or ``"baseline"`` (the
    whole document returns to its pre-curator state).
    """
    proposal = await db.get(CurationProposal, proposal_id)
    if proposal is None:
        raise LookupError(f"proposal not found: {proposal_id}")
    job = await db.get(CurationJob, proposal.job_id)
    if job is None:
        raise LookupError(f"job not found for proposal: {proposal_id}")
    doc = await db.get(Document, job.doc_id)
    if doc is None:
        raise LookupError(f"document not found for proposal: {proposal_id}")

    snapshot = await snapshot_for_proposal(db, proposal_id)
    baseline_id: int | None = None
    if snapshot is not None:
        nodes, chunks = snapshot_payload(snapshot)
        ctx = await build_context(db, doc, data_dir)
        await restore_rows(ctx, nodes, chunks)
        mode = "snapshot"
        restored = {"nodes": len(nodes), "chunks": len(chunks)}
    else:
        baseline = await latest_baseline(db, doc.id)
        if baseline is None:
            raise ValueError(
                "proposal has no snapshot and the document has no baseline; "
                "nothing safe to restore"
            )
        result = await restore_baseline(db, doc.id, baseline.id, force=True)
        baseline_id = baseline.id
        mode = "baseline"
        restored = {
            "nodes": result["restored_nodes"],
            "chunks": result["restored_chunks"],
        }

    job.status = "rolled_back"
    job.last_error = f"undo proposal {proposal_id} ({mode})"
    job.updated_at = utcnow_iso()
    await record_outcome(
        db,
        doc_id=doc.id,
        issue_id=job.issue_id,
        kind=job.kind,
        strategy=proposal.strategy,
        action="rolled_back",
        actor="human:undo",
        note=f"{mode}; proposal {proposal_id}",
    )
    await db.flush()
    return {
        "proposal_id": proposal_id,
        "doc_id": doc.id,
        "mode": mode,
        "baseline_id": baseline_id,
        "restored": restored,
        "stepwise": mode == "snapshot",
    }


async def backfill_snapshots(
    db: AsyncSession, *, dry_run: bool = False
) -> dict:
    """Give legacy applied proposals the pre-write state they were missing.

    Proposals written before the runner recorded snapshots have no row, so
    they could only be undone by restoring the whole document to its
    adoption baseline. When a document has exactly one applied proposal,
    the adoption baseline *is* that proposal's pre-write state — copying it
    into a snapshot restores stepwise undo without guessing.

    Conservative on purpose: skip documents with more than one applied
    proposal (the baseline would be older than that proposal) and documents
    whose baseline content hash no longer matches. Caller commits.
    """
    proposals = (
        (
            await db.execute(
                select(CurationProposal)
                .where(CurationProposal.applied_ops > 0)
                .order_by(CurationProposal.created_at)
            )
        )
        .scalars()
        .all()
    )
    recorded = set(
        (
            await db.execute(select(CurationSnapshot.proposal_id).distinct())
        )
        .scalars()
        .all()
    )
    jobs = {
        job.id: job
        for job in (await db.execute(select(CurationJob))).scalars().all()
    }
    per_doc = Counter(
        jobs[proposal.job_id].doc_id
        for proposal in proposals
        if proposal.job_id in jobs
    )

    repaired = 0
    skipped = {"multi_proposal": 0, "no_baseline": 0, "hash_mismatch": 0, "no_doc": 0}
    repaired_ids: list[str] = []
    for proposal in proposals:
        if proposal.id in recorded:
            continue
        job = jobs.get(proposal.job_id)
        if job is None or per_doc[job.doc_id] != 1:
            skipped["multi_proposal" if job is not None else "no_doc"] += 1
            continue
        doc = await db.get(Document, job.doc_id)
        if doc is None:
            skipped["no_doc"] += 1
            continue
        baseline = (
            await db.execute(
                select(DocBaseline)
                .where(DocBaseline.doc_id == job.doc_id)
                .order_by(DocBaseline.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if baseline is None:
            skipped["no_baseline"] += 1
            continue
        if baseline.content_hash != doc.content_hash:
            skipped["hash_mismatch"] += 1
            continue
        nodes = json.loads(gzip.decompress(baseline.toc_json).decode("utf-8"))
        chunks = json.loads(gzip.decompress(baseline.chunks_json).decode("utf-8"))
        if not dry_run:
            await write_snapshot(
                db, job.doc_id, proposal.id, nodes=nodes, chunks=chunks
            )
        repaired += 1
        repaired_ids.append(proposal.id)

    return {
        "applied_proposals": len(proposals),
        "already_had_snapshot": len(proposals) - repaired - sum(skipped.values()),
        "repaired": repaired,
        "skipped": skipped,
        "dry_run": dry_run,
        "repaired_ids": repaired_ids[:50],
    }
