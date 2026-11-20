from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..ingest.tabletext import table_html_to_markdown
from ..models import Chunk, Document, Resource, TocNode
from ..ocr.parser import _html_to_text

_REF_LIMIT = 12
_LIST_LIMIT_MAX = 200
_RESOURCE_TYPES = {"", "table", "figure"}


def resource_ref(resource: Resource) -> dict[str, Any]:
    """A lightweight handle an agent can act on without fetching content."""
    return {
        "resource_id": resource.resource_id,
        "type": resource.type,
        "title": resource.title,
        "page": resource.page_idx,
        "node_id": resource.node_id,
        "has_content": bool(resource.content),
        "has_image": bool(resource.image_path),
    }


def figures_of(anchor: dict[str, Any]) -> list[dict[str, Any]]:
    """The figure subset of an anchor's attached resource handles."""
    return [
        ref for ref in anchor.get("resources") or [] if ref.get("type") == "figure"
    ]


_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def image_mime(path: Path | str) -> str:
    """Best-effort MIME type from the file suffix; PNG is the safe default."""
    return _MIME_BY_SUFFIX.get(Path(path).suffix.lower(), "image/png")


def _covers(
    resource: Resource,
    node_id: str | None,
    page_start: int | None,
    page_end: int | None,
) -> bool:
    """Page-first matching: a section read sees every resource on its pages.

    Node equality is only a fallback when no page bound can match (a
    resource without a page, or an anchor without one).
    """
    page = resource.page_idx
    if page is not None and (page_start is not None or page_end is not None):
        if (page_start is None or page >= page_start) and (
            page_end is None or page <= page_end
        ):
            return True
    return bool(node_id and resource.node_id == node_id)


async def list_resources(
    db: AsyncSession,
    doc_id: str,
    *,
    page_start: int | None = None,
    page_end: int | None = None,
    type_: str = "",
    query: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    """List table/figure handles for one document.

    ``query`` is a case-insensitive substring over the caption/title, not a
    fuzzy match: agents should use it for "表1" / "图3"-style discovery and
    then fetch by ``resource_id``.
    """
    if await db.get(Document, doc_id) is None:
        raise ValueError(f"document not found: {doc_id}")
    if type_ not in _RESOURCE_TYPES:
        raise ValueError(f"invalid resource type: {type_}")
    limit = max(1, min(limit, _LIST_LIMIT_MAX))

    stmt = select(Resource).where(Resource.doc_id == doc_id)
    if type_:
        stmt = stmt.where(Resource.type == type_)
    if page_start is not None:
        stmt = stmt.where(Resource.page_idx >= page_start)
    if page_end is not None:
        stmt = stmt.where(Resource.page_idx <= page_end)
    if query:
        stmt = stmt.where(
            func.instr(
                func.lower(func.coalesce(Resource.title, "")), query.lower()
            )
            > 0
        )
    rows = list(
        (await db.execute(stmt.order_by(Resource.page_idx, Resource.id))).scalars()
    )
    return {
        "doc_id": doc_id,
        "count": len(rows),
        "truncated": len(rows) > limit,
        "resources": [resource_ref(row) for row in rows[:limit]],
    }


async def get_resource(
    db: AsyncSession, doc_id: str, resource_id: str
) -> tuple[dict[str, Any], str | None]:
    """Return (public payload, server-only image path).

    The image path stays out of the payload: MCP callers get a local file
    path only when they explicitly ask for image content.
    """
    resource = (
        await db.execute(
            select(Resource).where(
                Resource.doc_id == doc_id, Resource.resource_id == resource_id
            )
        )
    ).scalar_one_or_none()
    if resource is None:
        raise ValueError(f"resource not found: {doc_id}/{resource_id}")

    content: str | None = None
    if resource.type == "table" and resource.content:
        content = table_html_to_markdown(resource.content) or _html_to_text(
            resource.content
        )

    heading_path: str | None = None
    if resource.node_id:
        heading_path = (
            await db.execute(
                select(TocNode.heading_path).where(
                    TocNode.doc_id == doc_id,
                    TocNode.node_id == resource.node_id,
                )
            )
        ).scalar_one_or_none()

    doc = await db.get(Document, doc_id)
    payload = {
        "doc_id": doc_id,
        "doc_title": doc.title if doc is not None else None,
        "resource_id": resource.resource_id,
        "type": resource.type,
        "title": resource.title,
        "page": resource.page_idx,
        "node_id": resource.node_id,
        "heading_path": heading_path,
        "content": content,
        "image_available": bool(resource.image_path),
    }
    return payload, resource.image_path


async def attach_resources(
    db: AsyncSession, anchors: list[dict[str, Any]], *, limit: int = _REF_LIMIT
) -> None:
    """Attach bounded resource refs to search hits / citations / section reads.

    Each resource appears at most once per response, on the first anchor
    that covers it, so adjacent chunks sharing a page do not repeat a
    table or figure.
    """
    if not anchors:
        return
    doc_ids = {anchor["doc_id"] for anchor in anchors if anchor.get("doc_id")}
    if not doc_ids:
        for anchor in anchors:
            anchor["resources"] = []
        return

    rows = list(
        (
            await db.execute(
                select(Resource)
                .where(Resource.doc_id.in_(doc_ids))
                .order_by(Resource.page_idx, Resource.id)
            )
        ).scalars()
    )
    by_doc: dict[str, list[Resource]] = defaultdict(list)
    for resource in rows:
        by_doc[resource.doc_id].append(resource)

    seen: set[tuple[str, str]] = set()
    for anchor in anchors:
        refs: list[dict[str, Any]] = []
        doc_id = anchor.get("doc_id")
        for resource in by_doc.get(doc_id, []):
            key = (resource.doc_id, resource.resource_id)
            if key in seen:
                continue
            if not _covers(
                resource,
                anchor.get("node_id"),
                anchor.get("page_start"),
                anchor.get("page_end"),
            ):
                continue
            refs.append(resource_ref(resource))
            if len(refs) >= limit:
                break
        seen.update((doc_id, ref["resource_id"]) for ref in refs)
        anchor["resources"] = refs


def _field(obj: Any, name: str) -> Any:
    """Read a field from an ORM row or a raw dict row."""
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name)


def resource_owner(
    resource: Any, chunks: list[Any]
) -> Any | None:
    """Pick the most specific chunk owning a resource.

    Accepts ORM objects or dict rows (the Web reading stream uses dicts).
    A node-bound resource belongs to its own node; otherwise the narrowest
    page-covering chunk wins, preferring the chunk that starts on the page.
    """
    page = _field(resource, "page_idx")
    node_id = _field(resource, "node_id")
    if node_id:
        node_chunks = [
            chunk for chunk in chunks if _field(chunk, "node_id") == node_id
        ]
        covering = [
            chunk
            for chunk in node_chunks
            if page is not None
            and _field(chunk, "page_start") is not None
            and _field(chunk, "page_end") is not None
            and _field(chunk, "page_start") <= page <= _field(chunk, "page_end")
        ]
        # Prefer a chunk that actually contains the page; a narrower chunk
        # elsewhere in the same node must not win on span alone. Fall back
        # to the whole node when no chunk records page bounds.
        candidates = covering or node_chunks
    elif page is not None:
        candidates = [
            chunk
            for chunk in chunks
            if _field(chunk, "page_start") is not None
            and _field(chunk, "page_end") is not None
            and _field(chunk, "page_start") <= page <= _field(chunk, "page_end")
        ]
    else:
        return None
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda chunk: (
            (_field(chunk, "page_end") or 0) - (_field(chunk, "page_start") or 0),
            _field(chunk, "page_start") != page,
            _field(chunk, "chunk_index"),
        ),
    )


async def link_resources(db: AsyncSession, doc_id: str) -> int:
    """Persist each resource's owning section node; returns rows updated.

    Only fills NULL owners, so it is safe to re-run. Resources whose pages
    have no chunk (a figure on a skipped cover/TOC page) stay unlinked and
    keep working through the page-fallback read path.
    """
    chunks = list(
        (
            await db.execute(
                select(Chunk)
                .where(Chunk.doc_id == doc_id)
                .order_by(Chunk.chunk_index)
            )
        ).scalars()
    )
    resources = list(
        (
            await db.execute(select(Resource).where(Resource.doc_id == doc_id))
        ).scalars()
    )
    updated = 0
    for resource in resources:
        if resource.node_id:
            continue
        owner = resource_owner(resource, chunks)
        if owner is None or owner.node_id is None:
            continue
        resource.node_id = owner.node_id
        updated += 1
    return updated


def image_file(image_path: str | None, resources_root: Path) -> Path | None:
    """Resolve an image path only when it is a real file under the root."""
    if not image_path:
        return None
    path = Path(image_path).resolve()
    root = Path(resources_root).resolve()
    if path != root and not path.is_relative_to(root):
        return None
    if not path.is_file():
        return None
    return path
