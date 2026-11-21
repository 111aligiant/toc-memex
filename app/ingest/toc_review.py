"""纲目复审：locate TOC nodes whose titles are not on their declared pages.

Design: ``docs/self-evolution.md`` §5.3 / ``docs/m3-toc-review.md``.
Deterministic first pass — no LLM: for every node whose normalized title
does not occur on any page of its declared range, scan a window around the
declared start. A title found on exactly one page yields a ``set_page`` op
whose evidence is the title itself (so ``validate_op`` can ground it);
multiple candidate pages yield no op — silence over guessing.

Parent/child range constraints are deliberately not re-implemented here:
the existing ``validate_op`` gate drops proposals that cross a parent or
strand a child, with the reason recorded.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..curation.ops import _norm
from ..models import Document, TocNode

# Pages each side of the declared start where a title may be re-found.
REVIEW_WINDOW = 10


async def review_document(
    db: AsyncSession,
    doc: Document,
    pages: dict[int, str],
    *,
    limit: int = 20,
    window: int = REVIEW_WINDOW,
) -> dict:
    """Locate mis-placed nodes; return proposed ops plus a full tally.

    ``pages`` maps page index → page text (usually from the OCR cache).
    Returns ``{"doc_id", "checked", "ok", "proposed", "ambiguous",
    "unlocatable", "skipped"}``; an empty ``pages`` means the document has
    no cache to review against and everything lands in ``skipped``.
    """
    nodes = (
        (
            await db.execute(
                select(TocNode)
                .where(TocNode.doc_id == doc.id)
                .order_by(TocNode.page_start, TocNode.id)
            )
        )
        .scalars()
        .all()
    )
    organized = {
        index: _norm(text) for index, text in (pages or {}).items() if text
    }
    report: dict = {
        "doc_id": doc.id,
        "checked": 0,
        "ok": 0,
        "catalog_pages": [],
        "proposed": [],
        "ambiguous": [],
        "unlocatable": [],
        "skipped": 0,
    }
    if not organized:
        report["skipped"] = len(nodes)
        return report

    # The printed TOC page(s) contain most node titles by definition. They
    # are not valid relocation targets — without this, a mis-placed node
    # "moves" to the catalog page and the fix is nonsense (validate_op would
    # drop it, but the proposal should never be made).
    titles = {
        _norm(node.title or "") for node in nodes if len(_norm(node.title or "")) >= 2
    }
    catalog_pages: set[int] = set()
    if titles:
        threshold = max(3, int(len(titles) * 0.2))
        for index, text in organized.items():
            hits = sum(1 for title in titles if title in text)
            if hits >= threshold:
                catalog_pages.add(index)
    report["catalog_pages"] = sorted(catalog_pages)

    last_page = max(organized) if organized else 0
    if doc.page_count:
        last_page = max(last_page, doc.page_count - 1)

    for node in nodes:
        if len(report["proposed"]) >= limit:
            break
        needle = _norm(node.title or "")
        if len(needle) < 2 or node.page_start is None:
            report["skipped"] += 1
            continue
        start = node.page_start
        end = node.page_end if node.page_end is not None else start
        declared = [
            organized[index]
            for index in range(start, end + 1)
            if index in organized
        ]
        if not declared:
            report["skipped"] += 1
            continue
        report["checked"] += 1
        if any(needle in text for text in declared):
            report["ok"] += 1
            continue
        lo = max(0, start - max(0, window))
        hi = min(last_page, start + max(0, window))
        hits = [
            index
            for index in range(lo, hi + 1)
            if index in organized
            and index not in catalog_pages
            and needle in organized[index]
        ]
        if len(hits) == 1:
            found = hits[0]
            report["proposed"].append(
                {
                    "node_id": node.node_id,
                    "title": node.title,
                    "declared": [start, end],
                    "found": found,
                    "op": {
                        "op": "set_page",
                        "node_id": node.node_id,
                        "page_start": found,
                        "page_end": max(end, found),
                        "evidence": node.title,
                    },
                }
            )
        elif hits:
            report["ambiguous"].append(
                {"node_id": node.node_id, "title": node.title, "hits": hits}
            )
        else:
            report["unlocatable"].append(
                {"node_id": node.node_id, "title": node.title, "declared": [start, end]}
            )
    return report
