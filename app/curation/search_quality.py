"""Search-quality loop: record zero-hit queries and replay them.

``docs/self-evolution.md`` §5.4.2: the library should learn from what people
asked for and could not find. Before this module a miss left nothing behind —
``usage_log`` only received one row per hit — so the most useful signal (the
unanswered query) was invisible.

Two halves live here:

- ``zero_hit_queries`` groups the ``rank=0`` rows written by
  ``usage_log_write`` into a ranked report;
- ``replay_zero_hits`` runs those queries against the current index and reports
  which ones the library can answer now. That is the measurement a keyword,
  synonym or TOC fix is judged by ("recovered" vs "still zero"), and it never
  writes: replaying a query must not feed itself back into the log.

Proposing the fix (``zero-result-queries`` jobs, ``set_keywords``/synonym ops)
is the next half of the loop and is deliberately not in this module: it writes
through the normal op path, not from here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ``usage_log_write`` records a miss as one row with this rank and no document.
# Hit rows keep rank >= 1, so this identifies misses without a schema change.
MISS_RANK = 0

DEFAULT_WINDOW_DAYS = 7
DEFAULT_LIMIT = 50
DEFAULT_TOP_K = 8
# Candidate mapping for a still-zero query (self-evolution §5.4.2): a document
# is worth a look when the query and its title/keywords share at least half
# the query terms — but not ALL of them. All of them means the document
# fallback already answers the query (``app/index/fts.py``), so no job is
# needed; none of them means the query says nothing about this document.
CANDIDATE_MIN_COVERAGE = 0.5
CANDIDATE_PER_QUERY = 3


async def zero_hit_queries(
    db: AsyncSession,
    *,
    days: int = DEFAULT_WINDOW_DAYS,
    limit: int = DEFAULT_LIMIT,
) -> list[dict]:
    """Missed queries in the window, most frequent first.

    ``misses`` counts how often the query missed; ``last_seen`` is when it was
    last tried. Grouping by ``(query, tool)`` keeps the same words from MCP and
    the Web separate — they usually mean different contexts.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=max(0, days))
    ).isoformat()
    rows = (
        (
            await db.execute(
                text(
                    "SELECT query, tool, COUNT(*) AS misses, "
                    "MAX(created_at) AS last_seen "
                    "FROM usage_log "
                    "WHERE rank = :miss AND created_at >= :cutoff "
                    "GROUP BY query, tool "
                    "ORDER BY misses DESC, last_seen DESC LIMIT :k"
                ),
                {"miss": MISS_RANK, "cutoff": cutoff, "k": max(1, limit)},
            )
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


async def replay_zero_hits(
    db: AsyncSession,
    *,
    days: int = DEFAULT_WINDOW_DAYS,
    limit: int = DEFAULT_LIMIT,
    top_k: int = DEFAULT_TOP_K,
) -> dict:
    """Re-run recorded misses against the current index.

    A query is ``recovered`` when it now returns at least one hit. The result
    carries the hit count and the documents found, so the caller can see
    whether a fix helped the right document or merely surfaced noise.
    """
    from ..index.fts import search_chunks

    misses = await zero_hit_queries(db, days=days, limit=limit)
    queries: list[dict] = []
    doc_ids: list[str] = []
    for row in misses:
        hits = await search_chunks(db, row["query"], top_k=max(1, top_k))
        found = list(
            dict.fromkeys(hit["doc_id"] for hit in hits if hit.get("doc_id"))
        )
        doc_ids.extend(found)
        queries.append(
            {
                "query": row["query"],
                "tool": row["tool"],
                "misses": int(row["misses"]),
                "last_seen": row["last_seen"],
                "hits": len(hits),
                "doc_ids": found,
                "recovered": bool(hits),
            }
        )

    titles: dict[str, str] = {}
    unique = list(dict.fromkeys(doc_ids))
    if unique:
        placeholders = ", ".join(f":d{i}" for i in range(len(unique)))
        params = {f"d{i}": doc_id for i, doc_id in enumerate(unique)}
        for doc_id, title in (
            await db.execute(
                text(f"SELECT id, title FROM documents WHERE id IN ({placeholders})"),
                params,
            )
        ).all():
            titles[doc_id] = title
    for item in queries:
        item["docs"] = [
            {"doc_id": doc_id, "title": titles.get(doc_id, "")}
            for doc_id in item["doc_ids"]
        ]

    recovered = sum(1 for item in queries if item["recovered"])
    return {
        "window_days": days,
        "replayed": len(queries),
        "recovered": recovered,
        "still_zero": len(queries) - recovered,
        "queries": queries,
    }


async def candidate_pairs(
    db: AsyncSession,
    *,
    days: int = DEFAULT_WINDOW_DAYS,
    miss_limit: int = DEFAULT_LIMIT,
    per_query: int = CANDIDATE_PER_QUERY,
) -> list[dict]:
    """Map still-zero queries to documents worth a curator look.

    The mapping is deliberately shallow and explainable: jieba tokens of the
    query versus jieba tokens of the document title + keywords. A candidate
    shares at least half the query terms but not all of them — all of them
    would already be answered by the document fallback, and fewer than half
    says the query is not about that document. The returned ``shared`` list
    is the evidence an agent sees and a human can check.

    A query with a single token is never mapped: a one-token query has no
    "half", so either the document matches it (fallback answers) or it does
    not (no basis for a job).
    """
    from ..index.fts import seg_query

    misses = await zero_hit_queries(db, days=days, limit=miss_limit)
    by_query: dict[str, dict] = {}
    for row in misses:
        item = by_query.setdefault(
            row["query"],
            {"query": row["query"], "misses": 0, "tools": [], "last_seen": row["last_seen"]},
        )
        item["misses"] += int(row["misses"])
        if row["tool"] not in item["tools"]:
            item["tools"].append(row["tool"])
        if row["last_seen"] > item["last_seen"]:
            item["last_seen"] = row["last_seen"]
    if not by_query:
        return []

    docs = (
        (
            await db.execute(
                text(
                    "SELECT id, title, keywords FROM documents WHERE status = 'ready'"
                )
            )
        )
        .mappings()
        .all()
    )
    doc_tokens = {
        doc["id"]: set(seg_query(f"{doc['title'] or ''} {doc['keywords'] or ''}"))
        for doc in docs
    }

    pairs: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for query, item in by_query.items():
        tokens = set(seg_query(query))
        if len(tokens) < 2:
            continue
        candidates: list[dict] = []
        for doc in docs:
            shared = tokens & doc_tokens[doc["id"]]
            coverage = len(shared) / len(tokens)
            if not shared or coverage < CANDIDATE_MIN_COVERAGE or coverage >= 1.0:
                continue
            candidates.append(
                {
                    "doc_id": doc["id"],
                    "title": doc["title"] or "",
                    "keywords": doc["keywords"] or "",
                    "shared": sorted(shared),
                    "coverage": round(coverage, 2),
                }
            )
        candidates.sort(
            key=lambda candidate: (
                -candidate["coverage"],
                -len(candidate["shared"]),
                candidate["title"],
            )
        )
        for candidate in candidates[: max(1, per_query)]:
            key = (candidate["doc_id"], query)
            if key in seen:
                continue
            seen.add(key)
            pairs.append(
                {
                    "query": query,
                    "misses": item["misses"],
                    "tools": item["tools"],
                    "last_seen": item["last_seen"],
                    **candidate,
                }
            )
    return pairs


async def candidate_queries_for_doc(
    db: AsyncSession,
    doc_id: str,
    *,
    days: int = DEFAULT_WINDOW_DAYS,
    limit: int = CANDIDATE_PER_QUERY,
) -> list[dict]:
    """The candidate pairs that point at one document (agent tool helper)."""
    pairs = await candidate_pairs(db, days=days)
    return [pair for pair in pairs if pair["doc_id"] == doc_id][: max(1, limit)]
