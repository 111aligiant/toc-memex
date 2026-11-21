"""Closed-loop verification: measure a document before/after a write.

Part of the self-evolution design (``docs/self-evolution.md`` §5.1). Every
curation write is measured with a small set of deterministic verifiers —
no LLM, read-only — and a degraded result authorises an automatic rollback
through the same snapshot/restore path as a manual undo.

Degradation is deliberately narrow so a legitimate fix is never rolled
back by noise:

- TOC structural errors rose (the hard invariant already enforced in
  ``apply_ops``; kept here so the verdict carries the numbers);
- the share of nodes whose title actually appears on their declared pages
  dropped by more than ``PAGE_RATIO_TOLERANCE``;
- the full-text index no longer matches the chunk rows;
- short chunks or heading-only chunks rose.

Improvements are reported for the verdict but never block anything.
"""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..index.activity import lesson_write
from ..models import TocNode
from . import strategies
from .ops import _node_issues, _norm
from .outcomes import mark_regressed, record_outcome

# A page-evidence ratio may wobble by this much before it counts as worse:
# one node flip in a 50-node document should not spam rollbacks.
PAGE_RATIO_TOLERANCE = 0.02
# Historical-query replay: how many past queries to replay and where a rank
# has to move before it counts as a regression. A write that shifts one
# result by a place is noise; losing the document for a query is not.
SEARCH_REPLAY_LIMIT = 5
SEARCH_REPLAY_TOP_K = 10
SEARCH_RANK_TOLERANCE = 1.0

VERDICTS = ("better", "worse", "neutral")


async def snapshot_metrics(ctx) -> dict:
    """The verifier readings for one document, as plain JSON-safe data.

    A verifier that fails (missing cache, odd row) reports ``None`` for its
    metric instead of raising — verification must never fail the write.
    """
    metrics: dict = {}
    nodes: list[TocNode] | None = None
    try:
        nodes = await ctx.nodes()
        issues = _node_issues(nodes)
        metrics["toc"] = {
            "errors": sum(1 for issue in issues if issue.severity == "error"),
            "warnings": sum(1 for issue in issues if issue.severity == "warning"),
        }
    except Exception:
        metrics["toc"] = None
    metrics["pages"] = _page_evidence(nodes or [], ctx)
    metrics["search"] = await _search_replay(ctx)
    try:
        chunks = await ctx.chunks()
        node_map = {node.node_id: node for node in (nodes or await ctx.nodes())}
        metrics["blocks"] = {
            "short": sum(
                1
                for chunk in chunks
                if len(chunk.content or "") < strategies.SHORT_CHUNK_CHARS
            ),
            "heading_only": sum(
                1
                for chunk in chunks
                if strategies.is_heading_only(
                    chunk.content or "", node_map.get(chunk.node_id or "")
                )
            ),
        }
    except Exception:
        metrics["blocks"] = None
    try:
        chunks_n = (
            await ctx.db.execute(
                text("SELECT count(*) FROM chunks WHERE doc_id = :doc"),
                {"doc": ctx.doc.id},
            )
        ).scalar_one()
        fts_n = (
            await ctx.db.execute(
                text("SELECT count(*) FROM chunk_fts WHERE doc_id = :doc"),
                {"doc": ctx.doc.id},
            )
        ).scalar_one()
        metrics["fts"] = {
            "chunks": int(chunks_n),
            "fts_rows": int(fts_n),
            "consistent": int(chunks_n) == int(fts_n),
        }
    except Exception:
        metrics["fts"] = None
    return metrics


def _page_evidence(nodes: list[TocNode], ctx) -> dict | None:
    """Share of nodes whose title text occurs on a page in their range.

    ``None`` when the OCR cache is empty (nothing to check against) — a
    missing cache is not a quality signal either way.
    """
    if not ctx.pages:
        return None
    normalized_pages = {
        index: _norm(value) for index, value in ctx.pages.items() if value
    }
    if not normalized_pages:
        return None
    checked = 0
    hit = 0
    for node in nodes:
        if node.page_start is None:
            continue
        start = node.page_start
        end = node.page_end if node.page_end is not None else node.page_start
        ranged = [
            normalized_pages[index]
            for index in range(start, end + 1)
            if index in normalized_pages
        ]
        if not ranged:
            continue
        needle = _norm(node.title or "")
        if len(needle) < 2:
            continue
        checked += 1
        if any(needle in page for page in ranged):
            hit += 1
    if not checked:
        return None
    return {
        "checked": checked,
        "on_page": hit,
        "ratio": round(hit / checked, 4),
    }


async def _search_replay(ctx) -> dict | None:
    """Replay this document's historical queries against the current index.

    ``usage_log`` already records which real queries surfaced this document.
    Re-running the most recent ones is the regression guard for relevance: a
    write that merges away the chunk those queries matched, or pushes the
    document out of the top results, is worse even when the TOC and chunk
    metrics look fine (``docs/self-evolution.md`` §5.1.1, §5.4.2).

    ``None`` when the document has no recorded queries — absence of history is
    not a verdict. A failed replay drops that query rather than failing the
    write.
    """
    try:
        rows = (
            await ctx.db.execute(
                text(
                    "SELECT query, MAX(id) AS last_id FROM usage_log "
                    "WHERE doc_id = :doc AND rank >= 1 GROUP BY query "
                    "ORDER BY last_id DESC LIMIT :k"
                ),
                {"doc": ctx.doc.id, "k": SEARCH_REPLAY_LIMIT},
            )
        ).all()
    except Exception:
        return None
    if not rows:
        return None

    from ..index.fts import search_chunks

    ranks: list[int] = []
    for query, _last_id in rows:
        try:
            hits = await search_chunks(ctx.db, query, top_k=SEARCH_REPLAY_TOP_K)
        except Exception:
            continue
        rank = next(
            (
                position
                for position, hit in enumerate(hits, start=1)
                if hit.get("doc_id") == ctx.doc.id
            ),
            None,
        )
        # A miss counts as one place past the window, so "still present but
        # lower" and "gone" both move the rank in the same direction.
        ranks.append(rank if rank is not None else SEARCH_REPLAY_TOP_K + 1)
    if not ranks:
        return None
    return {
        "queries": len(ranks),
        "hit_docs": sum(1 for rank in ranks if rank <= SEARCH_REPLAY_TOP_K),
        "mean_rank": round(sum(ranks) / len(ranks), 2),
        "top_k": SEARCH_REPLAY_TOP_K,
    }


def compare_metrics(before: dict, after: dict) -> dict:
    """Verdict for one write: worse authorises rollback, better/neutral keep.

    Returns ``{"verdict", "reasons", "deltas", "before", "after"}``; the
    whole payload is what ``CurationOutcome.verify_json`` stores.
    """
    reasons: list[str] = []
    improved: list[str] = []
    deltas: dict = {}

    def _pick(metrics: dict, name: str) -> dict | None:
        value = metrics.get(name) if metrics else None
        return value if isinstance(value, dict) else None

    toc_before, toc_after = _pick(before, "toc"), _pick(after, "toc")
    if toc_before and toc_after:
        delta = toc_after["errors"] - toc_before["errors"]
        deltas["tocErrors"] = delta
        if delta > 0:
            reasons.append(f"TOC errors {toc_before['errors']} → {toc_after['errors']}")
        elif delta < 0:
            improved.append("toc-errors")

    pages_before, pages_after = _pick(before, "pages"), _pick(after, "pages")
    if pages_before and pages_after:
        delta = round(pages_after["ratio"] - pages_before["ratio"], 4)
        deltas["pageRatio"] = delta
        if delta < -PAGE_RATIO_TOLERANCE:
            reasons.append(
                f"title-on-page share {pages_before['ratio']:.2f} → {pages_after['ratio']:.2f}"
            )
        elif delta > PAGE_RATIO_TOLERANCE:
            improved.append("page-evidence")

    search_before, search_after = _pick(before, "search"), _pick(after, "search")
    if search_before and search_after:
        hits_delta = search_after["hit_docs"] - search_before["hit_docs"]
        rank_delta = round(search_after["mean_rank"] - search_before["mean_rank"], 2)
        deltas["searchHits"] = hits_delta
        deltas["searchRank"] = rank_delta
        problems: list[str] = []
        if hits_delta < 0:
            problems.append(
                f"historical queries finding this doc "
                f"{search_before['hit_docs']} → {search_after['hit_docs']}"
            )
        if rank_delta > SEARCH_RANK_TOLERANCE:
            problems.append(
                f"mean replay rank {search_before['mean_rank']:.2f} → "
                f"{search_after['mean_rank']:.2f}"
            )
        if problems:
            reasons.append("; ".join(problems))
        elif hits_delta > 0 or rank_delta < -SEARCH_RANK_TOLERANCE:
            improved.append("search-replay")

    blocks_before, blocks_after = _pick(before, "blocks"), _pick(after, "blocks")
    if blocks_before and blocks_after:
        for key, label in (("short", "short chunks"), ("heading_only", "heading-only chunks")):
            delta = blocks_after[key] - blocks_before[key]
            deltas[key] = delta
            if delta > 0:
                reasons.append(f"{label} {blocks_before[key]} → {blocks_after[key]}")
            elif delta < 0:
                improved.append(key)

    fts_before, fts_after = _pick(before, "fts"), _pick(after, "fts")
    if fts_before and fts_after:
        if fts_before.get("consistent") and not fts_after.get("consistent"):
            reasons.append(
                f"FTS out of sync ({fts_after['chunks']} chunks vs {fts_after['fts_rows']} rows)"
            )
        elif not fts_before.get("consistent") and fts_after.get("consistent"):
            improved.append("fts")

    if reasons:
        verdict = "worse"
    elif improved:
        verdict = "better"
    else:
        verdict = "neutral"
    return {
        "verdict": verdict,
        "reasons": reasons,
        "deltas": deltas,
        "before": before,
        "after": after,
    }


async def record_auto_rollback(
    db: AsyncSession,
    *,
    doc_id: str,
    issue_id: str = "",
    kind: str = "",
    strategy: str = "",
    verify: dict,
    proposal_id: str = "",
    actor: str = "curator",
) -> None:
    """Ledger + negative lesson for one automatically rolled-back write.

    Caller commits. The original applied outcome (if the caller already
    recorded one) is marked ``regressed`` so the observation window cannot
    later count this write as a success.
    """
    reasons = "; ".join(verify.get("reasons") or []) or "指标变差"
    await record_outcome(
        db,
        doc_id=doc_id,
        issue_id=issue_id,
        kind=kind,
        strategy=strategy,
        action="auto_rollback",
        verdict="worse",
        actor=actor,
        verify=verify,
        note=f"verification degraded: {reasons}"
        + (f"; proposal {proposal_id}" if proposal_id else ""),
    )
    await mark_regressed(db, doc_id, issue_id)
    try:
        await lesson_write(
            db,
            trigger=f"{kind or 'curation'}：{strategy or '策略'} 写入被复测回滚",
            issue=f"复测判 worse：{reasons}",
            solution=(
                "该策略在这种文档上使指标变差，已自动回滚到写前状态；"
                "复用前先看 outcome.verify_json 的前后指标并人工复核"
            ),
            keywords=f"{kind},{strategy},auto-rollback,verify",
            author_agent=actor.split(":")[0] or "curator",
        )
    except Exception:
        # A lesson must never fail the caller's transaction; the outcome
        # row above is the machine record that matters.
        pass
    await db.flush()


def verify_summary(verify: dict | None) -> str:
    """One-line human summary for notes and CLI output."""
    if not verify:
        return "unverified"
    return f"{verify.get('verdict')}: " + ("; ".join(verify.get("reasons") or []) or "no change")


def dumps(verify: dict | None) -> str | None:
    return json.dumps(verify, ensure_ascii=False) if verify else None