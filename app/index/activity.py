from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Integer, String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from ..models import AgentNote, Base, Chunk, Document, TocNode, utcnow_iso

ACTIVITY_KINDS = (
    "read",      # read_section auto-logged
    "cite",      # agent used a section in its answer
    "note",      # free-form remark anchored to material
    "handoff",   # state handed to the next agent
    "erratum",   # "this Toc Memex content is wrong" — QA signal
    "review",    # verdict on errata / content quality
)

TASK_STATUSES = ("open", "doing", "blocked", "done", "archived")

# Erratum lifecycle: proposed by default (never affects retrieval);
# a review that endorses it promotes it to confirmed (retrieval returns
# the erratum alongside the content); a rejecting review sets rejected.
ERRATUM_STATUSES = ("proposed", "confirmed", "rejected")

# Erratum scopes, per document dialect:
#   error      — a mistake in the text
#   superseded — legal: replaced/repealed by another document or clause
#   retracted  — academic: withdrawn, must not be cited without notice
ERRATUM_SCOPES = ("error", "superseded", "retracted")


class Erratum(Base):
    """A flagged error in Toc Memex content, with its own lifecycle.

    Kept separate from the append-only Activity log: the log records that
    someone flagged/verified something; the erratum row is the live state
    retrieval consults. Original content is never edited — corrections
    ride alongside it.

    scope selects the dialect of "this content is not usable as-is":
    - "error": a mistake in the text (OCR damage, mistranslation, typo);
    - "superseded": a legal document/clause was replaced by another
      (article repealed, standard revised) — supersedes_* names the
      replacement;
    - "retracted": an academic work was withdrawn (publisher retraction)
      and must not be cited without the retraction notice.
    """

    __tablename__ = "errata"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    chunk_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(String(20), nullable=False, default="error", index=True)
    supersedes_doc_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    supersedes_node_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="proposed", index=True)
    proposed_by: Mapped[str] = mapped_column(String(80), nullable=False)
    confirmed_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    activity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(
        String(40), nullable=False, default=utcnow_iso, onupdate=utcnow_iso
    )


class UsageLog(Base):
    """Auto-logged retrieval usage: which query surfaced which chunk, and
    whether an agent actually used it (mark_used). Feeds future relevance
    weighting: chunks repeatedly marked used are probably the good ones.
    """

    __tablename__ = "usage_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    query: Mapped[str] = mapped_column(String(1000), nullable=False)
    tool: Mapped[str] = mapped_column(String(20), nullable=False, default="search")
    doc_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    node_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    chunk_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    used: Mapped[bool] = mapped_column(Integer, nullable=False, default=0, index=True)
    actor_agent: Mapped[str | None] = mapped_column(String(80), nullable=True)
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utcnow_iso)


class Task(Base):
    """A first-class task that spans sessions and agents.

    One task is one unit of work agents cooperate on: they attach reads,
    cites, notes and handoffs to it so the next agent can see exactly
    what material was used and what came of it.
    """

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    # task | gangmu-review — a work type, so a review order can be listed
    # apart from ordinary agent tasks (self-evolution §5.2).
    kind: Mapped[str] = mapped_column(
        String(30), nullable=False, default="task", server_default="task"
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    owner_agent: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(
        String(40), nullable=False, default=utcnow_iso, onupdate=utcnow_iso
    )


class Activity(Base):
    """Append-only activity log: read / cite / note / handoff / erratum / review.

    Never superseded and never edited — a correction is a new row (kind
    "review") that replies to the erratum's id. Filterable by task, doc,
    node, agent, kind and time.
    """

    __tablename__ = "activities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    doc_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    node_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    chunk_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor_agent: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    session_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    reply_to: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utcnow_iso, index=True)


async def task_create(
    db: AsyncSession,
    title: str,
    goal: str | None = None,
    owner_agent: str | None = None,
    task_id: str | None = None,
    kind: str = "task",
) -> Task:
    from ..models import new_uuid

    task = Task(
        id=task_id or new_uuid(),
        title=title,
        goal=goal,
        owner_agent=owner_agent,
        kind=kind,
    )
    db.add(task)
    await db.flush()
    return task


async def task_get(db: AsyncSession, task_id: str) -> Task | None:
    return await db.get(Task, task_id)


async def task_list(
    db: AsyncSession,
    status: str = "",
    owner_agent: str = "",
    limit: int = 20,
) -> list[Task]:
    stmt = select(Task).order_by(Task.updated_at.desc())
    if status:
        stmt = stmt.where(Task.status == status)
    if owner_agent:
        stmt = stmt.where(Task.owner_agent == owner_agent)
    stmt = stmt.limit(max(1, min(limit, 100)))
    return list((await db.execute(stmt)).scalars().all())


async def task_update(
    db: AsyncSession,
    task_id: str,
    status: str | None = None,
    owner_agent: str | None = None,
    goal: str | None = None,
) -> Task | None:
    task = await db.get(Task, task_id)
    if task is None:
        return None
    if status is not None:
        if status not in TASK_STATUSES:
            raise ValueError(f"invalid status: {status}")
        task.status = status
    if owner_agent is not None:
        task.owner_agent = owner_agent
    if goal is not None:
        task.goal = goal
    task.updated_at = utcnow_iso()
    await db.flush()
    return task


async def activity_write(
    db: AsyncSession,
    kind: str,
    actor_agent: str,
    content: str | None = None,
    task_id: str | None = None,
    doc_id: str | None = None,
    node_id: str | None = None,
    chunk_id: int | None = None,
    session_id: str | None = None,
    reply_to: int | None = None,
    scope: str = "error",
    supersedes_doc_id: str | None = None,
    supersedes_node_id: str | None = None,
) -> Activity:
    if kind not in ACTIVITY_KINDS:
        raise ValueError(f"invalid kind: {kind}")
    if not actor_agent:
        raise ValueError("actor_agent is required")
    if kind in ("note", "handoff", "erratum", "review") and not (content or "").strip():
        raise ValueError(f"kind={kind} requires content")
    if kind == "erratum" and doc_id is None and node_id is None:
        raise ValueError("erratum must anchor to a document or node")
    if kind == "review" and reply_to is None:
        raise ValueError("review must reply_to an erratum id")

    if task_id is not None and await db.get(Task, task_id) is None:
        raise ValueError(f"task not found: {task_id}")

    activity = Activity(
        kind=kind,
        task_id=task_id,
        doc_id=doc_id,
        node_id=node_id,
        chunk_id=chunk_id,
        actor_agent=actor_agent,
        session_id=session_id,
        content=content,
        reply_to=reply_to,
    )
    db.add(activity)
    await db.flush()

    if kind == "erratum" and doc_id:
        await erratum_upsert(
            db,
            doc_id,
            node_id,
            content or "",
            proposed_by=actor_agent,
            chunk_id=chunk_id,
            activity_id=activity.id,
            scope=scope,
            supersedes_doc_id=supersedes_doc_id,
            supersedes_node_id=supersedes_node_id,
        )
    return activity


async def activity_list(
    db: AsyncSession,
    task_id: str | None = None,
    doc_id: str | None = None,
    node_id: str | None = None,
    actor_agent: str | None = None,
    kind: str | None = None,
    reply_to: int | None = None,
    since: str | None = None,
    limit: int = 20,
) -> list[Activity]:
    stmt = select(Activity).order_by(Activity.id.desc())
    if task_id:
        stmt = stmt.where(Activity.task_id == task_id)
    if doc_id:
        stmt = stmt.where(Activity.doc_id == doc_id)
    if node_id:
        stmt = stmt.where(Activity.node_id == node_id)
    if actor_agent:
        stmt = stmt.where(Activity.actor_agent == actor_agent)
    if kind:
        stmt = stmt.where(Activity.kind == kind)
    if reply_to is not None:
        stmt = stmt.where(Activity.reply_to == reply_to)
    if since:
        stmt = stmt.where(Activity.created_at >= since)
    stmt = stmt.limit(max(1, min(limit, 200)))
    return list((await db.execute(stmt)).scalars().all())


def activity_out(activity: Activity) -> dict:
    return {
        "id": activity.id,
        "kind": activity.kind,
        "task_id": activity.task_id,
        "doc_id": activity.doc_id,
        "node_id": activity.node_id,
        "chunk_id": activity.chunk_id,
        "actor_agent": activity.actor_agent,
        "session_id": activity.session_id,
        "content": activity.content,
        "reply_to": activity.reply_to,
        "created_at": activity.created_at,
    }


def task_out(task: Task) -> dict:
    return {
        "id": task.id,
        "title": task.title,
        "goal": task.goal,
        "kind": task.kind,
        "status": task.status,
        "owner_agent": task.owner_agent,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


# ── hand-off notes (agent_notes) ───────────────────────────────────────

# 入库备注: written by the human at upload time, one per document. Agents
# can read it (list_notes / get_document) but must never create, edit or
# delete it — the MCP tools enforce that. It is a human's description of
# what the file is and why it is in the library, which agents need before
# trusting its contents.
INGEST_NOTE_KIND = "ingest"
INGEST_NOTE_AUTHOR = "human"


def note_out(note: AgentNote) -> dict:
    return {
        "id": note.id,
        "author": note.author,
        "kind": note.kind,
        "body": note.body,
        "doc_id": note.doc_id,
        "node_id": note.node_id,
        "chunk_id": note.chunk_id,
        "task_id": note.task_id,
        "issue_id": note.issue_id,
        "session_id": note.session_id,
        "reply_to": note.reply_to,
        "created_at": note.created_at,
    }


async def notes_list(
    db: AsyncSession,
    doc_id: str | None = None,
    task_id: str | None = None,
    author: str | None = None,
    kind: str | None = None,
    limit: int = 50,
) -> list[AgentNote]:
    """Hand-off notes, newest first, filterable by the work unit they belong to.

    ``task_id`` is the filter agents actually need ("what did the previous
    agent hand over on this task"). Without it the only way to find a
    hand-off was to scan every note in the library.
    """
    stmt = select(AgentNote).order_by(AgentNote.id.desc())
    if doc_id:
        stmt = stmt.where(AgentNote.doc_id == doc_id)
    if task_id:
        stmt = stmt.where(AgentNote.task_id == task_id)
    if author:
        stmt = stmt.where(AgentNote.author == author)
    if kind:
        stmt = stmt.where(AgentNote.kind == kind)
    stmt = stmt.limit(max(1, min(limit, 200)))
    return list((await db.execute(stmt)).scalars().all())


async def ingest_note(db: AsyncSession, doc_id: str) -> AgentNote | None:
    """The document's single 入库备注, if one was written."""
    return (
        (
            await db.execute(
                select(AgentNote)
                .where(
                    AgentNote.doc_id == doc_id,
                    AgentNote.kind == INGEST_NOTE_KIND,
                )
                .order_by(AgentNote.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def upsert_ingest_note(
    db: AsyncSession,
    doc_id: str,
    body: str,
    author: str = INGEST_NOTE_AUTHOR,
) -> AgentNote | None:
    """Set (or clear, with an empty body) a document's 入库备注.

    One per document: a second write edits the existing row instead of
    appending another note, so the library list has a single value to show
    and edit. Only the web layer calls this — no agent-facing tool does.
    """
    text = (body or "").strip()
    row = await ingest_note(db, doc_id)
    if not text:
        if row is not None:
            await db.delete(row)
        return None
    if row is None:
        row = AgentNote(
            author=author,
            kind=INGEST_NOTE_KIND,
            body=text,
            doc_id=doc_id,
        )
        db.add(row)
    else:
        row.body = text
        row.author = author
    await db.flush()
    return row


async def task_timeline(
    db: AsyncSession, task_id: str, limit: int = 200
) -> dict | None:
    """One task's whole timeline: append-only activities plus hand-off notes.

    Notes written through ``add_note`` live in ``agent_notes`` while reads,
    cites, errata and ``activity_write`` hand-offs live in ``activities``.
    The Web task page already merged them; the MCP surface did not, so an
    agent following the server instructions ("activity_list(task_id=...) to
    see what previous agents handed over") could never see a hand-off note.
    This is the single merge point both surfaces now use.
    """
    task = await db.get(Task, task_id)
    if task is None:
        return None

    activities = list(
        (
            await db.execute(
                select(Activity).where(Activity.task_id == task_id).order_by(Activity.id)
            )
        )
        .scalars()
        .all()
    )
    notes = list(
        (
            await db.execute(
                select(AgentNote).where(AgentNote.task_id == task_id).order_by(AgentNote.id)
            )
        )
        .scalars()
        .all()
    )

    doc_ids = {
        row.doc_id for row in [*activities, *notes] if row.doc_id is not None
    }
    doc_titles: dict[str, str] = {}
    node_titles: dict[tuple[str, str], tuple[str | None, str | None]] = {}
    if doc_ids:
        docs = (
            (await db.execute(select(Document).where(Document.id.in_(doc_ids))))
            .scalars()
            .all()
        )
        doc_titles = {doc.id: doc.title for doc in docs}
        nodes = (
            (
                await db.execute(
                    select(TocNode).where(TocNode.doc_id.in_(doc_ids))
                )
            )
            .scalars()
            .all()
        )
        node_titles = {
            (node.doc_id, node.node_id): (node.title, node.heading_path)
            for node in nodes
        }

    def _node(doc_id, node_id):
        return node_titles.get((doc_id, node_id), (None, None))

    timeline = [
        {
            "source": "activity",
            "id": row.id,
            "kind": row.kind,
            "actor": row.actor_agent,
            "content": row.content,
            "docId": row.doc_id,
            "docTitle": doc_titles.get(row.doc_id or ""),
            "nodeId": row.node_id,
            "nodeTitle": _node(row.doc_id, row.node_id)[0],
            "headingPath": _node(row.doc_id, row.node_id)[1],
            "chunkId": row.chunk_id,
            "replyTo": row.reply_to,
            "createdAt": row.created_at,
        }
        for row in activities
    ] + [
        {
            "source": "note",
            "id": row.id,
            "kind": row.kind,
            "actor": row.author,
            "content": row.body,
            "docId": row.doc_id,
            "docTitle": doc_titles.get(row.doc_id or ""),
            "nodeId": row.node_id,
            "nodeTitle": _node(row.doc_id, row.node_id)[0],
            "headingPath": _node(row.doc_id, row.node_id)[1],
            "chunkId": row.chunk_id,
            "replyTo": row.reply_to,
            "createdAt": row.created_at,
        }
        for row in notes
    ]
    timeline.sort(key=lambda item: (item["createdAt"] or "", item["source"], item["id"]))
    if limit:
        timeline = timeline[-max(1, min(limit, 1000)) :]

    return {
        "task": {
            "id": task.id,
            "title": task.title,
            "goal": task.goal,
            "status": task.status,
            "owner": task.owner_agent,
            "createdAt": task.created_at,
            "updatedAt": task.updated_at,
        },
        "timeline": timeline,
        "counts": {
            "activities": len(activities),
            "notes": len(notes),
            "total": len(activities) + len(notes),
        },
    }


async def erratum_upsert(
    db: AsyncSession,
    doc_id: str,
    node_id: str | None,
    body: str,
    proposed_by: str,
    chunk_id: int | None = None,
    activity_id: int | None = None,
    scope: str = "error",
    supersedes_doc_id: str | None = None,
    supersedes_node_id: str | None = None,
) -> Erratum:
    """Create an erratum row when an agent writes kind='erratum'.

    scope="superseded" / "retracted" require a body explaining the
    replacement / retraction source; supersedes_* may name the replacing
    document or clause for superseded items.
    """
    if scope not in ERRATUM_SCOPES:
        raise ValueError(f"invalid scope: {scope}")
    if scope == "superseded" and not supersedes_doc_id and not supersedes_node_id:
        raise ValueError(
            "superseded erratum must name the replacement via "
            "supersedes_doc_id (or supersedes_node_id); explain it in the body"
        )
    erratum = Erratum(
        doc_id=doc_id,
        node_id=node_id,
        chunk_id=chunk_id,
        body=body,
        scope=scope,
        supersedes_doc_id=supersedes_doc_id,
        supersedes_node_id=supersedes_node_id,
        proposed_by=proposed_by,
        activity_id=activity_id,
    )
    db.add(erratum)
    await db.flush()
    return erratum


async def confirm_erratum(
    db: AsyncSession,
    activity_id: int,
    confirmed_by: str,
    accepted: bool,
    *,
    reviewer_session: str | None = None,
) -> Erratum | None:
    """Promote/demote the erratum referenced by a 'review' activity.

    A second agent independently verified: accepted=True -> confirmed
    (retrieval will surface the correction), False -> rejected.

    Two-party gate enforced in code: the reviewer must differ from the
    proposer, otherwise one compromised/buggy agent can confirm its own
    fabricated correction and inject it into everyone's retrieval. The gate
    keys on the server-assigned ``session_id`` when both sides carry one,
    because the self-reported ``actor_agent`` name is trivially spoofable
    (an agent can declare a different name and approve its own erratum). It
    falls back to comparing names only for legacy rows with no session.
    """
    from sqlalchemy import select as _select

    review = await db.get(Activity, activity_id)
    if review is None or review.kind != "review" or review.reply_to is None:
        return None
    original = await db.get(Activity, review.reply_to)
    if original is None or original.kind != "erratum":
        return None

    # Prefer the unforgeable session identity over the self-reported name.
    proposer_session = original.session_id
    effective_reviewer_session = reviewer_session or review.session_id
    if proposer_session and effective_reviewer_session:
        if proposer_session == effective_reviewer_session:
            raise ValueError(
                "self-confirmation forbidden: the reviewer's session matches "
                "the erratum proposer's session"
            )
    elif confirmed_by == original.actor_agent:
        raise ValueError(
            "self-confirmation forbidden: the reviewer must differ from "
            f"the erratum proposer ({original.actor_agent})"
        )
    if confirmed_by and review.actor_agent and confirmed_by != review.actor_agent:
        raise ValueError(
            "confirmed_by must match the review activity's author: "
            f"review was written by {review.actor_agent}"
        )

    stmt = _select(Erratum).where(Erratum.activity_id == original.id)
    erratum = (await db.execute(stmt)).scalars().first()
    if erratum is None:
        # erratum written before the errata table existed; create from log
        erratum = Erratum(
            doc_id=original.doc_id or "",
            node_id=original.node_id,
            chunk_id=original.chunk_id,
            body=original.content or "",
            proposed_by=original.actor_agent,
            activity_id=original.id,
        )
        db.add(erratum)
        await db.flush()

    if erratum.status in ("confirmed", "rejected"):
        # Frozen once decided: a later review must not flip the verdict.
        # Re-litigating requires a NEW erratum against the same content.
        raise ValueError(
            f"erratum already {erratum.status} "
            f"(by {erratum.confirmed_by}); write a new erratum to re-open"
        )

    erratum.status = "confirmed" if accepted else "rejected"
    erratum.confirmed_by = confirmed_by
    erratum.updated_at = utcnow_iso()
    await db.flush()
    return erratum


async def errata_for_chunks(
    db: AsyncSession,
    doc_id: str,
    chunk_ids: list[int],
    node_ids: list[str] | None = None,
) -> list[Erratum]:
    """Confirmed errata attached to the given chunks/nodes (retrieval QA).

    OR semantics: an erratum anchored by node_id only still matches a hit
    whose chunk_ids intersect — most errata carry no chunk_id. An erratum
    anchored to an ancestor node (a whole chapter) matches every hit in
    its subtree, because the correction covers the chapter's content.
    """
    stmt = select(Erratum).where(
        Erratum.doc_id == doc_id,
        Erratum.status == "confirmed",
    )
    errata = list((await db.execute(stmt)).scalars().all())

    if not errata or (not chunk_ids and not node_ids):
        return []

    hit_node_ids: set[str] = set(node_ids or [])
    if chunk_ids:
        rows = await db.execute(
            select(Chunk.node_id).where(
                Chunk.doc_id == doc_id, Chunk.id.in_(chunk_ids)
            )
        )
        hit_node_ids |= {n for n in rows.scalars() if n}

    # node_ids are flat sequence numbers ("doc:0157"), hierarchy lives in
    # toc_nodes.parent_id — compute each hit's ancestor chain so an
    # erratum anchored to a chapter root covers all of its subsections.
    ancestors: set[str] = set()
    if hit_node_ids:
        rows = await db.execute(
            select(TocNode.node_id, TocNode.parent_id).where(
                TocNode.doc_id == doc_id
            )
        )
        parent_of = {n: p for n, p in rows.all()}
        for node_id in hit_node_ids:
            current, seen = node_id, set()
            while current and current not in seen:
                seen.add(current)
                parent = parent_of.get(current)
                if parent:
                    ancestors.add(parent)
                current = parent

    matched: list[Erratum] = []
    for erratum in errata:
        if erratum.chunk_id and erratum.chunk_id in chunk_ids:
            matched.append(erratum)
            continue
        anchor = erratum.node_id
        if not anchor:
            continue
        if anchor in hit_node_ids or anchor in ancestors:
            matched.append(erratum)
    return matched


async def usage_log_write(
    db: AsyncSession,
    query: str,
    tool: str,
    hits: list[dict],
    actor_agent: str | None = None,
    task_id: str | None = None,
) -> int:
    """Persist one usage row per retrieved hit (rank preserved).

    A query with no hits writes a single ``rank=0`` row with no document, so a
    miss is recorded instead of vanishing. That row is the raw material for the
    search-quality loop (``docs/self-evolution.md`` §5.4.2): it says what people
    asked for and the library could not answer. Hit rows keep ``rank >= 1``,
    so ``rank = 0`` identifies a miss without a schema change; retrieval heat
    ignores NULL-document rows.
    """
    from ..models import new_uuid

    if task_id is not None and await db.get(Task, task_id) is None:
        task_id = None  # never let logging break retrieval
    if not hits:
        db.add(
            UsageLog(
                query=query[:1000],
                tool=tool,
                doc_id=None,
                node_id=None,
                chunk_id=None,
                rank=0,
                used=0,
                actor_agent=actor_agent,
                task_id=task_id,
            )
        )
        await db.flush()
        return 0
    for rank, hit in enumerate(hits, start=1):
        db.add(
            UsageLog(
                query=query[:1000],
                tool=tool,
                doc_id=hit.get("doc_id"),
                node_id=hit.get("node_id"),
                chunk_id=hit.get("chunk_id"),
                rank=rank,
                used=0,
                actor_agent=actor_agent,
                task_id=task_id,
            )
        )
    await db.flush()
    return len(hits)


async def errata_by_hit(
    db: AsyncSession,
    hits: list[dict],
) -> list[list[dict]]:
    """Confirmed errata for each hit, with ancestor matching preserved.

    ``confirmed_errata_hint`` returns the union over all hits. Callers that
    attach errata to individual hits used to filter that union by exact
    ``node_id`` equality, which silently dropped corrections anchored to an
    ancestor (a confirmed erratum on 第12章 never appeared when searching
    12.3.2). This keeps the per-hit association: each hit is matched with
    the same chunk/ancestor-chain rule ``errata_for_chunks`` already uses.
    """
    out: list[list[dict]] = []
    for hit in hits:
        doc_id = hit.get("doc_id")
        if not doc_id:
            out.append([])
            continue
        node_ids = [hit["node_id"]] if hit.get("node_id") else None
        errata = await errata_for_chunks(
            db, doc_id, list(hit.get("chunk_ids") or []), node_ids
        )
        seen: set[int] = set()
        items: list[dict] = []
        for erratum in errata:
            if erratum.id in seen:
                continue
            seen.add(erratum.id)
            items.append(erratum_out(erratum))
        out.append(items)
    return out


async def confirmed_errata_hint(
    db: AsyncSession,
    hits: list[dict],
) -> list[dict]:
    """Return confirmed errata matching the retrieved hits, as dicts ready
    to embed in a search/ask/read response."""
    by_doc: dict[str, dict[str, list[int]]] = {}
    for hit in hits:
        doc_id = hit.get("doc_id")
        if not doc_id:
            continue
        entry = by_doc.setdefault(doc_id, {"chunk_ids": [], "node_ids": []})
        if hit.get("chunk_ids"):
            entry["chunk_ids"].extend(hit["chunk_ids"])
        if hit.get("node_id"):
            entry["node_ids"].append(hit["node_id"])

    out: list[dict] = []
    for doc_id, ids in by_doc.items():
        for erratum in await errata_for_chunks(
            db,
            doc_id,
            ids["chunk_ids"],
            ids["node_ids"] or None,
        ):
            out.append(erratum_out(erratum))
    return out


def erratum_out(erratum: Erratum) -> dict:
    return {
        "id": erratum.id,
        "doc_id": erratum.doc_id,
        "node_id": erratum.node_id,
        "chunk_id": erratum.chunk_id,
        "body": erratum.body,
        "scope": erratum.scope,
        "supersedes_doc_id": erratum.supersedes_doc_id,
        "supersedes_node_id": erratum.supersedes_node_id,
        "status": erratum.status,
        "proposed_by": erratum.proposed_by,
        "confirmed_by": erratum.confirmed_by,
        "activity_id": erratum.activity_id,
        "created_at": erratum.created_at,
        "updated_at": erratum.updated_at,
    }


class Lesson(Base):
    """Structured lessons learned: when it applies, what went wrong, what
    to do. Retrieved by trigger keywords when a new task starts — so the
    same pitfall is not stepped in twice by a different agent.

    ``support_count`` is the consolidation signal (borrowed from
    altk-evolve): re-observing the same lesson bumps the count instead of
    inserting a duplicate, so a lesson seen fifty times ranks above one seen
    once, and the table does not grow a copy per run.
    """

    __tablename__ = "lessons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trigger: Mapped[str] = mapped_column(String(500), nullable=False)
    issue: Mapped[str] = mapped_column(Text, nullable=False)
    solution: Mapped[str] = mapped_column(Text, nullable=False)
    keywords: Mapped[str | None] = mapped_column(String(500), nullable=True, index=True)
    author_agent: Mapped[str] = mapped_column(String(80), nullable=False)
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    doc_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    support_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # JSON array of session ids that contributed to this lesson. Used so one
    # connection cannot inflate support_count by resubmitting the same lesson
    # (S4). Nullable: rows written before this column have no history.
    contributor_sessions: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(
        String(40), nullable=False, default=utcnow_iso, onupdate=utcnow_iso
    )


class Score(Base):
    """Outcome evaluation for a task, kept separate from the activity log.
    dimension e.g. hit_rate / correctness / rework; value numeric or
    textual; by records who scored. Feed-back target for usage weighting.
    """

    __tablename__ = "scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dimension: Mapped[str] = mapped_column(String(40), nullable=False)
    value: Mapped[str] = mapped_column(String(200), nullable=False)
    by: Mapped[str] = mapped_column(String(80), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utcnow_iso)


def _lesson_key(trigger: str, issue: str) -> str:
    """Normalized identity for consolidation.

    Two lessons are "the same" when their trigger and issue say the same
    thing after whitespace/punctuation folding. The solution may improve on
    re-observation, so it is not part of the key.
    """
    import re as _re

    def fold(value: str) -> str:
        return _re.sub(r"[\s,，。.;；:：!！?？、\-—_/\\|()（）\[\]【】]+", "", (value or "")).lower()

    return f"{fold(trigger)}|{fold(issue)}"


async def lesson_write(
    db: AsyncSession,
    trigger: str,
    issue: str,
    solution: str,
    author_agent: str,
    keywords: str | None = None,
    task_id: str | None = None,
    doc_id: str | None = None,
    author_session: str | None = None,
) -> Lesson:
    """Record a lesson, consolidating with an existing identical one.

    ADR-0002 §11.3: the same pitfall observed again bumps ``support_count``
    and refreshes the solution instead of inserting a duplicate row. The
    table then reflects "how often this bit us", which is what makes a
    lesson rank above a one-off remark.

    ``author_session`` is the server-assigned connection id. One connection
    may contribute to a given lesson only once: a repeat submission from the
    same session neither bumps ``support_count`` nor overwrites the solution
    text, so an agent cannot inflate a lesson's rank (or clobber another
    agent's fix) by resubmitting it (S4).
    """
    import json as _json

    if not trigger.strip() or not issue.strip() or not solution.strip():
        raise ValueError("trigger, issue and solution are required")
    if not author_agent:
        raise ValueError("author_agent is required")
    if task_id is not None and await db.get(Task, task_id) is None:
        raise ValueError(f"task not found: {task_id}")

    key = _lesson_key(trigger, issue)
    existing = None
    for candidate in (await db.execute(select(Lesson))).scalars().all():
        if _lesson_key(candidate.trigger, candidate.issue) == key:
            existing = candidate
            break
    if existing is not None:
        try:
            contributors = (
                _json.loads(existing.contributor_sessions)
                if existing.contributor_sessions
                else []
            )
        except (ValueError, TypeError):
            contributors = []
        if not isinstance(contributors, list):
            contributors = []
        if author_session and author_session in contributors:
            # Same connection again: no rank inflation, no solution overwrite.
            return existing
        existing.support_count = (existing.support_count or 1) + 1
        existing.solution = solution.strip()
        if keywords:
            existing.keywords = keywords
        if doc_id:
            existing.doc_id = doc_id
        if author_session:
            contributors.append(author_session)
            existing.contributor_sessions = _json.dumps(contributors)
        existing.updated_at = utcnow_iso()
        await db.flush()
        return existing

    lesson = Lesson(
        trigger=trigger.strip(),
        issue=issue.strip(),
        solution=solution.strip(),
        keywords=keywords,
        author_agent=author_agent,
        task_id=task_id,
        doc_id=doc_id,
        support_count=1,
        contributor_sessions=(
            _json.dumps([author_session]) if author_session else None
        ),
    )
    db.add(lesson)
    await db.flush()
    return lesson


def _lesson_terms(value: str) -> list[str]:
    """Tokenize a lesson query for matching, Chinese included.

    Splitting only on whitespace/punctuation made a natural Chinese
    sentence one long token, so a long phrase matched nothing while one of
    its component words matched. jieba (already the index tokenizer) plus a
    character-bigram fallback gives the sentence its component terms.
    """
    import re as _re

    import jieba

    raw = (value or "").strip()
    if not raw:
        return []
    terms: list[str] = []

    def add(term: str) -> None:
        term = term.strip().lower()
        if len(term) < 2:
            return
        if term not in terms:
            terms.append(term)

    # Whitespace/punctuation groups first: keeps identifiers and English
    # phrases ("rule_merge", "short-chunks") intact.
    for group in _re.split(r"[\s,，;；。.!！?？、/|:：()（）\[\]【】]+", raw):
        add(group)
    for term in jieba.lcut_for_search(raw):
        add(term)

    # jieba segments unknown compounds into single characters; recover the
    # overlapping bigrams so a term that only exists inside the sentence is
    # still searchable.
    compact = _re.sub(r"[\s,，;；。.!！?？、/|:：()（）\[\]【】]+", "", raw)
    if len(compact) >= 2 and not any(char.isascii() for char in compact):
        for index in range(len(compact) - 1):
            add(compact[index : index + 2])
    return terms


async def lesson_search(
    db: AsyncSession,
    query: str,
    top_k: int = 5,
) -> list[Lesson]:
    """Match lessons whose trigger/keywords/issue share words with the query.

    Small table, so scoring is in Python; relevance is the total length of
    the distinct matched terms (a matched 4-character term says more than a
    matched 2-character term), then ``support_count`` so a pitfall seen
    fifty times outranks a one-off remark on the same topic.
    """
    words = _lesson_terms(query)
    if not words:
        return []
    lessons = (await db.execute(select(Lesson))).scalars().all()

    def _score(lesson: Lesson) -> tuple:
        haystack = " ".join(
            filter(None, [lesson.trigger, lesson.keywords or "", lesson.issue])
        ).lower()
        matched = [word for word in words if word in haystack]
        relevance = sum(len(word) for word in matched)
        return (relevance, lesson.support_count or 1)

    scored = [(_score(lesson), lesson) for lesson in lessons]
    scored = [(score, lesson) for score, lesson in scored if score[0] > 0]
    scored.sort(key=lambda pair: (-pair[0][0], -pair[0][1], pair[1].id))
    return [lesson for _, lesson in scored[: max(1, min(top_k, 20))]]


async def score_write(
    db: AsyncSession,
    task_id: str,
    dimension: str,
    value: str,
    by: str,
    comment: str | None = None,
) -> Score:
    if await db.get(Task, task_id) is None:
        raise ValueError(f"task not found: {task_id}")
    if not dimension.strip() or not value.strip() or not by:
        raise ValueError("dimension, value and by are required")
    score = Score(
        task_id=task_id,
        dimension=dimension.strip()[:40],
        value=value.strip()[:200],
        by=by,
        comment=comment,
    )
    db.add(score)
    await db.flush()
    return score


async def score_list(
    db: AsyncSession,
    task_id: str | None = None,
    dimension: str | None = None,
    limit: int = 50,
) -> list[Score]:
    stmt = select(Score).order_by(Score.id.desc())
    if task_id:
        stmt = stmt.where(Score.task_id == task_id)
    if dimension:
        stmt = stmt.where(Score.dimension == dimension)
    stmt = stmt.limit(max(1, min(limit, 200)))
    return list((await db.execute(stmt)).scalars().all())


def lesson_out(lesson: Lesson) -> dict:
    return {
        "id": lesson.id,
        "trigger": lesson.trigger,
        "issue": lesson.issue,
        "solution": lesson.solution,
        "keywords": lesson.keywords,
        "author_agent": lesson.author_agent,
        "task_id": lesson.task_id,
        "doc_id": lesson.doc_id,
        "support_count": lesson.support_count,
        "created_at": lesson.created_at,
        "updated_at": lesson.updated_at,
    }


def score_out(score: Score) -> dict:
    return {
        "id": score.id,
        "task_id": score.task_id,
        "dimension": score.dimension,
        "value": score.value,
        "by": score.by,
        "comment": score.comment,
        "created_at": score.created_at,
    }
