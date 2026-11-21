"""Outcome ledger writes (ADR-0002 §8.1, §11).

Every curation action — applied proposal, rollback, human restore — lands
here. ``rank_strategies`` reads it later; until there are enough rows it
falls back to a cost prior.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import CurationOutcome

ACTIONS = (
    "applied",
    "partial",
    "rejected",
    "auto_rollback",
    "rolled_back",
    "restored_by_human",
    "restored_forced",
    "llm_unavailable",
)
VERDICTS = ("better", "worse", "neutral")
# Observation window (self-evolution L2): a write starts pending and is
# settled to survived after this many days unless it was rolled back.
SURVIVALS = ("pending", "survived", "regressed")
OBSERVATION_DAYS = 7.0
_ROLLBACK_ACTIONS = (
    "auto_rollback",
    "rolled_back",
    "restored_by_human",
    "restored_forced",
)


async def record_outcome(
    db: AsyncSession,
    *,
    doc_id: str,
    action: str,
    actor: str,
    issue_id: str = "",
    kind: str = "",
    strategy: str = "",
    verdict: str = "neutral",
    delta: dict | None = None,
    verify: dict | None = None,
    survival: str = "pending",
    note: str | None = None,
) -> CurationOutcome:
    if action not in ACTIONS:
        raise ValueError(f"invalid outcome action: {action}")
    if verdict not in VERDICTS:
        raise ValueError(f"invalid verdict: {verdict}")
    if survival not in SURVIVALS:
        raise ValueError(f"invalid survival: {survival}")
    row = CurationOutcome(
        doc_id=doc_id,
        issue_id=issue_id,
        kind=kind,
        strategy=strategy,
        action=action,
        verdict=verdict,
        delta_json=json.dumps(delta, ensure_ascii=False) if delta else None,
        verify_json=json.dumps(verify, ensure_ascii=False) if verify else None,
        survival=survival,
        actor=actor,
        note=note,
    )
    db.add(row)
    await db.flush()
    return row


async def mark_regressed(
    db: AsyncSession, doc_id: str, issue_id: str | None = ""
) -> CurationOutcome | None:
    """Mark the newest still-pending applied outcome for this issue regressed.

    Called when a write is undone — automatically by verification or by a
    human — so the observation window can never count it as a success.
    ``issue_id=None`` regresses every pending write on the document (used by
    whole-document baseline restores, which invalidate all of them).
    """
    conditions = [
        CurationOutcome.doc_id == doc_id,
        CurationOutcome.action.in_(("applied", "partial")),
        CurationOutcome.survival == "pending",
    ]
    if issue_id is not None:
        conditions.append(CurationOutcome.issue_id == issue_id)
    rows = (
        (
            await db.execute(
                select(CurationOutcome)
                .where(*conditions)
                .order_by(CurationOutcome.id.desc())
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return None
    for row in rows:
        row.survival = "regressed"
    await db.flush()
    return rows[0]


async def settle_outcomes(
    db: AsyncSession, days: float = OBSERVATION_DAYS
) -> int:
    """Close the observation window for writes older than ``days``.

    Pending outcomes whose document/issue later rolled back are already
    ``regressed`` (marked at rollback time); everything else that survived
    the window is marked ``survived``. Parsing uses the stored ISO strings.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    reversed_keys = {
        (row.doc_id, row.issue_id)
        for row in (
            await db.execute(
                select(CurationOutcome).where(
                    CurationOutcome.action.in_(_ROLLBACK_ACTIONS)
                )
            )
        )
        .scalars()
        .all()
    }
    pending = (
        (
            await db.execute(
                select(CurationOutcome).where(
                    CurationOutcome.action.in_(("applied", "partial")),
                    CurationOutcome.survival == "pending",
                )
            )
        )
        .scalars()
        .all()
    )
    settled = 0
    for row in pending:
        try:
            created = datetime.fromisoformat(row.created_at)
        except (TypeError, ValueError):
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created >= cutoff:
            continue
        row.survival = (
            "regressed" if (row.doc_id, row.issue_id) in reversed_keys else "survived"
        )
        settled += 1
    if settled:
        await db.flush()
    return settled


async def strategy_report(db: AsyncSession) -> list[dict]:
    """Per-strategy tallies for ``python -m app.curator report``."""
    rows = (
        (await db.execute(select(CurationOutcome).order_by(CurationOutcome.id)))
        .scalars()
        .all()
    )
    buckets: dict[str, dict] = {}
    for row in rows:
        bucket = buckets.setdefault(
            row.strategy or row.action,
            {"strategy": row.strategy or row.action, "applied": 0, "better": 0,
             "worse": 0, "neutral": 0, "delta": {}},
        )
        bucket["applied"] += 1
        bucket[row.verdict] = bucket.get(row.verdict, 0) + 1
        if row.delta_json:
            try:
                for key, value in json.loads(row.delta_json).items():
                    if isinstance(value, (int, float)):
                        bucket["delta"].setdefault(key, []).append(value)
            except ValueError:
                pass
    report = []
    for bucket in buckets.values():
        averages = {
            key: round(sum(values) / len(values), 3)
            for key, values in bucket["delta"].items()
            if values
        }
        report.append({**bucket, "delta": averages})
    return sorted(report, key=lambda item: -item["applied"])


async def rank_strategies(db: AsyncSession, kind: str) -> list[str]:
    """Strategies ordered by success rate for one issue kind.

    Success = better / (better + worse). A strategy with fewer than 3
    decided samples is not trusted over the cost prior: with no evidence,
    the cheap deterministic pass runs first. Once there is evidence, a
    strategy that keeps making things worse drops behind one that works.

    Only applied/partial outcomes carry a verdict; rejected proposals never
    ran, so counting them would penalise a strategy for the reviewer's
    decision rather than its own effect.
    """
    rows = (
        (
            await db.execute(
                select(CurationOutcome).where(CurationOutcome.kind == kind)
            )
        )
        .scalars()
        .all()
    )
    # Cost prior when samples are thin: cheap deterministic work first.
    # rule_summary is deliberately absent — document summaries are not
    # generated (ADR-0002 §7.2 revised).
    prior = ["rule_merge", "rule_page_ranges", "agent_chunk", "agent_toc"]
    tallies: dict[str, dict] = {}
    for row in rows:
        if not row.strategy or row.action not in ("applied", "partial"):
            continue
        tally = tallies.setdefault(row.strategy, {"better": 0, "worse": 0})
        if row.verdict in tally:
            tally[row.verdict] += 1

    scored: list[tuple] = []
    for strategy, tally in tallies.items():
        samples = tally["better"] + tally["worse"]
        rate = (tally["better"] / samples) if samples else 0.5
        # (no-evidence first key, then worst-rate first, then name)
        scored.append((samples < 3, -rate, strategy))
    scored.sort()
    ordered = [strategy for _, _, strategy in scored]
    return ordered + [strategy for strategy in prior if strategy not in ordered]


def strategies_for(kind: str, ranked: list[str]) -> list[str]:
    """The strategies that can act on ``kind``, in ranked order.

    Ranking is global; applicability is per kind. The result keeps the
    *rank* order (so a strategy that keeps failing sinks), but only among
    strategies that can actually address this kind — a short-chunks job is
    never offered rule_page_ranges just because that ranks well overall.

    Strategies with no evidence yet fall back to the kind's cost prior, so
    the deterministic pass is tried before the agent one.
    """
    applicable = _APPLICABLE.get(kind, ())
    if not applicable:
        return []
    ordered = [name for name in ranked if name in applicable]
    # A strategy with no outcome history still appears (it has not been
    # measured yet); rank_strategies appends the prior for exactly this.
    for name in applicable:
        if name not in ordered:
            ordered.append(name)
    return ordered


_APPLICABLE: dict[str, tuple[str, ...]] = {
    "short-chunks": ("rule_merge", "agent_chunk"),
    "missing-pages": ("rule_page_ranges", "agent_toc"),
}
