"""Quality issues and per-document stats, shared by the Web API and curator.

These used to live in ``app/web_api.py``. The curator needs the same issue
enumeration the quality page shows (ADR-0002 §12), and it must not import
FastAPI, so the read-only SQLite helpers moved here. ``web_api`` imports them
back; behaviour is unchanged and the 421-test baseline verifies it.
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from ..identifiers import document_key
from ..ingest.printed_toc import toc_page_indices
from ..ingest.toc import validate_toc
from ..ocr.cache import OcrCacheError, load_cached_pages


def connect_ro(db_path: Path) -> sqlite3.Connection:
    """A read-only, query-only connection to the archive database."""
    connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def issue_id(doc_id: str, kind: str, node_id: str | None = None) -> str:
    """Stable id for a quality group, so notes can point at the exact
    complaint rather than just the document it lives in."""
    return f"{doc_id}:{kind}" + (f":{node_id}" if node_id else "")


def quality_groups(db_path: Path, data_dir: Path | None) -> list[dict]:
    """Every quality complaint in the library, each with a stable id.

    Shared by /api/quality and /api/overview so the overview's alert list,
    the quality page and the notes that answer them all agree on identity.
    """
    documents = doc_summaries(db_path, data_dir)
    groups: list[dict] = []
    for doc in documents:
        stats = compute_doc_stats(db_path, doc_row(db_path, doc["id"]), data_dir)
        if doc["status"] == "failed":
            groups.append(
                {
                    "id": issue_id(doc["id"], "failed"),
                    "kind": "failed",
                    "severity": "error",
                    "title": "解析失败",
                    "count": 1,
                    "desc": f"{doc['filename']} · {doc['error'] or '解析失败'}",
                    "docId": doc["id"],
                    "docTitle": doc["title"],
                }
            )
        elif doc["status"] not in ("ready", "failed"):
            groups.append(
                {
                    "id": issue_id(doc["id"], "parsing"),
                    "kind": "parsing",
                    "severity": "warning",
                    "title": "解析中",
                    "count": 1,
                    "desc": f"{doc['filename']} · 当前数据不完整",
                    "docId": doc["id"],
                    "docTitle": doc["title"],
                }
            )
        if stats["unplacedNodes"]:
            groups.append(
                {
                    "id": issue_id(doc["id"], "unplaced"),
                    "kind": "unplaced",
                    "severity": "warning",
                    "title": "未定位节点",
                    "count": stats["unplacedNodes"],
                    "desc": f"{doc['title']} · 没有页码证据，目录中显示为 pages ?",
                    "docId": doc["id"],
                    "docTitle": doc["title"],
                }
            )
        if stats["missingPages"]:
            groups.append(
                {
                    "id": issue_id(doc["id"], "missing-pages"),
                    "kind": "missing-pages",
                    "severity": "warning",
                    "title": "缺失页",
                    "count": stats["missingPages"],
                    "desc": f"{doc['title']} · 正文覆盖 {stats['covered']}/{stats['contentPages']}",
                    "docId": doc["id"],
                    "docTitle": doc["title"],
                }
            )
        if stats["shortChunks"]:
            groups.append(
                {
                    "id": issue_id(doc["id"], "short-chunks"),
                    "kind": "short-chunks",
                    "severity": "warning",
                    "title": "短切片",
                    "count": stats["shortChunks"],
                    "desc": (
                        f"{doc['title']} · {stats['shortChunks']} 个切片少于 50 字，"
                        "可能是标题碎片、表格残片或真正的短节"
                    ),
                    "docId": doc["id"],
                    "docTitle": doc["title"],
                }
            )
        for issue in stats["issueMessages"]:
            if "duplicate" in issue["message"] or "重复" in issue["message"]:
                groups.append(
                    {
                        "id": issue_id(doc["id"], "duplicate-heading", issue["nodeId"]),
                        "kind": "duplicate-heading",
                        "severity": "warning",
                        "title": "重复标题编号",
                        "count": 1,
                        "desc": f"{doc['title']} · {issue['message']}",
                        "docId": doc["id"],
                        "docTitle": doc["title"],
                        "nodeId": issue["nodeId"],
                    }
                )

    # Library-level: two ready documents carrying the same document number.
    # Detection only; merging or re-identifying documents is a human call.
    by_key: dict[str, list[dict]] = defaultdict(list)
    for doc in documents:
        if doc["status"] != "ready":
            continue
        key = document_key(doc["title"])
        if key:
            by_key[key].append(doc)
    for key, docs in by_key.items():
        if len(docs) < 2:
            continue
        for doc in docs:
            other = next(item for item in docs if item["id"] != doc["id"])
            groups.append(
                {
                    "id": issue_id(doc["id"], "duplicate-doc", key),
                    "kind": "duplicate-doc",
                    "severity": "warning",
                    "title": "重复文档",
                    "count": len(docs) - 1,
                    "desc": (
                        f"{doc['title']} 与《{other['title']}》"
                        f"使用同一文档编号 {key}（对方 {other['pages']} 页）"
                    ),
                    "docId": doc["id"],
                    "docTitle": doc["title"],
                    "relatedDocId": other["id"],
                    "relatedDocTitle": other["title"],
                }
            )
    return groups


def note_counts_by_issue(db_path: Path) -> dict[str, int]:
    """How many notes answer each quality issue (issue_id -> count).

    Tolerates a database that predates the issue_id column: the column is
    added by ensure_schema() at startup, but a read-only connection opened
    against an older file must not turn the overview into a 500.
    """
    with closing(connect_ro(db_path)) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(agent_notes)")
        }
        if "issue_id" not in columns:
            return {}
        rows = connection.execute(
            "SELECT issue_id, COUNT(*) FROM agent_notes "
            "WHERE issue_id IS NOT NULL GROUP BY issue_id"
        ).fetchall()
    return {row[0]: row[1] for row in rows}


# ── per-document quality stats (memoized) ────────────────────────────────

_stats_cache: dict[tuple, dict] = {}


def invalidate_stats_cache() -> None:
    _stats_cache.clear()


def compute_doc_stats(
    db_path: Path, doc_row: sqlite3.Row, data_dir: Path | None
) -> dict:
    """Coverage / issue stats for one document, mirroring the prototype's
    quality model: covered, contentPages, nonContent, errors, warnings and
    the six per-document checks."""
    doc_id = doc_row["id"]
    page_count = doc_row["page_count"] or 0
    key = (
        doc_id,
        doc_row["updated_at"],
        doc_row["node_count"],
        doc_row["chunk_count"],
        str(db_path),
    )
    cached = _stats_cache.get(key)
    if cached is not None:
        return cached

    with closing(connect_ro(db_path)) as connection:
        nodes = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM toc_nodes WHERE doc_id = ? ORDER BY id", (doc_id,)
            ).fetchall()
        ]
        all_chunks = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM chunks WHERE doc_id = ? ORDER BY chunk_index", (doc_id,)
            ).fetchall()
        ]

    children: dict[str | None, list[str]] = defaultdict(list)
    for node in nodes:
        children[node["parent_id"]].append(node["node_id"])
    direct = Counter(chunk["node_id"] for chunk in all_chunks if chunk["node_id"])

    proxies = [SimpleNamespace(**node) for node in nodes]
    issues = validate_toc(proxies, page_count)
    errors = sum(1 for issue in issues if issue.severity == "error")
    warnings = sum(1 for issue in issues if issue.severity == "warning")

    page_chunks: set[int] = set()
    for chunk in all_chunks:
        if chunk["page_start"] is None or chunk["page_end"] is None:
            continue
        for page in range(
            max(0, chunk["page_start"]), min(page_count - 1, chunk["page_end"]) + 1
        ):
            page_chunks.add(page)

    content_start = min(
        (chunk["page_start"] for chunk in all_chunks if chunk["page_start"] is not None),
        default=0,
    )
    content_start = max(0, min(content_start, page_count))
    non_content = set(range(content_start))
    directory = data_dir or db_path.parent
    try:
        ocr_pages = load_cached_pages(directory / "ocr" / doc_id)
    except OcrCacheError:
        ocr_pages = None
    if ocr_pages:
        non_content |= {
            page for page in toc_page_indices(ocr_pages) if 0 <= page < page_count
        }
    non_content = {page for page in non_content if 0 <= page < page_count}

    covered = sum(1 for page in page_chunks if page not in non_content)
    content_pages = max(0, page_count - len(non_content))
    missing_pages = max(0, content_pages - covered)

    stats = {
        "covered": covered,
        "contentPages": content_pages,
        "nonContent": len(non_content),
        "missingPages": missing_pages,
        "errors": errors,
        "warnings": warnings,
        "nodesWithoutDirect": sum(
            1 for node in nodes if direct.get(node["node_id"], 0) == 0
        ),
        "leafNodesWithoutChunks": sum(
            1
            for node in nodes
            if not children.get(node["node_id"]) and direct.get(node["node_id"], 0) == 0
        ),
        "orphanChunks": sum(1 for chunk in all_chunks if not chunk["node_id"]),
        "shortChunks": sum(
            1 for chunk in all_chunks if len(chunk["content"] or "") < 50
        ),
        "unplacedNodes": sum(1 for node in nodes if node["page_start"] is None),
        "issueMessages": [
            {"severity": issue.severity, "message": issue.message, "nodeId": issue.node_id}
            for issue in issues
        ][:20],
    }
    _stats_cache[key] = stats
    return stats


# ── document summaries ────────────────────────────────────────────────────


def latest_runs(db_path: Path) -> dict[str, dict]:
    """Newest ingest_runs row per document, keyed by doc_id."""
    try:
        with closing(connect_ro(db_path)) as connection:
            rows = connection.execute(
                """
                SELECT r.* FROM ingest_runs r
                JOIN (
                    SELECT doc_id, MAX(id) AS max_id
                    FROM ingest_runs GROUP BY doc_id
                ) latest ON latest.max_id = r.id
                """
            ).fetchall()
    except sqlite3.OperationalError:
        return {}  # database predates the table; caller shows "未记录"
    return {row["doc_id"]: dict(row) for row in rows}


def provenance_out(row: dict | None) -> dict:
    """List-friendly provenance from a raw ingest_runs row."""
    from ..ingest.provenance import provenance_summary

    if row is None:
        return provenance_summary(None)

    # provenance_summary reads ORM attributes; a SimpleNamespace matches.
    return provenance_summary(SimpleNamespace(**row))


def doc_summaries(db_path: Path, data_dir: Path | None) -> list[dict]:
    runs = latest_runs(db_path)
    with closing(connect_ro(db_path)) as connection:
        rows = connection.execute(
            """
            SELECT d.*,
                   c.name AS collection_name,
                   (SELECT COUNT(*) FROM toc_nodes t WHERE t.doc_id = d.id)
                       AS node_count,
                   (SELECT COUNT(*) FROM chunks c2 WHERE c2.doc_id = d.id)
                       AS chunk_count,
                   (SELECT body FROM agent_notes n
                     WHERE n.doc_id = d.id AND n.kind = 'ingest'
                     ORDER BY n.id DESC LIMIT 1) AS ingest_note,
                   (SELECT id FROM agent_notes n
                     WHERE n.doc_id = d.id AND n.kind = 'ingest'
                     ORDER BY n.id DESC LIMIT 1) AS ingest_note_id
            FROM documents d
            LEFT JOIN collections c ON c.id = d.collection_id
            ORDER BY d.created_at DESC
            """
        ).fetchall()

    summaries = []
    for row in rows:
        doc = dict(row)
        stats = compute_doc_stats(db_path, row, data_dir)
        summaries.append(
            {
                "id": doc["id"],
                "title": doc["title"],
                "filename": doc["filename"],
                "filePath": doc["file_path"],
                "ns": doc["namespace_id"],
                "type": doc["doc_type"] or ext_type(doc["filename"]),
                "size": file_size(doc["file_path"]),
                "pages": doc["page_count"] or 0,
                "nodes": doc["node_count"],
                "chunks": doc["chunk_count"],
                "covered": stats["covered"],
                "contentPages": stats["contentPages"],
                "nonContent": stats["nonContent"],
                "errors": stats["errors"],
                "warnings": stats["warnings"],
                "status": doc["status"],
                "error": doc["error"],
                # Live ingest state (written by ingest before parsing starts).
                "stage": doc.get("stage"),
                "progress": doc.get("progress"),
                "plan": doc.get("parse_plan"),
                # 入库备注 (human-written at upload, editable on the library page).
                "ingestNote": doc.get("ingest_note"),
                "ingestNoteId": doc.get("ingest_note_id"),
                "createdAt": doc["created_at"],
                "updatedAt": doc["updated_at"],
                "collectionId": doc["collection_id"],
                "collectionName": doc["collection_name"],
                "provenance": provenance_out(runs.get(doc["id"])),
            }
        )
    return summaries


def ext_type(filename: str) -> str:
    suffix = Path(filename).suffix.lstrip(".").lower()
    return {
        "docx": "doc",
        "md": "md",
        "markdown": "md",
        "txt": "txt",
        "xlsx": "xlsx",
        "xls": "xlsx",
        "csv": "csv",
        "tsv": "csv",
        "png": "image",
        "jpg": "image",
        "jpeg": "image",
        "webp": "image",
        "bmp": "image",
        "tif": "image",
        "tiff": "image",
        "gif": "image",
    }.get(suffix, suffix or "pdf")


def file_size(file_path: str | None) -> int | None:
    if not file_path:
        return None
    try:
        return Path(file_path).stat().st_size
    except OSError:
        return None


def doc_row(db_path: Path, doc_id: str) -> sqlite3.Row:
    with closing(connect_ro(db_path)) as connection:
        row = connection.execute(
            """
            SELECT d.*,
                   (SELECT COUNT(*) FROM toc_nodes t WHERE t.doc_id = d.id)
                       AS node_count,
                   (SELECT COUNT(*) FROM chunks c WHERE c.doc_id = d.id)
                       AS chunk_count
            FROM documents d WHERE d.id = ?
            """,
            (doc_id,),
        ).fetchone()
    if row is None:
        raise LookupError(f"document not found: {doc_id}")
    return row


def non_content_pages(
    db_path: Path, doc_id: str, data_dir: Path | None, content_start: int
) -> set[int]:
    pages = set(range(max(0, content_start)))
    directory = data_dir or db_path.parent
    try:
        ocr_pages = load_cached_pages(directory / "ocr" / doc_id)
    except OcrCacheError:
        return pages
    if not ocr_pages:
        return pages
    return pages | toc_page_indices(ocr_pages)
