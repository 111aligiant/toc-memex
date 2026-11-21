"""Patrol signals: one read-only diagnosis surface for the curator agent.

``docs/self-evolution.md`` §5.4.1. The quality page, the search log, the
activity ledger and the outcome ledger each know something about what is
wrong with the library; the agent saw only the first (through
``get_quality`` on one document). This module joins them into one bounded
snapshot so the agent can *name* a problem before it can fix one:

- quality issues aggregated by kind (the same groups ``/api/quality`` shows);
- zero-hit queries — what people asked for and the library could not answer;
- documents searched often but never marked used (high heat, no payoff);
- proposals waiting for a human;
- writes the closed-loop verifier rolled back recently.

Everything here is read-only and bounded. It proposes nothing and writes
nothing; naming a new job kind from these signals remains a human decision
(M4a v1). Callers get plain JSON, so the CLI, the agent tool and a future MCP
tool all read the same shape.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT_WINDOW_DAYS = 7
DEFAULT_LIMIT = 20
DEFAULT_UNUSED_MIN = 5


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=max(0, days))).isoformat()


async def _titles(db: AsyncSession, doc_ids: list[str]) -> dict[str, str]:
    unique = list(dict.fromkeys(doc_id for doc_id in doc_ids if doc_id))
    if not unique:
        return {}
    placeholders = ", ".join(f":d{i}" for i in range(len(unique)))
    rows = (
        await db.execute(
            text(f"SELECT id, title FROM documents WHERE id IN ({placeholders})"),
            {f"d{i}": doc_id for i, doc_id in enumerate(unique)},
        )
    ).all()
    return {doc_id: title for doc_id, title in rows}


async def _quality_issues(db: AsyncSession, data_dir, limit: int) -> list[dict]:
    """Quality groups folded by kind, with a few examples each.

    The raw groups are per document/node; the agent needs the shape of the
    problem ("which detectors keep firing, on how many documents"), so the
    report aggregates by kind and keeps the largest examples.
    """
    try:
        path = Path((await db.execute(text("PRAGMA database_list"))).first()[2])
        if not str(path) or str(path) == ":memory:":
            return []
        from . import issues as issues_mod

        groups = issues_mod.quality_groups(path, data_dir)
    except Exception:  # a partial database must not take the report down
        return []

    buckets: dict[str, dict] = {}
    for group in groups:
        kind = group.get("kind") or "unknown"
        bucket = buckets.setdefault(
            kind,
            {
                "kind": kind,
                "severity": group.get("severity") or "warning",
                "groups": 0,
                "count": 0,
                "documents": set(),
                "examples": [],
            },
        )
        bucket["groups"] += 1
        bucket["count"] += int(group.get("count") or 1)
        if group.get("docId"):
            bucket["documents"].add(group["docId"])
        if len(bucket["examples"]) < 3:
            bucket["examples"].append(
                {
                    "issue_id": group.get("id"),
                    "doc_id": group.get("docId"),
                    "doc_title": group.get("docTitle"),
                    "desc": (group.get("desc") or "")[:160],
                }
            )

    ordered = sorted(
        buckets.values(), key=lambda item: (item["count"], item["groups"]), reverse=True
    )
    for bucket in ordered:
        bucket["documents"] = len(bucket["documents"])
    return ordered[: max(1, limit)]


async def _zero_hits(db: AsyncSession, days: int, limit: int) -> list[dict]:
    from .search_quality import zero_hit_queries

    try:
        return await zero_hit_queries(db, days=days, limit=limit)
    except Exception:
        return []


async def _unused_heat(
    db: AsyncSession,
    days: int,
    limit: int,
    min_searches: int,
) -> list[dict]:
    """Search-surfaced documents nobody ever marked used.

    This is ADR-0002 §11.2's heat signal in its simplest form: a document
    keeps appearing in results, but no agent or human says "this answered
    my question" (``mark_used``). Either the document is noise or the search
    terms point at it for the wrong reason — a human should look.
    """
    try:
        rows = (
            await db.execute(
                text(
                    "SELECT doc_id, COUNT(*) AS searched, "
                    "COUNT(DISTINCT query) AS queries, MAX(created_at) AS last_seen "
                    "FROM usage_log "
                    "WHERE doc_id IS NOT NULL AND rank >= 1 AND created_at >= :cutoff "
                    "GROUP BY doc_id "
                    "HAVING SUM(used) = 0 AND COUNT(*) >= :minimum "
                    "ORDER BY searched DESC, last_seen DESC LIMIT :k"
                ),
                {
                    "cutoff": _cutoff(days),
                    "minimum": max(1, min_searches),
                    "k": max(1, limit),
                },
            )
        ).mappings().all()
    except Exception:
        return []
    titles = await _titles(db, [row["doc_id"] for row in rows])
    return [
        {
            "doc_id": row["doc_id"],
            "title": titles.get(row["doc_id"], ""),
            "searched": int(row["searched"]),
            "queries": int(row["queries"]),
            "last_seen": row["last_seen"],
        }
        for row in rows
    ]


async def _pending_proposals(db: AsyncSession, limit: int) -> list[dict]:
    """Proposals with high-risk ops still waiting for a human."""
    try:
        rows = (
            await db.execute(
                text(
                    "SELECT p.id, p.strategy, p.rationale, p.created_at, "
                    "j.doc_id, d.title AS doc_title "
                    "FROM curation_proposals p "
                    "LEFT JOIN curation_jobs j ON j.id = p.job_id "
                    "LEFT JOIN documents d ON d.id = j.doc_id "
                    "WHERE p.verdict = 'pending' "
                    "ORDER BY p.created_at DESC LIMIT :k"
                ),
                {"k": max(1, limit)},
            )
        ).mappings().all()
    except Exception:
        return []
    return [
        {
            "proposal_id": row["id"],
            "strategy": row["strategy"],
            "doc_id": row["doc_id"],
            "doc_title": row["doc_title"] or "",
            "rationale": (row["rationale"] or "")[:160],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


async def _recent_rollbacks(db: AsyncSession, days: int, limit: int) -> list[dict]:
    try:
        rows = (
            await db.execute(
                text(
                    "SELECT doc_id, kind, strategy, note, created_at "
                    "FROM curation_outcomes "
                    "WHERE action = 'auto_rollback' AND created_at >= :cutoff "
                    "ORDER BY id DESC LIMIT :k"
                ),
                {"cutoff": _cutoff(days), "k": max(1, limit)},
            )
        ).mappings().all()
    except Exception:
        return []
    titles = await _titles(db, [row["doc_id"] for row in rows])
    return [
        {
            "doc_id": row["doc_id"],
            "title": titles.get(row["doc_id"], ""),
            "kind": row["kind"],
            "strategy": row["strategy"],
            "note": (row["note"] or "")[:160],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


async def _errata_counts(db: AsyncSession) -> dict[str, int]:
    try:
        rows = (
            await db.execute(
                text("SELECT status, COUNT(*) FROM errata GROUP BY status")
            )
        ).all()
    except Exception:
        return {}
    return {status: int(count) for status, count in rows}


async def collect_signals(
    db: AsyncSession,
    data_dir,
    *,
    days: int = DEFAULT_WINDOW_DAYS,
    limit: int = DEFAULT_LIMIT,
    unused_min: int = DEFAULT_UNUSED_MIN,
) -> dict:
    """The bounded, read-only diagnosis snapshot described in the module doc."""
    return {
        "window_days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "quality_issues": await _quality_issues(db, data_dir, limit),
        "zero_hit_queries": await _zero_hits(db, days, limit),
        "unused_heat": await _unused_heat(db, days, limit, unused_min),
        "pending_proposals": await _pending_proposals(db, limit),
        "auto_rollbacks": await _recent_rollbacks(db, days, limit),
        "errata": await _errata_counts(db),
    }
