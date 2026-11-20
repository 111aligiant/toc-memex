"""Topic collections: the "which topic" axis of the library.

A collection is a named topic area with a description written for agents
("what it holds, when a request belongs here"). It is deliberately not a
namespace: namespaces answer *whose* library, collections answer *which
topic*. Documents belong to at most one collection; NULL means unfiled.

The agent-facing contract lives here too: ``resolve_scope`` turns the
``collection`` argument an agent passes (id, name, or the reserved word
``unassigned``) into a ``Scope``, and refuses unknown values loudly rather
than silently searching the whole library.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..identifiers import family_key
from ..models import Collection, Document

UNASSIGNED = "unassigned"
MAX_NAME = 80


def name_key(value: str) -> str:
    """The uniqueness key for a collection name.

    Python's casefold (not SQLite's ASCII-only lower/NOCASE) so "Ärger" and
    "ärger" collide. Stored in ``collections.name_key`` with a UNIQUE index,
    which is what actually enforces uniqueness under concurrency.
    """
    return (value or "").strip().casefold()


class CollectionNotFound(ValueError):
    """An agent named a collection that does not exist.

    The message lists every valid collection with its document count, so a
    caller that guessed a near-miss name can retry without another round
    trip to ``list_collections()``.
    """

    def __init__(self, value: str, valid: list[tuple[str, int]]) -> None:
        self.value = value
        self.valid = valid
        listing = ", ".join(f"{name} ({count})" for name, count in valid)
        super().__init__(
            f"collection not found: {value}\n"
            f"valid collections: {listing or '(none)'}\n"
            f'Use collection="{UNASSIGNED}" for documents not yet filed.'
        )


@dataclass(frozen=True)
class Scope:
    """A resolved collection filter.

    ``collection is None and unassigned is False`` means "no filter".
    """

    collection: Collection | None
    unassigned: bool

    @property
    def active(self) -> bool:
        return self.collection is not None or self.unassigned


def _normalize_name(value: str) -> str:
    name = (value or "").strip()
    if not name:
        raise ValueError("collection name is required")
    if len(name) > MAX_NAME:
        raise ValueError(f"collection name is too long (max {MAX_NAME})")
    if name.casefold() == UNASSIGNED:
        raise ValueError(f'"{UNASSIGNED}" is reserved; pick another name')
    return name


async def _name_taken(db: AsyncSession, name: str, exclude_id: str | None = None) -> bool:
    stmt = select(Collection.id).where(Collection.name_key == name_key(name))
    if exclude_id:
        stmt = stmt.where(Collection.id != exclude_id)
    return (await db.execute(stmt)).first() is not None


async def list_collections(db: AsyncSession) -> list[dict]:
    """Every collection with its document count, in display order.

    Counts include all document statuses, matching the library header's
    total rather than only "ready" documents.
    """
    counts = (
        select(Document.collection_id, func.count().label("doc_count"))
        .where(Document.collection_id.is_not(None))
        .group_by(Document.collection_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(Collection, func.coalesce(counts.c.doc_count, 0))
            .outerjoin(counts, counts.c.collection_id == Collection.id)
            .order_by(Collection.sort_order, Collection.name)
        )
    ).all()
    return [
        {
            "id": collection.id,
            "name": collection.name,
            "description": collection.description or "",
            "doc_count": int(count or 0),
        }
        for collection, count in rows
    ]


async def unassigned_doc_count(db: AsyncSession) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(Document)
            .where(Document.collection_id.is_(None))
        )
    ).scalar() or 0


async def resolve_scope(db: AsyncSession, value: str) -> Scope:
    """Resolve an agent-supplied collection argument into a Scope.

    Accepts the id, the exact name (case-insensitive), or ``unassigned``.
    Empty means "no filter". Anything else raises CollectionNotFound.
    """
    raw = (value or "").strip()
    if not raw:
        return Scope(None, False)
    if raw.casefold() == UNASSIGNED:
        return Scope(None, True)

    collection = await db.get(Collection, raw)
    if collection is None:
        collection = (
            await db.execute(
                select(Collection).where(Collection.name_key == name_key(raw))
            )
        ).scalar_one_or_none()
    if collection is None:
        valid = [(row["name"], row["doc_count"]) for row in await list_collections(db)]
        raise CollectionNotFound(value, valid)
    return Scope(collection, False)


async def doc_ids_in_scope(db: AsyncSession, scope: Scope) -> list[str] | None:
    """Document ids allowed by a scope; ``None`` means "no filter".

    An empty collection returns ``[]`` — a real empty list, never folded
    into ``None``, because downstream search treats ``None`` as unscoped
    and would silently search the whole library.
    """
    if not scope.active:
        return None
    stmt = select(Document.id).order_by(Document.id)
    if scope.unassigned:
        stmt = stmt.where(Document.collection_id.is_(None))
    else:
        stmt = stmt.where(Document.collection_id == scope.collection.id)
    return list((await db.execute(stmt)).scalars().all())


async def collections_by_doc(
    db: AsyncSession, doc_ids: list[str]
) -> dict[str, dict | None]:
    """Map each doc id to ``{"id", "name"}`` or None when unfiled.

    One query for the whole batch; unknown ids map to None so callers never
    have to special-case a missing key.
    """
    if not doc_ids:
        return {}
    rows = (
        await db.execute(
            select(Document.id, Collection.id, Collection.name)
            .outerjoin(Collection, Collection.id == Document.collection_id)
            .where(Document.id.in_(set(doc_ids)))
        )
    ).all()
    mapping: dict[str, dict | None] = {doc_id: None for doc_id in doc_ids}
    for doc_id, collection_id, name in rows:
        mapping[doc_id] = {"id": collection_id, "name": name} if collection_id else None
    return mapping


async def create_collection(
    db: AsyncSession, name: str, description: str | None = None
) -> Collection:
    clean = _normalize_name(name)
    if await _name_taken(db, clean):
        raise ValueError(f"collection name already exists: {clean}")
    collection = Collection(
        name=clean,
        name_key=name_key(clean),
        description=(description or "").strip() or None,
    )
    db.add(collection)
    try:
        await db.flush()
    except IntegrityError as exc:
        # Two requests can pass the pre-check together; the UNIQUE index
        # then rejects the loser. Surface it as the same readable error the
        # caller already handles instead of an IntegrityError.
        await db.rollback()
        raise ValueError(f"collection name already exists: {clean}") from exc
    return collection


async def update_collection(
    db: AsyncSession,
    collection_id: str,
    name: str | None = None,
    description: str | None = None,
) -> Collection:
    collection = await db.get(Collection, collection_id)
    if collection is None:
        raise ValueError(f"collection not found: {collection_id}")
    if name is not None:
        clean = _normalize_name(name)
        if await _name_taken(db, clean, exclude_id=collection_id):
            raise ValueError(f"collection name already exists: {clean}")
        collection.name = clean
        collection.name_key = name_key(clean)
    if description is not None:
        collection.description = description.strip() or None
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise ValueError(f"collection name already exists: {name}") from exc
    return collection


async def delete_collection(db: AsyncSession, collection_id: str) -> int:
    """Delete a collection; its documents become unfiled (never deleted).

    Returns how many documents were unfiled.
    """
    collection = await db.get(Collection, collection_id)
    if collection is None:
        raise ValueError(f"collection not found: {collection_id}")
    result = await db.execute(
        update(Document)
        .where(Document.collection_id == collection_id)
        .values(collection_id=None, updated_at=Document.updated_at)
    )
    unfiled = result.rowcount or 0
    await db.delete(collection)
    await db.flush()
    return unfiled


async def assign_documents(
    db: AsyncSession, doc_ids: list[str], collection_id: str | None
) -> tuple[int, list[str]]:
    """File documents into a collection (or unfiled when None).

    ``documents.updated_at`` is pinned to its current value: filing a
    document is a library-organisation act, not a content change, and
    moving it to the top of "recently updated" would misrepresent that.

    Returns ``(updated, missing)`` where ``updated`` counts only documents
    whose collection actually changed — re-filing a document that is
    already there is not a move, and reporting it as one would make the
    UI claim work it did not do. ``missing`` lists ids that do not exist;
    the caller reports them rather than failing the whole batch.
    """
    if collection_id is not None and await db.get(Collection, collection_id) is None:
        raise ValueError(f"collection not found: {collection_id}")
    if not doc_ids:
        return 0, []
    wanted = list(dict.fromkeys(doc_ids))
    rows = (
        await db.execute(
            select(Document.id, Document.collection_id).where(Document.id.in_(wanted))
        )
    ).all()
    existing = {doc_id for doc_id, _ in rows}
    missing = [doc_id for doc_id in wanted if doc_id not in existing]
    changing = [
        doc_id for doc_id, current in rows if current != collection_id
    ]
    if not changing:
        return 0, missing
    result = await db.execute(
        update(Document)
        .where(Document.id.in_(changing))
        .values(collection_id=collection_id, updated_at=Document.updated_at)
    )
    return result.rowcount or 0, missing


async def suggest_collections(db: AsyncSession) -> list[dict]:
    """Proposed topic collections for currently unfiled documents.

    A fresh library has ``collections = 0`` and every document unfiled,
    which makes the collection axis (and scoped search) inert. Grouping a
    numbered document library by designation family is a safe, reversible
    default: every document is still visible, and any assignment can be
    changed in the Web UI. Documents whose name carries no designation stay
    unfiled.
    """
    rows = (
        await db.execute(
            select(Document.id, Document.title, Document.filename).where(
                Document.collection_id.is_(None)
            )
        )
    ).all()
    groups: dict[str, list[dict]] = {}
    for doc_id, title, filename in rows:
        key = family_key(title, filename)
        if key is None:
            continue
        groups.setdefault(key, []).append(
            {"id": doc_id, "title": title, "filename": filename}
        )
    return [
        {
            "name": name,
            "description": f"{name} 系列文档",
            "doc_ids": [doc["id"] for doc in docs],
            "documents": docs,
        }
        for name, docs in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))
        if docs
    ]


async def apply_suggestions(
    db: AsyncSession, suggestions: list[dict] | None = None
) -> dict:
    """Create the suggested collections and file their documents.

    Idempotent: an existing collection is reused (by casefolded name), and
    re-filing a document that is already there counts as no change.
    """
    suggestions = suggestions if suggestions is not None else await suggest_collections(db)
    created = 0
    assigned = 0
    for suggestion in suggestions:
        name = suggestion["name"]
        collection = (
            await db.execute(
                select(Collection).where(Collection.name_key == name_key(name))
            )
        ).scalar_one_or_none()
        if collection is None:
            collection = await create_collection(
                db, name, suggestion.get("description")
            )
            created += 1
        updated, _missing = await assign_documents(
            db, suggestion["doc_ids"], collection.id
        )
        assigned += updated
    return {
        "collections": len(suggestions),
        "created": created,
        "assigned": assigned,
    }
