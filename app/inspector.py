"""Standalone read-only chunk inspector.

    python -m app.inspector --db data/tocmemex.db --port 8765

Serves a local web UI and a small JSON API over the existing SQLite database.
No OCR calls, no writes, no external services.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlparse

from .bindguard import enforce_bind_host, is_allowed_request_host
from .ingest.printed_toc import toc_page_indices
from .ingest.toc import validate_toc
from .ocr.cache import OcrCacheError, load_cached_pages
from .ocr.parser import page_text
from .paths import is_safe_doc_id

STATIC_DIR = Path(__file__).resolve().parent / "inspector_ui"
STATIC_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
}


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{db_path.resolve().as_uri()}?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def list_documents(db_path: Path) -> list[dict]:
    with closing(_connect(db_path)) as connection:
        rows = connection.execute(
            """
            SELECT d.id, d.title, d.filename, d.namespace_id, d.doc_type,
                   d.doc_date, d.page_count, d.status, d.error, d.created_at,
                   (SELECT COUNT(*) FROM toc_nodes t WHERE t.doc_id = d.id)
                       AS node_count,
                   (SELECT COUNT(*) FROM chunks c WHERE c.doc_id = d.id)
                       AS chunk_count
            FROM documents d
            ORDER BY d.created_at DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]


def _children_map(nodes: list[dict]) -> dict[str | None, list[str]]:
    children: dict[str | None, list[str]] = defaultdict(list)
    for node in nodes:
        children[node["parent_id"]].append(node["node_id"])
    return children


def _non_content_pages(
    db_path: Path,
    doc_id: str,
    data_dir: Path | None,
    content_start: int,
) -> set[int]:
    """Pages that are not body content, shown grey in the coverage strip.

    Everything before the first content chunk is front matter. When the
    cached OCR is available, also mark every printed TOC page, including the
    per-chapter TOCs that appear inside the body.
    """
    pages = set(range(max(0, content_start)))
    directory = data_dir or db_path.parent
    try:
        ocr_pages = load_cached_pages(directory / "ocr" / doc_id)
    except OcrCacheError:
        return pages
    if not ocr_pages:
        return pages
    return pages | toc_page_indices(ocr_pages)


def document_payload(
    db_path: Path, doc_id: str, data_dir: Path | None = None
) -> dict | None:
    if not is_safe_doc_id(doc_id):
        return None
    with closing(_connect(db_path)) as connection:
        doc_row = connection.execute(
            "SELECT * FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        if doc_row is None:
            return None
        nodes = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM toc_nodes WHERE doc_id = ? ORDER BY id", (doc_id,)
            ).fetchall()
        ]
        chunks = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM chunks WHERE doc_id = ? ORDER BY chunk_index",
                (doc_id,),
            ).fetchall()
        ]

    doc = dict(doc_row)
    children = _children_map(nodes)
    direct = Counter(chunk["node_id"] for chunk in chunks if chunk["node_id"])

    subtree: dict[str, int] = {}

    def visit(node_id: str) -> int:
        if node_id in subtree:
            return subtree[node_id]
        total = direct.get(node_id, 0)
        for child in children.get(node_id, []):
            total += visit(child)
        subtree[node_id] = total
        return total

    for node in nodes:
        visit(node["node_id"])

    proxies = [SimpleNamespace(**node) for node in nodes]
    issues = validate_toc(proxies, doc.get("page_count") or 0)
    issue_by_node: dict[str, list[str]] = defaultdict(list)
    for issue in issues:
        if issue.node_id:
            issue_by_node[issue.node_id].append(issue.message)

    for node in nodes:
        node["direct_chunks"] = direct.get(node["node_id"], 0)
        node["subtree_chunks"] = subtree.get(node["node_id"], 0)
        node["issues"] = issue_by_node.get(node["node_id"], [])
        node["child_count"] = len(children.get(node["node_id"], []))

    page_count = doc.get("page_count") or 0
    page_chunks: dict[int, list[int]] = defaultdict(list)
    for chunk in chunks:
        start = chunk.get("page_start")
        end = chunk.get("page_end")
        if start is None or end is None:
            continue
        for page in range(max(0, start), min(page_count - 1, end) + 1):
            page_chunks[page].append(chunk["chunk_index"])
    content_start = min(
        (chunk["page_start"] for chunk in chunks if chunk["page_start"] is not None),
        default=0,
    )
    content_start = max(0, min(content_start, page_count))
    non_content = {
        page
        for page in _non_content_pages(db_path, doc_id, data_dir, content_start)
        if 0 <= page < page_count
    }
    coverage = [
        {
            "page": page,
            "chunks": page_chunks.get(page, []),
            "non_content": page in non_content,
        }
        for page in range(page_count)
    ]
    covered_pages = sum(
        1 for row in coverage if row["chunks"] and not row["non_content"]
    )
    non_content_count = len(non_content)
    content_pages = max(0, page_count - non_content_count)
    missing_pages = max(0, content_pages - covered_pages)

    stats = {
        "nodes": len(nodes),
        "chunks": len(chunks),
        "roots": sum(1 for node in nodes if node["parent_id"] is None),
        "nodes_without_direct": sum(
            1 for node in nodes if direct.get(node["node_id"], 0) == 0
        ),
        "leaf_nodes_without_chunks": sum(
            1
            for node in nodes
            if not children.get(node["node_id"])
            and direct.get(node["node_id"], 0) == 0
        ),
        "orphan_chunks": sum(1 for chunk in chunks if not chunk["node_id"]),
        "short_chunks": sum(
            1 for chunk in chunks if len(chunk.get("content") or "") < 50
        ),
        "unplaced_nodes": sum(
            1 for node in nodes if node.get("page_start") is None
        ),
        "covered_pages": covered_pages,
        "content_pages": content_pages,
        "non_content_pages": non_content_count,
        "missing_pages": missing_pages,
        "errors": sum(1 for issue in issues if issue.severity == "error"),
        "warnings": sum(1 for issue in issues if issue.severity == "warning"),
    }

    return {
        "document": doc,
        "nodes": nodes,
        "chunks": chunks,
        "issues": [
            {
                "severity": issue.severity,
                "message": issue.message,
                "node_id": issue.node_id,
            }
            for issue in issues
        ],
        "coverage": coverage,
        "stats": stats,
    }


def page_payload(
    db_path: Path, doc_id: str, page: int, data_dir: Path | None = None
) -> dict | None:
    if not is_safe_doc_id(doc_id):
        return None
    directory = data_dir or db_path.parent
    try:
        pages = load_cached_pages(directory / "ocr" / doc_id)
    except OcrCacheError:
        return None
    if not pages:
        return None
    target = next((item for item in pages if item.page_index == page), None)
    if target is None:
        return None
    return {
        "page": page,
        "text": page_text(target),
        "blocks": [
            {
                "label": block.label,
                "content": block.content,
                "bbox": list(block.bbox),
                "order": block.order,
            }
            for block in target.blocks
        ],
    }


class InspectorHandler(BaseHTTPRequestHandler):
    db_path: Path
    data_dir: Path | None
    server_version = "ChunkInspector/1.0"

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, name: str) -> None:
        path = STATIC_DIR / name
        if not path.exists():
            self._json({"error": "not found"}, 404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", STATIC_TYPES[name])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=60")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        # Loopback binding does not stop DNS rebinding: a page on
        # evil.example can re-resolve its own name to 127.0.0.1 and read this
        # API while the browser still sends ``Host: evil.example``. The
        # inspector is read-only but serves document text, so it needs the
        # same Host check as the web app (S2).
        if not is_allowed_request_host(self.headers.get("Host")):
            self._json({"error": "request rejected by host guard"}, 400)
            return
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        page_match = re.fullmatch(
            r"/api/documents/([^/]+)/page/(\d+)", path
        )
        if page_match:
            payload = page_payload(
                self.db_path,
                page_match.group(1),
                int(page_match.group(2)),
                self.data_dir,
            )
            if payload is None:
                self._json({"error": "page not available"}, 404)
            else:
                self._json(payload)
            return

        doc_match = re.fullmatch(r"/api/documents/([^/]+)", path)
        if doc_match:
            payload = document_payload(
                self.db_path, doc_match.group(1), self.data_dir
            )
            if payload is None:
                self._json({"error": "document not found"}, 404)
            else:
                self._json(payload)
            return

        if path == "/api/documents":
            self._json({"documents": list_documents(self.db_path)})
            return

        if path in ("/", "/index.html"):
            self._static("index.html")
            return

        name = path.lstrip("/")
        if name in STATIC_TYPES:
            self._static(name)
            return

        self._json({"error": "not found"}, 404)


def make_server(
    db_path: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    data_dir: Path | None = None,
) -> ThreadingHTTPServer:
    class Handler(InspectorHandler):
        pass

    Handler.db_path = db_path
    Handler.data_dir = data_dir
    return ThreadingHTTPServer((host, port), Handler)


def run_server(
    db_path: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    data_dir: Path | None = None,
) -> None:
    if not db_path.exists():
        raise SystemExit(
            f"database not found: {db_path}\n"
            "Run `python -m app.cli init` and ingest a document first."
        )

    server = make_server(db_path, host, port, data_dir)
    print(f"Chunk inspector: http://{host}:{port}")
    print(f"database: {db_path}")
    if data_dir:
        print(f"data dir: {data_dir}")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.inspector")
    parser.add_argument("--db", default="data/tocmemex.db")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    enforce_bind_host(args.host)

    db_path = Path(args.db)
    data_dir = Path(args.data_dir) if args.data_dir else db_path.parent
    run_server(db_path, args.host, args.port, data_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
