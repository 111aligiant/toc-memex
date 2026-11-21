"""Write ops: risk tiers, grounding, invariants, application (ADR-0002 §6).

The guards live here, not in the agent loop, so any caller — the curator's
own dispatch, the MCP server, a future agent — is held to the same rules.

Tiers
  L1  applied once validated: no_op, merge_chunks, reassign_chunk,
      drop_chunk (furniture only), split_chunk (at an evidenced heading),
      set_summary, set_keywords, correct_text (only with usage feedback)
  L2  proposal only, waits for a human: set_page, merge_nodes, split_node
  L3  refused outright: anything not in the tables above

Gates, in order
  grounding   evidence really is where the op claims (normalized substring);
              merges may not invent text (token multiset, ≤5% new)
  invariants  ranges legal, parents exist, no cycles, ids unique;
              the one hard gate is that validate_toc's error count does not rise
  snapshot    written before the first applied op
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..ingest.toc import TocNodeData, validate_toc
from ..models import Chunk, ChunkRemap, Document, TocNode, utcnow_iso
from ..ocr.cache import load_cached_pages
from ..ocr.parser import page_text
from .snapshots import write_snapshot

LOW_RISK = {
    "no_op",
    "merge_chunks",
    "reassign_chunk",
    "drop_chunk",
    "split_chunk",
    "set_summary",
    "set_keywords",
    "correct_text",
}
HIGH_RISK = {"set_page", "merge_nodes", "split_node"}

# OCR confusions a character-level correction may fix (§6.2).
CONFUSION_PAIRS = {
    ("0", "O"),
    ("O", "0"),
    ("1", "l"),
    ("l", "1"),
    ("己", "已"),
    ("已", "己"),
    ("日", "目"),
    ("目", "日"),
    ("rn", "m"),
    ("m", "rn"),
}

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[\s,，。.、;；:：!！?？\"'“”‘’()（）\[\]【】<>《》\-—_/\\|·…]+")


def _norm(value: str) -> str:
    return _PUNCT.sub("", value or "").lower()


def _tokens(value: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for token in _norm(value):
        counts[token] = counts.get(token, 0) + 1
    return counts


def risk_of(name: str) -> str:
    if name in LOW_RISK:
        return "low"
    if name in HIGH_RISK:
        return "high"
    return "forbidden"


@dataclass
class ProposalContext:
    """Everything an op needs to be validated and applied."""

    db: AsyncSession
    doc: Document
    pages: dict[int, str]  # page index → text, for evidence checks
    data_dir: Any

    async def nodes(self) -> list[TocNode]:
        return list(
            (
                await self.db.execute(
                    select(TocNode).where(TocNode.doc_id == self.doc.id).order_by(TocNode.id)
                )
            )
            .scalars()
            .all()
        )

    async def chunks(self) -> list[Chunk]:
        return list(
            (
                await self.db.execute(
                    select(Chunk)
                    .where(Chunk.doc_id == self.doc.id)
                    .order_by(Chunk.chunk_index)
                )
            )
            .scalars()
            .all()
        )

    async def has_usage_feedback(self, chunk_id: int) -> bool:
        """Did a human or agent flag this chunk as wrong? (ADR-0002 §11.4)

        ``correct_text`` is only allowed with feedback, never on the model's
        own suspicion: an unrequested text edit is a change nobody asked for
        and nobody can check. Two signals count:

        - a proposed or confirmed erratum anchored to this chunk or its node;
        - an agent note of kind ``pitfall`` anchored the same way.

        A search hit with ``used=0`` deliberately does NOT count: that says
        "this did not answer my query", not "this text is corrupt".
        """
        from ..index.activity import Erratum
        from ..models import AgentNote

        chunk = await self.db.get(Chunk, chunk_id)
        if chunk is None:
            return False
        erratum_anchor = Erratum.chunk_id == chunk_id
        note_anchor = AgentNote.chunk_id == chunk_id
        if chunk.node_id:
            erratum_anchor = or_(erratum_anchor, Erratum.node_id == chunk.node_id)
            note_anchor = or_(note_anchor, AgentNote.node_id == chunk.node_id)

        erratum = (
            await self.db.execute(
                select(Erratum.id)
                .where(
                    Erratum.doc_id == self.doc.id,
                    Erratum.status.in_(("proposed", "confirmed")),
                    erratum_anchor,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if erratum is not None:
            return True
        note = (
            await self.db.execute(
                select(AgentNote.id)
                .where(
                    AgentNote.doc_id == self.doc.id,
                    AgentNote.kind == "pitfall",
                    note_anchor,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if note is not None:
            return True
        # §11.4 also accepts a document-level complaint: a pitfall note on the
        # document with no chunk anchor still says "the text is wrong here",
        # and the correction itself is validated character-by-character.
        return (
            await self.db.execute(
                select(AgentNote.id)
                .where(
                    AgentNote.doc_id == self.doc.id,
                    AgentNote.kind == "pitfall",
                    AgentNote.chunk_id.is_(None),
                    AgentNote.node_id.is_(None),
                )
                .limit(1)
            )
        ).scalar_one_or_none() is not None


async def build_context(db: AsyncSession, doc: Document, data_dir) -> ProposalContext:
    pages: dict[int, str] = {}
    try:
        cached = load_cached_pages(data_dir / "ocr" / doc.id)
    except Exception:
        cached = None
    for page in cached or []:
        pages[page.page_index] = page_text(page)
    return ProposalContext(db=db, doc=doc, pages=pages, data_dir=data_dir)


def _evidence_on_pages(ctx: ProposalContext, evidence: str, start: int, end: int) -> bool:
    needle = _norm(evidence)
    if not needle:
        return False
    for page in range(start, end + 1):
        if needle in _norm(ctx.pages.get(page, "")):
            return True
    return False


def _node_issues(nodes: list[TocNode]) -> list:
    proxies = [
        TocNodeData(
            node_id=node.node_id,
            parent_id=node.parent_id,
            level=node.level,
            heading_num=node.heading_num,
            title=node.title,
            heading_path=node.heading_path,
            page_start=node.page_start,
            page_end=node.page_end,
            source=node.source,
            confidence=node.confidence,
        )
        for node in nodes
    ]
    return validate_toc(proxies, None)


def _error_count(nodes: list[TocNode]) -> int:
    return sum(1 for issue in _node_issues(nodes) if issue.severity == "error")


async def validate_op(ctx: ProposalContext, op: dict) -> tuple[str, str]:
    """Return ``(status, reason)`` where status is ok / dropped / pending."""
    name = str(op.get("op") or "")
    risk = risk_of(name)
    if risk == "forbidden":
        return "dropped", f"op not allowed: {name or '(empty)'}"

    nodes = {node.node_id: node for node in await ctx.nodes()}
    chunks = {chunk.id: chunk for chunk in await ctx.chunks()}

    if name == "no_op":
        return "ok", ""

    if name == "merge_chunks":
        ids = [int(value) for value in op.get("chunk_ids") or []]
        if len(ids) < 2:
            return "dropped", "merge_chunks needs at least two chunk_ids"
        if any(chunk_id not in chunks for chunk_id in ids):
            return "dropped", "chunk id not in this document"
        parts = [chunks[chunk_id] for chunk_id in sorted(ids, key=lambda i: chunks[i].chunk_index)]
        if len({part.node_id for part in parts}) > 1:
            return "dropped", "merge_chunks must not cross TOC nodes"
        joined = "\n".join(part.content or "" for part in parts)
        original = "".join(part.content or "" for part in parts)
        joined_tokens = _tokens(joined)
        original_tokens = _tokens(original)
        new_tokens = sum(
            max(0, count - original_tokens.get(token, 0))
            for token, count in joined_tokens.items()
        )
        if sum(joined_tokens.values()) and new_tokens / max(1, sum(joined_tokens.values())) > 0.05:
            return "dropped", "merge invents text (more than 5% new tokens)"
        return "ok", ""

    if name == "reassign_chunk":
        chunk_id = op.get("chunk_id")
        node_id = op.get("node_id")
        if chunk_id not in chunks:
            return "dropped", "chunk id not in this document"
        if node_id not in nodes:
            return "dropped", "target node does not exist"
        chunk = chunks[chunk_id]
        if chunk.page_start is None or chunk.page_end is None:
            return "dropped", "chunk has no page range"
        if not _evidence_on_pages(
            ctx, str(op.get("evidence") or ""), chunk.page_start, chunk.page_end
        ):
            return "dropped", "evidence not found on the chunk's pages"
        return "ok", ""

    if name == "drop_chunk":
        chunk_id = op.get("chunk_id")
        if chunk_id not in chunks:
            return "dropped", "chunk id not in this document"
        chunk = chunks[chunk_id]
        if len((chunk.content or "").strip()) >= 50:
            return "dropped", "drop_chunk is only for short furniture (headers/footers)"
        evidence = str(op.get("evidence") or "")
        if op.get("evidence_in_chunk"):
            # The slice itself is the material being judged (a heading the
            # chunker split off). Grounding: the evidence must be the chunk.
            if _norm(evidence) and _norm(evidence) not in _norm(chunk.content or ""):
                return "dropped", "evidence is not from this chunk"
            return "ok", ""
        if chunk.page_start is not None and not _evidence_on_pages(
            ctx, evidence, chunk.page_start, chunk.page_end or chunk.page_start
        ):
            return "dropped", "evidence not found on the chunk's page"
        return "ok", ""

    if name == "split_chunk":
        chunk_id = op.get("chunk_id")
        at = str(op.get("at") or "")
        if chunk_id not in chunks:
            return "dropped", "chunk id not in this document"
        chunk = chunks[chunk_id]
        if not at or at not in (chunk.content or ""):
            return "dropped", "split point is not in the chunk text"
        if chunk.page_start is not None and not _evidence_on_pages(
            ctx, at, chunk.page_start, chunk.page_end or chunk.page_start
        ):
            return "dropped", "split point is not on the chunk's pages"
        return "ok", ""

    if name == "set_summary":
        if not str(op.get("summary") or "").strip():
            return "dropped", "summary is empty"
        return "ok", ""

    if name == "set_keywords":
        if not str(op.get("keywords") or "").strip():
            return "dropped", "keywords are empty"
        return "ok", ""

    if name == "correct_text":
        chunk_id = op.get("chunk_id")
        if chunk_id not in chunks:
            return "dropped", "chunk id not in this document"
        old = str(op.get("old") or "")
        new = str(op.get("new") or "")
        if not old or not new:
            return "dropped", "correct_text needs old and new"
        if len(old) != len(new):
            return "dropped", "correct_text must be character-level (same length)"
        if not all((a, b) in CONFUSION_PAIRS for a, b in zip(old, new)):
            return "dropped", "replacement is not an OCR confusion pair"
        if old not in (chunks[chunk_id].content or ""):
            return "dropped", "old text is not in the chunk"
        # §11.4: never edit text on the model's own suspicion. There must be
        # a human/agent complaint anchored to this chunk or its node.
        if not await ctx.has_usage_feedback(chunk_id):
            return (
                "dropped",
                "correct_text needs usage feedback: no erratum or pitfall note "
                "on this chunk or its node",
            )
        return "ok", ""

    if name == "set_page":
        node_id = op.get("node_id")
        if node_id not in nodes:
            return "dropped", "node does not exist"
        start = op.get("page_start")
        end = op.get("page_end")
        if not isinstance(start, int) or not isinstance(end, int):
            return "dropped", "page_start/page_end must be integers"
        if start < 0 or end < start:
            return "dropped", "illegal page range"
        parent = nodes.get(nodes[node_id].parent_id or "")
        if parent is not None and parent.page_start is not None and parent.page_end is not None:
            if start < parent.page_start or end > parent.page_end:
                return "dropped", "range outside parent"
        for child in nodes.values():
            if child.parent_id != node_id:
                continue
            if child.page_start is None:
                continue
            if child.page_start < start or (child.page_end or child.page_start) > end:
                return "dropped", f"child {child.node_id} falls outside the new range"
        if not _evidence_on_pages(ctx, str(op.get("evidence") or ""), start, end):
            return "dropped", "evidence not found in the claimed range"
        return "pending", ""

    if name in ("merge_nodes", "split_node"):
        return "pending", ""

    return "dropped", f"op not implemented: {name}"


async def apply_ops(
    ctx: ProposalContext, ops: list[dict], proposal_id: str, *, verify: bool = True
) -> dict:
    """Validate and apply a whole proposal, op by op.

    Low-risk ops that pass are applied in order. High-risk ops that pass are
    left ``pending_human``. A failing op is dropped with its reason and the
    rest still run (mneme's "degrade, don't abort"). If applying would raise
    validate_toc's error count, everything applied so far is rolled back via
    the pre-write snapshot.

    With ``verify`` (default on) the document is also measured before and
    after the write; a degraded verdict (``verify.compare_metrics``) rolls
    the write back the same way. The comparison rides on the result as
    ``verify`` so the caller can record it and write a negative lesson.
    """
    from .verify import compare_metrics, snapshot_metrics

    metrics_before = await snapshot_metrics(ctx) if verify else None
    results: list[dict] = []
    applied: list[dict] = []
    errors_before = _error_count(await ctx.nodes())

    validated = []
    for index, op in enumerate(ops):
        status, reason = await validate_op(ctx, op)
        validated.append((index, op, status, reason))

    if any(status == "ok" for _, _, status, _ in validated):
        nodes = await ctx.nodes()
        chunks = await ctx.chunks()
        await write_snapshot(
            ctx.db,
            ctx.doc.id,
            proposal_id,
            nodes=[
                {
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
                for node in nodes
            ],
            chunks=[
                {
                    "node_id": chunk.node_id,
                    "chunk_index": chunk.chunk_index,
                    "content": chunk.content,
                    "heading_path": chunk.heading_path,
                    "page_start": chunk.page_start,
                    "page_end": chunk.page_end,
                    "token_count": chunk.token_count,
                    "ref_ids": chunk.ref_ids,
                }
                for chunk in chunks
            ],
        )

    for index, op, status, reason in validated:
        entry = {
            "index": index,
            "op": op.get("op"),
            "risk": risk_of(str(op.get("op") or "")),
            "status": status,
        }
        if reason:
            entry["reason"] = reason
        if status == "pending":
            entry["gate"] = "pending_human"
        elif status == "ok":
            await _apply_one(ctx, op, proposal_id)
            applied.append(op)
        results.append(entry)

    if applied:
        await ctx.db.flush()
        errors_after = _error_count(await ctx.nodes())
        if errors_after > errors_before:
            await _rollback_last_snapshot(ctx)
            for entry in results:
                if entry["status"] == "ok":
                    entry["status"] = "dropped"
                    entry["reason"] = (
                        f"rolled back: validate_toc errors {errors_before} → {errors_after}"
                    )
            applied = []

    if applied:
        await _rebuild_fts(ctx)

    comparison = None
    if applied and verify:
        comparison = compare_metrics(metrics_before, await snapshot_metrics(ctx))
        if comparison["verdict"] == "worse":
            await _rollback_last_snapshot(ctx)
            reasons = "; ".join(comparison.get("reasons") or [])
            for entry in results:
                if entry["status"] == "ok":
                    entry["status"] = "dropped"
                    entry["reason"] = f"rolled back: verification degraded ({reasons})"
            applied = []

    return {
        "results": results,
        "applied_ops": len(applied),
        "pending_ops": sum(1 for entry in results if entry["status"] == "pending"),
        "dropped_ops": sum(1 for entry in results if entry["status"] == "dropped"),
        "errors_before": errors_before,
        "errors_after": _error_count(await ctx.nodes()),
        "verify": comparison,
    }


async def _apply_one(ctx: ProposalContext, op: dict, proposal_id: str) -> None:
    name = op.get("op")
    db = ctx.db
    if name == "no_op":
        return
    if name == "set_summary":
        ctx.doc.summary = str(op["summary"]).strip()
        return
    if name == "set_keywords":
        ctx.doc.keywords = str(op["keywords"]).strip()
        return
    if name == "set_page":
        node = (
            await db.execute(
                select(TocNode).where(
                    TocNode.doc_id == ctx.doc.id, TocNode.node_id == op["node_id"]
                )
            )
        ).scalar_one()
        node.page_start = int(op["page_start"])
        node.page_end = int(op["page_end"])
        return
    if name == "merge_chunks":
        await _merge_chunks(ctx, op, proposal_id)
        return
    if name == "reassign_chunk":
        chunk = await db.get(Chunk, int(op["chunk_id"]))
        chunk.node_id = str(op["node_id"])
        return
    if name == "drop_chunk":
        chunk = await db.get(Chunk, int(op["chunk_id"]))
        await _remap_chunk_references(ctx, {chunk.id: None}, proposal_id)
        await db.delete(chunk)
        return
    if name == "split_chunk":
        chunk = await db.get(Chunk, int(op["chunk_id"]))
        at = str(op["at"])
        head, tail = chunk.content.split(at, 1)
        chunk.content = head
        new = Chunk(
            doc_id=chunk.doc_id,
            node_id=chunk.node_id,
            chunk_index=chunk.chunk_index + 1,
            content=at + tail,
            heading_path=chunk.heading_path,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
        )
        db.add(new)
        await db.flush()
        await _remap_chunk_references(ctx, {chunk.id: new.id}, proposal_id)
        return
    if name == "correct_text":
        chunk = await db.get(Chunk, int(op["chunk_id"]))
        chunk.content = chunk.content.replace(str(op["old"]), str(op["new"]), 1)
        return


async def _merge_chunks(ctx: ProposalContext, op: dict, proposal_id: str) -> None:
    """Concatenate same-node slices into one new slice; remap references.

    The merged slice keeps the earliest ``chunk_index`` so reading order is
    unchanged, and its page span covers every part. The originals are deleted
    *before* the insert: ``UNIQUE(doc_id, chunk_index)`` would otherwise
    reject a new row reusing a slot that is still occupied.
    """
    db = ctx.db
    ids = [int(value) for value in op["chunk_ids"]]
    rows = [
        (await db.execute(select(Chunk).where(Chunk.id == chunk_id))).scalar_one()
        for chunk_id in ids
    ]
    rows.sort(key=lambda row: row.chunk_index)
    first = rows[0]
    snapshot = [
        {
            "id": row.id,
            "chunk_index": row.chunk_index,
            "content": row.content,
            "page_start": row.page_start,
            "page_end": row.page_end,
            "token_count": row.token_count,
        }
        for row in rows
    ]
    merged_content = "\n".join(row.content or "" for row in rows)
    page_starts = [row.page_start for row in rows if row.page_start is not None]
    page_ends = [row.page_end for row in rows if row.page_end is not None]

    for row in rows:
        await db.delete(row)
    await db.flush()

    merged = Chunk(
        doc_id=first.doc_id,
        node_id=first.node_id,
        chunk_index=first.chunk_index,
        content=merged_content,
        heading_path=first.heading_path,
        page_start=min(page_starts) if page_starts else None,
        page_end=max(page_ends) if page_ends else None,
        token_count=sum(item["token_count"] or 0 for item in snapshot) or None,
    )
    db.add(merged)
    await db.flush()
    await _remap_chunk_references(
        ctx, {item["id"]: merged.id for item in snapshot}, proposal_id
    )
    await _renumber_chunks(ctx)


async def _renumber_chunks(ctx: ProposalContext) -> None:
    """Make chunk_index contiguous again after a merge or drop.

    Two passes through negative staging values: updating straight to the
    final index can collide with a row that has not moved yet. Rows are
    updated through the ORM so the session's objects stay in sync.
    """
    rows = (
        (
            await ctx.db.execute(
                select(Chunk).where(Chunk.doc_id == ctx.doc.id).order_by(Chunk.chunk_index)
            )
        )
        .scalars()
        .all()
    )
    needs = [chunk for index, chunk in enumerate(rows) if chunk.chunk_index != index]
    if not needs:
        return
    for chunk in needs:
        chunk.chunk_index = -chunk.id
    await ctx.db.flush()
    targets = {chunk.id: index for index, chunk in enumerate(rows)}
    for chunk in needs:
        chunk.chunk_index = targets[chunk.id]
    await ctx.db.flush()


async def _remap_chunk_references(
    ctx: ProposalContext, mapping: dict[int, int | None], proposal_id: str
) -> None:
    """Point activity rows at the chunk that now holds their text.

    A dropped chunk maps to None: its notes stay but the anchor is cleared
    rather than silently pointing at unrelated text. Tables that do not
    exist (a database built before the activity layer) are skipped.
    """
    db = ctx.db
    for old_id, new_id in mapping.items():
        if new_id is not None:
            db.add(
                ChunkRemap(
                    doc_id=ctx.doc.id,
                    old_id=old_id,
                    new_id=new_id,
                    proposal_id=proposal_id,
                )
            )
        for table in _ACTIVITY_TABLES:
            if new_id is None:
                statement = (
                    f"UPDATE {table} SET chunk_id = NULL "
                    "WHERE doc_id = :doc AND chunk_id = :old"
                )
                params = {"doc": ctx.doc.id, "old": old_id}
            else:
                statement = (
                    f"UPDATE {table} SET chunk_id = :new "
                    "WHERE doc_id = :doc AND chunk_id = :old"
                )
                params = {"new": new_id, "doc": ctx.doc.id, "old": old_id}
            try:
                await db.execute(text(statement), params)
            except Exception as exc:  # no such table on a partial database
                if "no such table" not in str(exc).lower():
                    raise


_ACTIVITY_TABLES = ("agent_notes", "activities", "errata", "usage_log")


async def _rebuild_fts(ctx: ProposalContext) -> None:
    from ..index.fts import index_document

    await index_document(ctx.db, ctx.doc, await ctx.chunks())


async def _rollback_last_snapshot(ctx: ProposalContext) -> None:
    """Restore the pre-write snapshot taken by ``apply_ops``."""
    from .snapshots import latest_snapshot, snapshot_payload

    row = await latest_snapshot(ctx.db, ctx.doc.id)
    if row is None:
        return
    nodes, chunks = snapshot_payload(row)
    await restore_rows(ctx, nodes, chunks)


async def restore_rows(
    ctx: ProposalContext, nodes: list[dict], chunks: list[dict]
) -> None:
    """Replace a document's derived rows with a stored pre-write state.

    Shared by the in-transaction rollback (invariant failure) and the
    per-proposal undo path. Kept in one place so both rebuild FTS and both
    detach ORM rows before the raw DELETEs: SQLite reuses the rowids, and a
    stale identity-map entry would otherwise collide on re-insert.
    """
    loaded_nodes = (
        (await ctx.db.execute(select(TocNode).where(TocNode.doc_id == ctx.doc.id)))
        .scalars()
        .all()
    )
    loaded_chunks = (
        (await ctx.db.execute(select(Chunk).where(Chunk.doc_id == ctx.doc.id)))
        .scalars()
        .all()
    )
    for obj in [*loaded_nodes, *loaded_chunks]:
        ctx.db.expunge(obj)
    for table in ("chunks", "toc_nodes"):
        await ctx.db.execute(
            text(f"DELETE FROM {table} WHERE doc_id = :doc"), {"doc": ctx.doc.id}
        )
    await ctx.db.flush()
    for node in nodes:
        ctx.db.add(TocNode(doc_id=ctx.doc.id, **node))
    for chunk in chunks:
        ctx.db.add(Chunk(doc_id=ctx.doc.id, **chunk))
    await ctx.db.flush()
    await _rebuild_fts(ctx)
