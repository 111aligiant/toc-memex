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
