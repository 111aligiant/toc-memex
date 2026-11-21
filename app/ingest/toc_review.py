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


# ── work orders: 编修 asks 纲目 for a structural review (M3b) ──────────

GANGMU_TASK_KIND = "gangmu-review"
GANGMU_TASK_PREFIX = "纲目复审："


async def _subtree_node_ids(db: AsyncSession, doc_id: str, root: str) -> set[str]:
    nodes = (
        (await db.execute(select(TocNode).where(TocNode.doc_id == doc_id)))
        .scalars()
        .all()
    )
    children: dict[str, list[str]] = {}
    for node in nodes:
        if node.parent_id:
            children.setdefault(node.parent_id, []).append(node.node_id)
    seen: set[str] = set()
    stack = [root]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(children.get(current, []))
    return seen


async def consume_review_task(
    db: AsyncSession,
    data_dir,
    task_id: str,
    *,
    limit: int = 20,
    window: int = REVIEW_WINDOW,
) -> dict:
    """Run a ``gangmu-review`` work order: review the document, file a proposal.

    The work order carries its document in ``goal`` as JSON
    (``{"doc_id", "node_id", "reason"}``), because a task may span agents but
    this one is about one document. A disputed subtree root limits the review
    to that subtree; without it the whole document is reviewed.

    The located ``set_page`` ops go through ``Dispatcher.propose_ops`` — high
    risk, so they wait for a human exactly like a curator proposal. The task is
    closed and a hand-off note anchored to both the task and the document
    records what happened, even when nothing was locatable.
    """
    import json

    from ..curation.dispatch import Dispatcher
    from ..curation.ops import build_context
    from ..index.activity import AgentNote, task_get, task_out, task_update

    task = await task_get(db, task_id)
    if task is None:
        raise LookupError(f"task not found: {task_id}")
    if task.kind != GANGMU_TASK_KIND:
        raise ValueError(f"not a {GANGMU_TASK_KIND} task: {task.kind}")
    if task.status in ("done", "archived"):
        return {"task": task_out(task), "skipped": f"already {task.status}"}
    try:
        payload = json.loads(task.goal or "{}")
    except ValueError:
        payload = {}
    doc_id = payload.get("doc_id") or ""
    root = payload.get("node_id") or None
    doc = await db.get(Document, doc_id) if doc_id else None
    if doc is None:
        raise LookupError(f"document not found for task {task_id}: {doc_id!r}")

    await task_update(db, task_id, status="doing", owner_agent="gangmu")
    ctx = await build_context(db, doc, data_dir)
    report = await review_document(
        db, doc, ctx.pages, limit=limit, window=window
    )

    ops = [entry["op"] for entry in report["proposed"]]
    if root:
        subtree = await _subtree_node_ids(db, doc.id, root)
        ops = [op for op in ops if op.get("node_id") in subtree]
        report["filtered_to"] = root
        report["filtered_ops"] = len(ops)

    proposal = None
    if ops:
        dispatcher = Dispatcher(db, data_dir, doc.id)
        proposal = await dispatcher.propose_ops(
            ops,
            rationale=(
                f"纲目复审工单 {task_id}："
                f"{(payload.get('reason') or '节点标题不在声明页')[:300]}"
            ),
        )
    await task_update(db, task_id, status="done", owner_agent="gangmu")
    try:
        db.add(
            AgentNote(
                author="gangmu",
                kind="handoff",
                doc_id=doc.id,
                task_id=task.id,
                body=(
                    f"纲目复审 {task_id}：检查 {report['checked']} 个节点，"
                    f"定位 {len(ops)} 个可修节点，"
                    f"模糊 {len(report['ambiguous'])}，无法定位 {len(report['unlocatable'])}"
                    + (f"；提案 {proposal['proposal_id']} 待批" if proposal else "；未产生提案")
                ),
            )
        )
        await db.flush()
    except Exception:  # a note must not fail the work order
        pass
    return {
        "task": task_out(task),
        "doc_id": doc.id,
        "title": doc.title,
        "report": report,
        "proposal": proposal,
    }
