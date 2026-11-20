"""Toc Memex knowledge-base web API.

    python -m app.web_api --db data/tocmemex.db --port 8791 --web-dir web/dist

Serves the JSON API consumed by the Vue 3 + Vite frontend (`web/`) and, when
built, the frontend static bundle itself. Uploaded files are ingested by a
child ``app.cli ingest`` process; this server itself never calls OCR. Derived
data is otherwise not edited here: the writes are activity-layer records
(notes, collections, endpoint settings), one usage_log row per search hit,
and the upload rows that child ingest creates.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, urlparse

import sqlalchemy

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .agent.models import AgentRun, AgentStep
from .agent.runner import execute_run as agent_execute_run
from .agent.runner import run_out as agent_run_out
from .agent.runner import step_out as agent_step_out
from .bindguard import enforce_bind_host
from .db import make_engine, session_factory
from .curation import issues as issues_mod
from .index.documents import ranked_document_ids
from .index.fts import search_chunks
from .index.resources import resource_owner as _resource_owner
from .ingest.bench import run_scenario
from .ingest.export import ExportError, build_export, export_kind
from .ingest.provenance import run_out
from .ingest.toc import validate_toc
from .llm.client import LLMError, llm_complete
from .llm.endpoints import endpoint_out
from .models import AgentNote, Document, ModelEndpoint, OcrEndpoint, TocBenchRun, TocBenchScenario, new_uuid, utcnow_iso
from .ocr.endpoints import ocr_endpoint_out
from .ocr.cache import OcrCacheError, load_cached_pages
from .ocr.parser import page_text


logger = logging.getLogger(__name__)

_MAX_UPLOAD_BYTES = 200 * 1024 * 1024
_UPLOAD_STATUSES = ("queued", "indexing", "ready", "duplicate", "failed")


def _stream_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for piece in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(piece)
    return digest.hexdigest()


_IMAGE_PREFIXES = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"BM",
    b"II*\x00",
    b"MM\x00*",
)


def _validate_upload_content(path: Path, name: str) -> None:
    """Reject files whose bytes contradict their extension.

    This catches the common "renamed or truncated file" case before a
    background ingest spends minutes on it. Deeper corruption that still
    has a valid header is reported by the ingest task with a friendly
    error, not a 4xx, because ingest is intentionally asynchronous.
    """
    suffix = Path(name).suffix.lower()
    with path.open("rb") as handle:
        head = handle.read(1024)
    if suffix == ".pdf":
        if b"%PDF-" not in head:
            raise HTTPException(
                status_code=400, detail="文件不是有效的 PDF：缺少 %PDF 文件头"
            )
    elif suffix == ".docx":
        if not head.startswith(b"PK\x03\x04"):
            raise HTTPException(
                status_code=400, detail="文件不是有效的 DOCX：不是 ZIP 容器"
            )
    elif suffix == ".doc":
        if not head.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
            raise HTTPException(
                status_code=400, detail="文件不是有效的 DOC：不是 Word 复合文档"
            )
    elif suffix in {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".gif"}:
        if not any(head.startswith(prefix) for prefix in _IMAGE_PREFIXES):
            raise HTTPException(
                status_code=400, detail="文件不是有效的图片：文件头无法识别"
            )
    elif suffix == ".webp":
        if not (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
            raise HTTPException(
                status_code=400, detail="文件不是有效的 WEBP：文件头无法识别"
            )


_INGEST_ERROR_HINTS = (
    ("PdfReadError", "PDF 文件损坏或无法读取"),
    ("PdfStreamError", "PDF 文件损坏或无法读取"),
    ("EOFError", "文件内容不完整或已损坏"),
    ("BadZipFile", "DOCX 文件损坏（ZIP 容器不完整）"),
    ("UnidentifiedImageError", "图片文件损坏或格式不受支持"),
    ("ModuleNotFoundError", "解析依赖缺失，请检查安装"),
    ("OCR", "OCR 解析失败；到设置页点「测试」检查端点，或点「重新上传」重试"),
)


def _friendly_ingest_error(detail: str) -> str:
    """Turn a child-process traceback into something a user can act on.

    The text must answer two questions the UI cannot: what actually failed
    (local file, credentials, rate limit, or the vendor's own 5xx), and what
    to do next. The retry path is the document row's 「重新上传」 button.
    """
    text = detail or ""

    # A vendor async job that the OCR service itself failed (state=failed).
    # The *last* occurrence is the raised exception; earlier ones are
    # traceback source lines that contain the literal "job failed:".
    if "job failed" in text:
        tail = text.rsplit("job failed", 1)[1]
        reason = tail.splitlines()[0].strip() if tail else ""
        reason = re.sub(r"^\s*\(job [^)]+\):\s*", "", reason).strip(" :\"'")
        reason = reason[:200] or "未知原因"
        lowered = reason.lower()
        if "500" in reason:
            return (
                f"解析失败：OCR 服务端执行任务时返回 500（{reason}）。"
                "文件本身没问题，这是 OCR 服务的临时故障。"
                "点这条任务的「重新上传」重试，通常第二次就能通过；"
                "若连续失败，到「设置 → OCR 端点」点「测试」确认服务可用，"
                "或换一张图再试。"
            )
        if "401" in reason or "403" in reason or "认证" in reason or "auth" in lowered:
            return (
                f"解析失败：OCR 服务认证失败（{reason}）。"
                "到「设置 → OCR 端点」检查 Token 后点「测试」。"
            )
        if "429" in reason or "限流" in reason or "频繁" in reason or "limit" in lowered:
            return (
                f"解析失败：OCR 服务限流（{reason}）。"
                "等 1 分钟后点「重新上传」再试。"
            )
        return (
            f"解析失败：OCR 服务端任务执行失败（{reason}）。"
            "点「重新上传」重试；若反复失败，到设置页测试 OCR 端点，"
            "或换一张图/把图片缩小后再试。"
        )

    lowered = text.lower()
    if "submit failed http 401" in lowered or "submit failed http 403" in lowered:
        return (
            "解析失败：OCR 服务拒绝认证（HTTP 401/403）。"
            "到「设置 → OCR 端点」重新填写 Token 并点「测试」。"
        )
    if "submit failed http 429" in lowered:
        return "解析失败：OCR 服务限流（HTTP 429）。等 1 分钟后点「重新上传」再试。"
    if "poll timeout" in lowered:
        return (
            "解析失败：OCR 服务超过等待时限仍未完成。"
            "稍后在解析队列点「重新上传」；大文件可先拆分或压缩。"
        )
    if "submit failed after retries" in lowered:
        return (
            "解析失败：连接不上 OCR 服务（已自动重试）。"
            "检查网络和「设置 → OCR 端点」的地址/Token 后点「测试」。"
        )
    for needle, message in _INGEST_ERROR_HINTS:
        if needle in text:
            return f"解析失败：{message}"
    return "解析失败：文件无法解析，请确认格式与完整性"


def _display_ingest_error(error: str | None) -> str | None:
    """Hide unreadable errors left by older versions in the queue.

    Before the child ran with PYTHONIOENCODING=utf-8, a GBK traceback was
    decoded as UTF-8 and the vendor's Chinese message became replacement
    characters. The raw text is gone, so say what happened and how to
    recover instead of showing mojibake forever.
    """
    if not error:
        return None
    if "�" in error or "data.get('errorMsg')" in error:
        return (
            "解析失败：OCR 服务端临时故障（旧版本记录，服务端错误详情已丢失）。"
            "点「重新上传」用原文件重试即可。"
        )
    return error


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


# Inline media types for the original-file route. Anything else is served
# as an octet stream so the browser downloads rather than guesses.
_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".csv": "text/csv; charset=utf-8",
    ".tsv": "text/tab-separated-values; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".gif": "image/gif",
    "image": "image/jpeg",
}


_issue_id = issues_mod.issue_id
_quality_groups = issues_mod.quality_groups
_note_counts_by_issue = issues_mod.note_counts_by_issue
compute_doc_stats = issues_mod.compute_doc_stats
_invalidate_cache = issues_mod.invalidate_stats_cache


# ── document summaries ────────────────────────────────────────────────────


_latest_runs = issues_mod.latest_runs
_provenance_out = issues_mod.provenance_out
_doc_summaries = issues_mod.doc_summaries
_ext_type = issues_mod.ext_type
_file_size = issues_mod.file_size


# ── document detail ───────────────────────────────────────────────────────


def _document_detail(db_path: Path, doc_id: str, data_dir: Path | None) -> dict | None:
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
                "SELECT * FROM chunks WHERE doc_id = ? ORDER BY chunk_index", (doc_id,)
            ).fetchall()
        ]
        resources = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM resources WHERE doc_id = ? ORDER BY id", (doc_id,)
            ).fetchall()
        ]
        ns_row = connection.execute(
            "SELECT id, kind FROM namespaces WHERE id = ?",
            (doc_row["namespace_id"],),
        ).fetchone()
        collection_row = connection.execute(
            "SELECT id, name FROM collections WHERE id = ?",
            (doc_row["collection_id"],),
        ).fetchone()
        note_row = connection.execute(
            "SELECT id, body, author, created_at FROM agent_notes "
            "WHERE doc_id = ? AND kind = 'ingest' ORDER BY id DESC LIMIT 1",
            (doc_id,),
        ).fetchone()

    doc = dict(doc_row)
    doc["ns"] = doc.pop("namespace_id")
    doc["nsKind"] = ns_row["kind"] if ns_row else None
    doc["type"] = doc.pop("doc_type") or _ext_type(doc["filename"])
    doc["size"] = _file_size(doc["file_path"])
    doc["collectionId"] = collection_row["id"] if collection_row else None
    doc["collectionName"] = collection_row["name"] if collection_row else None
    doc["ingestNote"] = note_row["body"] if note_row else None
    doc["ingestNoteId"] = note_row["id"] if note_row else None
    doc.pop("content_hash", None)

    children: dict[str | None, list[str]] = defaultdict(list)
    for node in nodes:
        children[node["parent_id"]].append(node["node_id"])
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
        node["directChunks"] = direct.get(node["node_id"], 0)
        node["subtreeChunks"] = subtree.get(node["node_id"], 0)
        node["issues"] = issue_by_node.get(node["node_id"], [])
        node["childCount"] = len(children.get(node["node_id"], []))

    # Attach each resource to exactly one chunk. Page-overlap attachment used
    # to put the same table or figure on every chunk that touched its page
    # (363 of the 672 resources in the memex corpus), so the reading stream
    # showed the same table two or three times. The owner is the most
    # specific chunk covering the page; a node-bound resource belongs to the
    # first chunk of its node.
    shaped_resources: list[tuple[dict, dict]] = []
    for resource in resources:
        shaped_resources.append(
            (
                resource,
                {
                    "id": resource["resource_id"],
                    "type": resource["type"],
                    "title": resource["title"],
                    "page": resource["page_idx"],
                    "content": resource["content"],
                    "image": f"/api/documents/{doc_id}/resources/{resource['resource_id']}/image"
                    if resource["image_path"]
                    else None,
                },
            )
        )

    def resource_owner(resource: dict) -> dict | None:
        return _resource_owner(resource, chunks)

    by_chunk: dict[int, list[dict]] = defaultdict(list)
    for resource, shaped in shaped_resources:
        owner = resource_owner(resource)
        if owner is not None:
            by_chunk[owner["chunk_index"]].append(shaped)

    for chunk in chunks:
        chunk["resources"] = by_chunk.get(chunk["chunk_index"], [])

    page_count = doc.get("page_count") or 0
    page_chunks: dict[int, list[int]] = defaultdict(list)
    for chunk in chunks:
        if chunk["page_start"] is None or chunk["page_end"] is None:
            continue
        for page in range(max(0, chunk["page_start"]), min(page_count - 1, chunk["page_end"]) + 1):
            page_chunks[page].append(chunk["chunk_index"])
    content_start = min(
        (chunk["page_start"] for chunk in chunks if chunk["page_start"] is not None),
        default=0,
    )
    content_start = max(0, min(content_start, page_count))
    non_content_pages = _non_content_pages(db_path, doc_id, data_dir, content_start)
    non_content = {
        page for page in non_content_pages if 0 <= page < page_count
    }
    coverage = [
        {
            "page": page,
            "chunks": page_chunks.get(page, []),
            "nonContent": page in non_content,
        }
        for page in range(page_count)
    ]

    stats = {
        "nodes": len(nodes),
        "chunks": len(chunks),
        "roots": sum(1 for node in nodes if node["parent_id"] is None),
        "nodesWithoutDirect": sum(
            1 for node in nodes if direct.get(node["node_id"], 0) == 0
        ),
        "leafNodesWithoutChunks": sum(
            1
            for node in nodes
            if not children.get(node["node_id"]) and direct.get(node["node_id"], 0) == 0
        ),
        "orphanChunks": sum(1 for chunk in chunks if not chunk["node_id"]),
        "shortChunks": sum(
            1 for chunk in chunks if len(chunk["content"] or "") < 50
        ),
        "unplacedNodes": sum(1 for node in nodes if node["page_start"] is None),
        "coveredPages": sum(
            1 for row in coverage if row["chunks"] and not row["nonContent"]
        ),
        "contentPages": max(0, page_count - len(non_content)),
        "nonContentPages": len(non_content),
        "missingPages": max(
            0,
            max(0, page_count - len(non_content))
            - sum(1 for row in coverage if row["chunks"] and not row["nonContent"]),
        ),
        "errors": sum(1 for issue in issues if issue.severity == "error"),
        "warnings": sum(1 for issue in issues if issue.severity == "warning"),
    }

    return {
        "document": doc,
        "nodes": nodes,
        "chunks": chunks,
        "issues": [
            {"severity": issue.severity, "message": issue.message, "nodeId": issue.node_id}
            for issue in issues
        ],
        "coverage": coverage,
        "stats": stats,
        "provenance": _document_provenance(db_path, doc_id),
        "exportKind": export_kind(doc, data_dir or db_path.parent),
    }


def _document_provenance(db_path: Path, doc_id: str) -> dict:
    """Current provenance + run history for one document."""
    try:
        with closing(_connect(db_path)) as connection:
            rows = connection.execute(
                "SELECT * FROM ingest_runs WHERE doc_id = ? ORDER BY id DESC LIMIT 20",
                (doc_id,),
            ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    runs = [run_out(SimpleNamespace(**dict(row))) for row in rows]
    return {"current": runs[0] if runs else None, "history": runs}


def _non_content_pages(
    db_path: Path, doc_id: str, data_dir: Path | None, content_start: int
) -> set[int]:
    return issues_mod.non_content_pages(db_path, doc_id, data_dir, content_start)


def _page_payload(
    db_path: Path, doc_id: str, page: int, data_dir: Path | None
) -> dict | None:
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


# ── FastAPI app ───────────────────────────────────────────────────────────


class CollectionIn(BaseModel):
    """Body for POST /api/collections."""

    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)

    @field_validator("name")
    @classmethod
    def _name_not_reserved(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("collection name is required")
        if clean.casefold() == "unassigned":
            raise ValueError('"unassigned" is reserved; pick another name')
        return clean


class CollectionPatch(BaseModel):
    """Body for PATCH /api/collections/{id}; omitted fields are untouched."""

    name: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=500)

    @field_validator("name")
    @classmethod
    def _name_not_reserved(cls, value: str | None) -> str | None:
        if value is None:
            return None
        clean = value.strip()
        if not clean:
            raise ValueError("collection name is required")
        if clean.casefold() == "unassigned":
            raise ValueError('"unassigned" is reserved; pick another name')
        return clean


class AssignIn(BaseModel):
    """Body for PUT /api/documents/collection. collectionId null = unfiled.

    The 500-document cap is enforced in the endpoint, not here: a schema
    violation would surface as 422, and the spec pins "too many" to 400.
    """

    docIds: list[str] = Field(default_factory=list)
    collectionId: str | None = None


class RestoreBaselineIn(BaseModel):
    """Body for POST /api/documents/{id}/restore-baseline (ADR-0002 §4.3).

    ``baseline_id`` null means the document's newest baseline. ``confirm``
    must be true: restoring discards every curator change to the document.
    ``force`` overrides a content-hash mismatch, i.e. restoring a baseline
    that describes an older version of the file.
    """

    baseline_id: int | None = None
    confirm: bool = False
    force: bool = False


class NoteIn(BaseModel):
    """Body for POST /api/notes. Defined at module level so FastAPI can
    resolve the string annotation under `from __future__ import annotations`."""

    author: str = Field(min_length=1, max_length=80)
    kind: str = Field(default="note", pattern="^(note|pitfall|handoff)$")
    body: str = Field(min_length=1, max_length=20000)
    doc_id: str | None = None
    node_id: str | None = None
    chunk_id: int | None = None
    task_id: str | None = Field(default=None, max_length=36)
    issue_id: str | None = Field(default=None, max_length=120)
    # Browser-side grouping hint (one tab session). Unlike the MCP path,
    # where the server assigns this id, a web client supplies its own —
    # it groups notes from one sitting, it is not proof of identity.
    session_id: str | None = Field(default=None, max_length=64)
    reply_to: int | None = None


class IngestNoteIn(BaseModel):
    """Body for PUT /api/documents/{id}/ingest-note.

    An empty body clears the note. Humans own this note: agents can read it
    over MCP, but no agent-facing tool creates or edits it.
    """

    body: str = Field(default="", max_length=20000)


class EndpointIn(BaseModel):
    """Body for POST/PUT /api/endpoints. api_key empty means "keep the
    stored one" on PUT; the plaintext is stored, never echoed back.
    reasoning_effort empty means "omit the field" (vendor default)."""

    name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str = Field(default="", max_length=500)
    model: str = Field(min_length=1, max_length=120)
    reasoning_effort: str = Field(default="", max_length=20)
    enabled: bool = True


class OcrEndpointIn(BaseModel):
    """Body for POST/PUT /api/ocr/endpoints. api_key empty means "keep the
    stored one" on PUT; the plaintext is stored, never echoed back."""

    name: str = Field(min_length=1, max_length=120)
    provider: str = Field(pattern="^(paddle|mistral|mineru|azure)$")
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str = Field(default="", max_length=500)
    model: str = Field(default="", max_length=120)
    enabled: bool = True

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        """Reject malformed or credential-bearing URLs at write time.

        The outbound guard (``app.ocr.netguard``) still runs per request —
        this only stops obviously wrong input from being stored, where it
        would otherwise fail on the next ingest instead of in the UI.
        """
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("base_url must be an http(s) URL")
        if not (parsed.hostname or "").strip():
            raise ValueError("base_url must include a host")
        if parsed.username or parsed.password:
            raise ValueError("base_url must not embed credentials")
        return value


class BenchIn(BaseModel):
    """Body for POST /api/bench: documents × OCR choices × endpoints.

    ``ocr_endpoint_ids`` accepts real ``ocr_endpoints`` ids plus the
    pseudo-choices ``cached`` (parse output on disk) and ``text`` (PDF text
    layer). Omitted means ``["cached"]``.
    """

    doc_ids: list[str] = Field(min_length=1)
    endpoint_ids: list[str] = Field(min_length=1)
    ocr_endpoint_ids: list[str] = Field(default_factory=list)


class RunIn(BaseModel):
    """Body for POST /api/runs (the agent console).

    One question is one run. ``priorRunId`` starts a follow-up: the previous
    answer and its citations are injected into the new run's prompt, but the
    trace stays separate (single-run model).
    """

    question: str = Field(min_length=1, max_length=2000)
    allowNotes: bool = True
    priorRunId: str | None = Field(default=None, max_length=36)
    maxCalls: int = Field(default=9999, ge=1, le=10000)
    # Summed token ceiling for the run; 0 = the server default (2,000,000).
    maxTokens: int = Field(default=0, ge=0, le=4_000_000)

    @field_validator("question")
    @classmethod
    def _question_not_blank(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("question is required")
        return clean


def _remove_document_files(data_dir: Path, doc_id: str, file_path: str | None) -> list[str]:
    """Delete a document's files, never anything outside ``data_dir``.

    ``file_path`` is user-visible data, not a trusted path: rows written by
    tests or old imports can point at a bare filename. Anything that does
    not resolve under the data directory is left alone, so deleting a
    library row can never reach the user's Documents folder.
    """
    root = Path(data_dir).resolve()
    removed: list[str] = []

    def inside(path: Path) -> bool:
        try:
            path.resolve().relative_to(root)
        except (OSError, ValueError):
            return False
        return True

    original = Path(file_path) if file_path else None
    if original is not None and inside(original) and original.is_file():
        original.unlink(missing_ok=True)
        removed.append(str(original))
        upload_dir = original.parent
        # data/uploads/<upload_id>/<name>: the upload folder holds only this
        # file. Leave any directory not shaped exactly like that alone.
        if upload_dir.parent.name == "uploads" and inside(upload_dir):
            shutil.rmtree(upload_dir, ignore_errors=True)
            removed.append(str(upload_dir))

    for path in (
        root / "ocr" / doc_id,
        root / "resources" / doc_id,
        root / "llm" / doc_id,
        root / "converted" / f"{doc_id}.pdf",
    ):
        if not inside(path) or not path.exists():
            continue
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        removed.append(str(path))
    return removed


def create_app(db_path: Path, data_dir: Path | None = None, web_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="Toc Memex API", docs_url=None, redoc_url=None)
    app.state.db_path = db_path
    app.state.data_dir = data_dir or db_path.parent
    app.state.web_dir = web_dir
    app.state.search_engine = None
    # Upload progress lives in memory: a browser refresh loses the ids but
    # never the work — ingest keeps running and the document appears in the
    # library when it finishes.
    app.state.uploads = {}
    # Strong references to running ingest tasks. Starlette's BackgroundTasks
    # would be awaited on the same keep-alive connection: the next file in a
    # multi-file upload would not start until the previous document finished
    # parsing. A detached task avoids that.
    app.state.upload_tasks = set()
    # Same reason as uploads: the run loop is a detached task, and dropping
    # the reference would let Python garbage-collect it mid-run.
    app.state.run_tasks = set()

    def engine():
        if app.state.search_engine is None:
            app.state.search_engine = make_engine(db_path)
        return app.state.search_engine

    async def ensure_schema():
        """Create any tables/columns the running database is missing."""
        from .db import ensure_schema

        await ensure_schema(engine())

    @app.on_event("startup")
    async def _startup() -> None:
        await ensure_schema()
        from .ingest.bench import fail_stale_processing_scenarios

        await fail_stale_processing_scenarios(session_factory(engine()))

    @app.get("/api/namespaces")
    def api_namespaces() -> dict:
        with closing(_connect(db_path)) as connection:
            rows = connection.execute(
                """
                SELECT n.id, n.kind, COUNT(d.id) AS doc_count
                FROM namespaces n
                LEFT JOIN documents d ON d.namespace_id = n.id
                GROUP BY n.id, n.kind
                ORDER BY doc_count DESC
                """
            ).fetchall()
        return {
            "namespaces": [
                {"id": row["id"], "kind": row["kind"], "count": row["doc_count"]}
                for row in rows
            ]
        }

    # ── collections (topic areas) ──
    def _collection_out(row, doc_count: int = 0) -> dict:
        return {
            "id": row.id,
            "name": row.name,
            "description": row.description or "",
            "sortOrder": row.sort_order,
            "docCount": doc_count,
        }

    @app.get("/api/collections")
    def api_collections() -> dict:
        with closing(_connect(db_path)) as connection:
            rows = connection.execute(
                """
                SELECT c.id, c.name, c.description, c.sort_order,
                       (SELECT COUNT(*) FROM documents d
                        WHERE d.collection_id = c.id) AS doc_count
                FROM collections c
                ORDER BY c.sort_order, c.name
                """
            ).fetchall()
            unassigned = connection.execute(
                "SELECT COUNT(*) FROM documents WHERE collection_id IS NULL"
            ).fetchone()[0]
        return {
            "collections": [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "description": row["description"] or "",
                    "sortOrder": row["sort_order"],
                    "docCount": row["doc_count"],
                }
                for row in rows
            ],
            "unassigned": unassigned,
        }

    @app.post("/api/collections")
    async def api_collection_create(body: CollectionIn) -> dict:
        from .index import collections as collections_mod

        async with session_factory(engine())() as session:
            try:
                row = await collections_mod.create_collection(
                    session, body.name, body.description
                )
            except ValueError as exc:
                await session.rollback()
                if "already exists" in str(exc):
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            await session.commit()
            return {"collection": _collection_out(row)}

    @app.patch("/api/collections/{collection_id}")
    async def api_collection_update(
        collection_id: str, body: CollectionPatch
    ) -> dict:
        from .index import collections as collections_mod

        async with session_factory(engine())() as session:
            try:
                row = await collections_mod.update_collection(
                    session, collection_id, name=body.name, description=body.description
                )
            except ValueError as exc:
                await session.rollback()
                if "already exists" in str(exc):
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            await session.commit()
            count = sum(
                1
                for doc in (
                    await session.execute(
                        sqlalchemy.select(Document.id).where(
                            Document.collection_id == collection_id
                        )
                    )
                ).scalars()
            )
            return {"collection": _collection_out(row, count)}

    @app.delete("/api/collections/{collection_id}")
    async def api_collection_delete(collection_id: str) -> dict:
        from .index import collections as collections_mod

        async with session_factory(engine())() as session:
            try:
                await collections_mod.delete_collection(session, collection_id)
            except ValueError as exc:
                await session.rollback()
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            await session.commit()
            unassigned = (
                await session.execute(
                    sqlalchemy.select(sqlalchemy.func.count())
                    .select_from(Document)
                    .where(Document.collection_id.is_(None))
                )
            ).scalar() or 0
        return {"deleted": collection_id, "unassigned": unassigned}

    @app.put("/api/documents/collection")
    async def api_documents_assign(body: AssignIn) -> dict:
        from .index import collections as collections_mod

        if len(body.docIds) > 500:
            raise HTTPException(
                status_code=400, detail="too many documents in one request (max 500)"
            )
        async with session_factory(engine())() as session:
            try:
                updated, missing = await collections_mod.assign_documents(
                    session, body.docIds, body.collectionId
                )
            except ValueError as exc:
                await session.rollback()
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            await session.commit()
        return {"updated": updated, "missing": missing}

    # ── uploads ────────────────────────────────────────────────────────
    # Ingest is the one write path that can take minutes (OCR), so the POST
    # only streams the file to disk and hands it to a background task. The
    # returned upload id is polled for status; the document itself appears
    # in the normal library list once ingest commits it.
    async def _run_upload(
        upload_id: str,
        path: Path,
        collection_id: str | None,
        image_mode: str = "structured",
        ingest_note: str = "",
    ) -> None:
        from .index import activity as activity_mod
        from .index import collections as collections_mod

        record = app.state.uploads[upload_id]
        note_text = (ingest_note or "").strip()
        # Ingest runs in a child process for the same reason the CLI exists:
        # parsing a 300-page PDF is CPU-bound and would freeze the event loop
        # (and this status endpoint) for minutes. The child inherits the
        # database and data dir this server was started with, not whatever
        # the shell's .env says.
        project_root = Path(__file__).resolve().parent.parent
        upload_env = os.environ.copy()
        upload_env["TOCMEMEX_DB_PATH"] = str(Path(db_path).resolve())
        upload_env["TOCMEMEX_DATA_DIR"] = str(Path(app.state.data_dir).resolve())
        # The child writes its traceback to stderr. Without this, a Chinese
        # Windows console encodes it as GBK while the parent decodes UTF-8,
        # turning the vendor's error message into replacement characters —
        # exactly what made "OCR识别任务失败" unreadable in the UI.
        upload_env["PYTHONIOENCODING"] = "utf-8"
        try:
            active_engine = engine()
            Session = session_factory(active_engine)
            digest = await asyncio.to_thread(_stream_digest, path)

            async with Session() as session:
                existing = (
                    await session.execute(
                        sqlalchemy.select(Document).where(
                            Document.namespace_id == "shared",
                            Document.content_hash == digest,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None and existing.status != "failed":
                    record["docId"] = existing.id
                    record["status"] = "duplicate"
                    record["message"] = "库中已有相同文件，未重复入库"
                    if collection_id and existing.collection_id is None:
                        try:
                            await collections_mod.assign_documents(
                                session, [existing.id], collection_id
                            )
                            await session.commit()
                            record["message"] += "；已归入所选分区"
                        except ValueError as exc:
                            await session.rollback()
                            record["message"] += f"；分区不存在：{exc}"
                    if note_text:
                        # A re-upload with a note updates the existing
                        # document's 入库备注 rather than adding another.
                        await activity_mod.upsert_ingest_note(
                            session, existing.id, note_text
                        )
                        await session.commit()
                        record["message"] += "；已更新入库备注"
                    shutil.rmtree(path.parent, ignore_errors=True)
                    return

            record["status"] = "indexing"
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "app.cli",
                "ingest",
                str(path),
                "--image-mode",
                image_mode,
                cwd=str(project_root),
                env=upload_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            if process.returncode != 0:
                detail = (stderr or stdout).decode("utf-8", "replace").strip()
                logger.warning("ingest failed for %s: %s", path, detail[-1200:])
                raise RuntimeError(_friendly_ingest_error(detail))
            printed = [
                line.strip()
                for line in stdout.decode("utf-8", "replace").splitlines()
                if line.strip()
            ]
            if not printed:
                raise RuntimeError("ingest finished without a document id")
            doc_id = printed[-1]

            if collection_id:
                async with Session() as session:
                    try:
                        await collections_mod.assign_documents(
                            session, [doc_id], collection_id
                        )
                        await session.commit()
                    except ValueError as exc:
                        await session.rollback()
                        record["message"] = f"已入库，但分区不存在：{exc}"
            if note_text:
                async with Session() as session:
                    await activity_mod.upsert_ingest_note(session, doc_id, note_text)
                    await session.commit()
            record["docId"] = doc_id
            record["status"] = "ready"
        except Exception as exc:
            logger.exception("upload %s failed", upload_id)
            record["status"] = "failed"
            record["error"] = str(exc) or exc.__class__.__name__
            # Ingest may have written the document row before the failure.
            # Leaving it "indexing" forever would make the library claim a
            # job is still running when nothing is.
            try:
                digest = await asyncio.to_thread(_stream_digest, path)
                Session = session_factory(engine())
                async with Session() as session:
                    row = (
                        await session.execute(
                            sqlalchemy.select(Document).where(
                                Document.namespace_id == "shared",
                                Document.content_hash == digest,
                            )
                        )
                    ).scalar_one_or_none()
                    if row is not None and row.status != "ready":
                        row.status = "failed"
                        row.error = (str(exc) or exc.__class__.__name__)[:1000]
                        await session.commit()
            except Exception:
                logger.exception("could not mark upload %s failed", upload_id)

    @app.post("/api/uploads")
    async def api_upload(
        file: UploadFile = File(...),
        collection_id: str | None = Form(default=None, alias="collectionId"),
        image_mode: str = Form(default="structured", alias="imageMode"),
        ingest_note: str = Form(default="", alias="ingestNote"),
    ) -> dict:
        from .ingest.pipeline import infer_doc_type, normalize_image_mode

        name = (
            (file.filename or "")
            .replace("\\", "/")
            .rsplit("/", 1)[-1]
            .strip()
            .replace("\x00", "")
            .replace(":", "_")
        )
        if not name or name in {".", ".."}:
            raise HTTPException(status_code=400, detail="文件名无效")
        if infer_doc_type(Path(name)) is None:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件类型：{Path(name).suffix or '（无扩展名）'}",
            )
        if Path(name).suffix.lower() in {".xls", ".xlsx", ".csv", ".tsv"}:
            raise HTTPException(
                status_code=400,
                detail="Excel / CSV 上传暂未开放，请先导出为 PDF 或文本后再上传",
            )
        if len(ingest_note) > 20000:
            raise HTTPException(status_code=400, detail="入库备注过长（上限 20000 字）")

        upload_id = new_uuid()
        upload_dir = app.state.data_dir / "uploads" / upload_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        target = upload_dir / name
        size = 0
        try:
            with target.open("wb") as handle:
                while True:
                    piece = await file.read(1024 * 1024)
                    if not piece:
                        break
                    size += len(piece)
                    if size > _MAX_UPLOAD_BYTES:
                        raise HTTPException(
                            status_code=413, detail="文件超过 200 MB 上限"
                        )
                    handle.write(piece)
        except HTTPException:
            shutil.rmtree(upload_dir, ignore_errors=True)
            raise
        except Exception:
            shutil.rmtree(upload_dir, ignore_errors=True)
            raise
        finally:
            await file.close()
        if size == 0:
            shutil.rmtree(upload_dir, ignore_errors=True)
            raise HTTPException(status_code=400, detail="文件为空")

        try:
            _validate_upload_content(target, name)
        except HTTPException:
            shutil.rmtree(upload_dir, ignore_errors=True)
            raise

        record = {
            "id": upload_id,
            "filename": name,
            "size": size,
            "status": "queued",
            "collectionId": collection_id or None,
            "imageMode": normalize_image_mode(image_mode)
            if infer_doc_type(Path(name)) == "image"
            else "structured",
            "ingestNote": ingest_note.strip(),
            "docId": None,
            "error": None,
            "message": "",
            "createdAt": utcnow_iso(),
        }
        app.state.uploads[upload_id] = record
        task = asyncio.create_task(
            _run_upload(
                upload_id,
                target,
                collection_id,
                record["imageMode"],
                record["ingestNote"],
            )
        )
        app.state.upload_tasks.add(task)
        task.add_done_callback(app.state.upload_tasks.discard)
        return {"upload": record}

    @app.get("/api/uploads/{upload_id}")
    def api_upload_status(upload_id: str) -> dict:
        record = app.state.uploads.get(upload_id)
        if record is None:
            raise HTTPException(status_code=404, detail="upload not found")
        payload = dict(record)
        if record.get("docId"):
            try:
                row = _doc_row(db_path, record["docId"])
                payload["document"] = {
                    "id": row["id"],
                    "title": row["title"],
                    "status": row["status"],
                    "error": row["error"],
                    "collectionId": row["collection_id"],
                }
            except HTTPException:
                pass
        return payload

    @app.get("/api/overview")
    def api_overview() -> dict:
        documents = _doc_summaries(db_path, app.state.data_dir)
        ready = sum(1 for doc in documents if doc["status"] == "ready")
        failed = sum(1 for doc in documents if doc["status"] == "failed")
        pending = sum(
            1 for doc in documents if doc["status"] not in ("ready", "failed")
        )
        alerts = _quality_groups(db_path, app.state.data_dir)
        note_counts = _note_counts_by_issue(db_path)
        for alert in alerts:
            alert["noteCount"] = note_counts.get(alert["id"], 0)
        return {
            "stats": {
                "docs": len(documents),
                "ready": ready,
                "pending": pending,
                "failed": failed,
                "pages": sum(doc["pages"] for doc in documents),
                "contentPages": sum(doc["contentPages"] for doc in documents),
                "chunks": sum(doc["chunks"] for doc in documents),
                "errors": sum(doc["errors"] for doc in documents),
                "warnings": sum(doc["warnings"] for doc in documents),
                "issues": sum(alert["count"] for alert in alerts),
                "issueGroups": len(alerts),
                "tracedIssues": sum(1 for alert in alerts if alert["noteCount"]),
            },
            "alerts": alerts[:20],
        }

    @app.get("/api/documents")
    def api_documents() -> dict:
        return {"documents": _doc_summaries(db_path, app.state.data_dir)}

    @app.get("/api/documents/search")
    async def api_document_search(
        q: str = Query(min_length=1),
    ) -> dict:
        """Document ids matching ``q``, most relevant first.

        The library's 「筛选文档」 box calls this instead of filtering in the
        browser: matching keywords and TOC headings needs the index, and its
        ranking separates the document actually about a topic from its
        siblings whose shared preamble merely lists it.
        """
        async with session_factory(engine())() as session:
            ids = await ranked_document_ids(session, q)
        return {"ids": ids}

    @app.get("/api/documents/{doc_id}")
    def api_document(doc_id: str) -> dict:
        payload = _document_detail(db_path, doc_id, app.state.data_dir)
        if payload is None:
            raise HTTPException(status_code=404, detail="document not found")
        return payload

    @app.delete("/api/documents/{doc_id}")
    async def api_document_delete(doc_id: str) -> dict:
        """Remove one document: index rows, notes, history and its files.

        Foreign-key cascades already cover the derived rows; the FTS tables
        and the two by-document history tables have no FK, so they are
        cleared here. The files are removed after the commit: a file that
        cannot be deleted must not leave the row stranded in the library.
        """
        file_path: str | None = None
        async with session_factory(engine())() as session:
            doc = await session.get(Document, doc_id)
            if doc is None:
                raise HTTPException(status_code=404, detail="document not found")
            file_path = doc.file_path
            for statement in (
                "DELETE FROM chunk_fts WHERE doc_id = :doc_id",
                "DELETE FROM doc_fts WHERE doc_id = :doc_id",
                "DELETE FROM usage_log WHERE doc_id = :doc_id",
                "DELETE FROM curation_outcomes WHERE doc_id = :doc_id",
            ):
                await session.execute(sqlalchemy.text(statement), {"doc_id": doc_id})
            await session.delete(doc)
            await session.commit()

        removed = _remove_document_files(app.state.data_dir, doc_id, file_path)
        # The upload record is only a progress row; dropping it keeps the
        # queue from offering a retry for a document that no longer exists.
        for upload_id, record in list(app.state.uploads.items()):
            if record.get("docId") == doc_id:
                app.state.uploads.pop(upload_id, None)
        _invalidate_cache()
        return {"deleted": doc_id, "files": removed}

    @app.get("/api/documents/{doc_id}/provenance")
    def api_document_provenance(doc_id: str) -> dict:
        return _document_provenance(db_path, doc_id)

    # ── baselines (ADR-0002 §4) ──
    @app.get("/api/documents/{doc_id}/baselines")
    async def api_document_baselines(doc_id: str) -> dict:
        """Every baseline for one document, newest first, plus whether the
        newest still matches the document's current content hash."""
        from .curation import baselines as baselines_mod

        async with session_factory(engine())() as session:
            from sqlalchemy import select

            doc = await session.get(Document, doc_id)
            if doc is None:
                raise HTTPException(status_code=404, detail="document not found")
            rows = (
                (
                    await session.execute(
                        select(baselines_mod.DocBaseline)
                        .where(baselines_mod.DocBaseline.doc_id == doc_id)
                        .order_by(baselines_mod.DocBaseline.id.desc())
                    )
                )
                .scalars()
                .all()
            )
            items = [await baselines_mod.baseline_out(session, row, doc) for row in rows]
        return {"doc_id": doc_id, "baselines": items}

    @app.post("/api/documents/{doc_id}/restore-baseline")
    async def api_restore_baseline(doc_id: str, body: RestoreBaselineIn) -> dict:
        """Replace the document's derived rows with a baseline snapshot.

        The current state is snapshotted first, so a restore is itself
        undoable; a ``curation_outcomes`` row records who restored and to
        which baseline.
        """
        from .curation import baselines as baselines_mod

        if not body.confirm:
            raise HTTPException(
                status_code=400,
                detail="confirm must be true: restoring discards curator changes",
            )
        async with session_factory(engine())() as session:
            try:
                result = await baselines_mod.restore_baseline(
                    session, doc_id, body.baseline_id, force=body.force
                )
            except LookupError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ValueError as exc:
                await session.rollback()
                message = str(exc)
                if message.startswith("content hash mismatch"):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "message": message,
                            "code": "hash_mismatch",
                            "can_force": True,
                        },
                    ) from exc
                raise HTTPException(status_code=409, detail=message) from exc
            await _record_restore_outcome(session, doc_id, result, "human:web")
            await session.commit()
        _invalidate_cache()
        return result

    @app.get("/api/documents/{doc_id}/chunks")
    def api_document_chunks(doc_id: str) -> dict:
        """Chunk list without bodies — enough to pick a note anchor."""
        with closing(_connect(db_path)) as connection:
            if connection.execute(
                "SELECT 1 FROM documents WHERE id = ?", (doc_id,)
            ).fetchone() is None:
                raise HTTPException(status_code=404, detail="document not found")
            rows = connection.execute(
                """
                SELECT id, node_id, chunk_index, heading_path, page_start, page_end
                FROM chunks WHERE doc_id = ? ORDER BY chunk_index
                """,
                (doc_id,),
            ).fetchall()
        return {
            "chunks": [
                {
                    "id": row["id"],
                    "index": row["chunk_index"],
                    "nodeId": row["node_id"],
                    "path": row["heading_path"] or "",
                    "page": (row["page_start"] + 1) if row["page_start"] is not None else None,
                    "pageEnd": (row["page_end"] + 1) if row["page_end"] is not None else None,
                }
                for row in rows
            ]
        }

    @app.get("/api/documents/{doc_id}/page/{page}")
    def api_page(doc_id: str, page: int) -> dict:
        payload = _page_payload(db_path, doc_id, page, app.state.data_dir)
        if payload is None:
            raise HTTPException(status_code=404, detail="page not available")
        return payload

    @app.get("/api/documents/{doc_id}/file")
    def api_document_file(doc_id: str) -> Response:
        """The original file, for the document header's 打开原文 button.

        Inline, so a browser with a PDF/DOCX viewer shows it instead of
        saving it. Only the path stored at ingest time is served — the
        route never accepts a caller-supplied path.
        """
        with closing(_connect(db_path)) as connection:
            row = connection.execute(
                "SELECT file_path, filename, doc_type FROM documents WHERE id = ?",
                (doc_id,),
            ).fetchone()
        if row is None or not row["file_path"]:
            raise HTTPException(status_code=404, detail="original file not recorded")
        path = Path(row["file_path"])
        if not path.is_file():
            raise HTTPException(status_code=404, detail="original file is missing")
        return FileResponse(
            path,
            media_type=_MEDIA_TYPES.get(
                path.suffix.lower(), "application/octet-stream"
            ),
            filename=row["filename"] or path.name,
            content_disposition_type="inline",
        )

    @app.get("/api/documents/{doc_id}/export")
    def api_document_export(doc_id: str) -> Response:
        """ZIP of the parse output this document was built from.

        OCR documents get the vendor ``raw.jsonl`` and, when the provider
        wrote one, the normalized ``pages.json``; documents parsed from
        their text layer get the original file plus pages re-parsed at
        export time. ``manifest.json`` names every file and the channel
        that produced it, so the archive never has to be guessed at later.
        """
        with closing(_connect(db_path)) as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE id = ?", (doc_id,)
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="document not found")
        doc = dict(row)
        run = _latest_runs(db_path).get(doc_id)
        try:
            bundle = build_export(doc, app.state.data_dir, run)
        except ExportError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # RFC 5987: Chinese filenames need the UTF-8 form; the ASCII fallback
        # keeps older clients from showing a mangled name.
        disposition = (
            f"attachment; filename=\"{bundle.filename.encode('ascii', 'replace').decode()}\"; "
            f"filename*=UTF-8''{quote(bundle.filename)}"
        )
        return Response(
            content=bundle.content,
            media_type="application/zip",
            headers={
                "Content-Disposition": disposition,
                "X-Export-Kind": bundle.kind,
            },
        )

    @app.get("/api/documents/{doc_id}/resources/{resource_id}")
    def api_resource_json(doc_id: str, resource_id: str) -> dict:
        with closing(_connect(db_path)) as connection:
            row = connection.execute(
                "SELECT * FROM resources WHERE doc_id = ? AND resource_id = ?",
                (doc_id, resource_id),
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="resource not found")
        resource = dict(row)
        return {
            "resource": {
                "id": resource["resource_id"],
                "type": resource["type"],
                "title": resource["title"],
                "page": resource["page_idx"],
                "content": resource["content"],
                "image": (
                    f"/api/documents/{doc_id}/resources/{resource_id}/image"
                    if resource["image_path"]
                    else None
                ),
            }
        }

    @app.get("/api/documents/{doc_id}/resources/{resource_id}/image")
    def api_resource_image(doc_id: str, resource_id: str) -> Response:
        with closing(_connect(db_path)) as connection:
            row = connection.execute(
                "SELECT image_path FROM resources WHERE doc_id = ? AND resource_id = ?",
                (doc_id, resource_id),
            ).fetchone()
        if row is None or not row["image_path"]:
            raise HTTPException(status_code=404, detail="image not available")
        path = Path(row["image_path"])
        if not path.exists():
            raise HTTPException(status_code=404, detail="image not available")
        return FileResponse(path, media_type=_MEDIA_TYPES["image"])

    @app.get("/api/search")
    async def api_search(
        q: str = Query(min_length=1),
        top: int = Query(default=20, le=100),
        collection: str = "",
    ) -> dict:
        from .index.collections import CollectionNotFound
        from .index.fts import scoped_search

        async with session_factory(engine())() as session:
            try:
                payload = await scoped_search(
                    session, q, top_k=top, collection=collection
                )
            except CollectionNotFound as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            # Retrieval usage is the signal the curator's heat score reads.
            # Without this every usage_log row came from MCP and a library
            # read through the Web had heat 0 forever.
            if payload["results"]:
                from .index.activity import usage_log_write

                try:
                    await usage_log_write(
                        session, q, "web", payload["results"], actor_agent="human:web"
                    )
                    await session.commit()
                except Exception:
                    await session.rollback()  # logging must never break search
        hits = payload["results"]
        with closing(_connect(db_path)) as connection:
            docs = {
                row["id"]: dict(row)
                for row in connection.execute(
                    "SELECT id, title, namespace_id FROM documents"
                ).fetchall()
            }
        results = []
        for hit in hits:
            doc = docs.get(hit["doc_id"], {})
            is_doc_hit = hit.get("match_type") == "doc"
            results.append(
                {
                    "docId": hit["doc_id"],
                    "doc": doc.get("title", ""),
                    "ns": doc.get("namespace_id", ""),
                    "nodeId": hit.get("node_id"),
                    "headingNum": hit.get("heading_num"),
                    "path": "" if is_doc_hit else (hit.get("heading_path") or hit.get("title") or ""),
                    "pageStart": None if is_doc_hit else hit.get("page_start"),
                    "pageEnd": None if is_doc_hit else hit.get("page_end"),
                    "score": float(hit.get("score") or 0.0),
                    "chunkId": hit.get("chunk_id"),
                    "snippet": hit.get("snippet") or "",
                    "matchType": hit.get("match_type"),
                    "collection": hit.get("collection"),
                }
            )
        return {
            "query": q,
            "results": results,
            "hint": payload["hint"],
            "scope": payload["scope"],
        }

    @app.get("/api/quality")
    def api_quality() -> dict:
        documents = _doc_summaries(db_path, app.state.data_dir)
        groups = _quality_groups(db_path, app.state.data_dir)
        note_counts = _note_counts_by_issue(db_path)
        for group in groups:
            group["noteCount"] = note_counts.get(group["id"], 0)
        checks: dict[str, list[dict]] = {}
        labels = [
            "无直接切片",
            "叶子无切片",
            "未归属切片",
            "短切片",
            "未定位节点",
            "缺失页",
        ]
        for doc in documents:
            stats = compute_doc_stats(db_path, _doc_row(db_path, doc["id"]), app.state.data_dir)
            values = [
                stats["nodesWithoutDirect"],
                stats["leafNodesWithoutChunks"],
                stats["orphanChunks"],
                stats["shortChunks"],
                stats["unplacedNodes"],
                stats["missingPages"],
            ]
            checks[doc["id"]] = [
                {
                    "label": label,
                    "value": value,
                    "tone": "" if value == 0 else ("danger" if label in ("缺失页", "叶子无切片") else "warn"),
                }
                for label, value in zip(labels, values)
            ]
        # One summary, so the quality page and the overview cannot drift
        # apart: `issues` counts every complaint (a doc with 13 missing
        # pages contributes 13), `groups` counts the complaint rows
        # themselves, and `tocWarnings` is the subset that came from TOC
        # structure validation (sibling overlap, duplicate numbers, …).
        # The page previously showed `sum(doc.warnings)` next to the group
        # totals, which are different things and never matched.
        by_kind: dict[str, int] = {}
        for group in groups:
            by_kind[group["kind"]] = by_kind.get(group["kind"], 0) + group["count"]
        summary = {
            "issues": sum(group["count"] for group in groups),
            "groups": len(groups),
            "byKind": by_kind,
            "tocWarnings": sum(doc["warnings"] for doc in documents),
            "tocErrors": sum(doc["errors"] for doc in documents),
            "traced": sum(1 for group in groups if group["noteCount"]),
        }
        return {"groups": groups, "checks": checks, "summary": summary}

    # ── LLM call log / usage (ADR-0002 §10.4) ──
    @app.get("/api/llm/calls")
    def api_llm_calls(
        doc_id: str | None = None,
        run_id: str | None = None,
        call_site: str | None = None,
        status: str | None = None,
        since: str | None = None,
        limit: int = Query(default=100, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        """Logged LLM calls, newest first, filterable by document or run.

        ``since`` is an ISO-8601 timestamp; records at or after it are
        returned. A malformed value is a 400 rather than silently ignored,
        so a typo in a dashboard URL does not look like "no calls".
        """
        from .curation import llm_log

        if since:
            try:
                datetime.fromisoformat(since)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"since must be an ISO-8601 timestamp: {since}",
                ) from exc
        data_dir = app.state.data_dir or db_path.parent
        filters = dict(
            doc_id=doc_id,
            run_id=run_id,
            call_site=call_site,
            status=status,
            since=since,
        )
        calls = llm_log.read_calls(data_dir, limit=limit, offset=offset, **filters)
        return {"calls": calls, "total": llm_log.count_calls(data_dir, **filters)}

    @app.get("/api/llm/usage")
    def api_llm_usage() -> dict:
        """Totals, per-day, per-run and per-endpoint token aggregates."""
        from .curation import llm_log

        data_dir = app.state.data_dir or db_path.parent
        return llm_log.usage_summary(data_dir)

    @app.get("/api/llm/calls/export")
    def api_llm_calls_export(
        format: str = "jsonl",
        doc_id: str | None = None,
        run_id: str | None = None,
        call_site: str | None = None,
        status: str | None = None,
        since: str | None = None,
    ) -> Response:
        """Download the whole (filtered) call log as JSONL or CSV.

        Same filters as /api/llm/calls but uncapped — an export that stops
        at a page size is not an export. JSONL keeps the records verbatim;
        CSV flattens to one row per call for spreadsheets.
        """
        from .curation import llm_log

        fmt = (format or "jsonl").lower()
        if fmt not in ("jsonl", "csv"):
            raise HTTPException(status_code=400, detail="format must be jsonl or csv")
        if since:
            try:
                datetime.fromisoformat(since)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"since must be an ISO-8601 timestamp: {since}",
                ) from exc
        data_dir = app.state.data_dir or db_path.parent
        body, media, filename = llm_log.export_calls(
            data_dir,
            doc_id=doc_id,
            run_id=run_id,
            call_site=call_site,
            status=status,
            since=since,
            format=fmt,
        )
        return Response(
            content=body,
            media_type=f"{media}; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/llm/calls/{call_id}/export")
    def api_llm_call_detail_export(call_id: str) -> Response:
        """One call's complete input and output as a JSON download.

        ``full`` log mode writes a per-call detail file with the verbatim
        prompt and response — this endpoint exports that. Calls logged in
        ``meta`` mode (or before the detail file existed) fall back to their
        JSONL record, which carries previews only.
        """
        from .curation import llm_log

        data_dir = app.state.data_dir or db_path.parent
        record, media, filename = llm_log.export_call_detail(data_dir, call_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"call not found: {call_id}")
        return Response(
            content=json.dumps(record, ensure_ascii=False, indent=2),
            media_type=f"{media}; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # ── curation proposals (ADR-0002 §13 P3) ──
    @app.get("/api/curation")
    async def api_curation(
        doc_id: str | None = None,
        verdict: str | None = "pending",
        limit: int = Query(default=100, le=500),
    ) -> dict:
        """Maintenance proposals, pending approval by default.

        Each row carries its ops and their per-op outcomes so the quality
        page can show *what* would change, not just that something would.
        """
        from .models import CurationJob, CurationProposal

        async with session_factory(engine())() as session:
            stmt = sqlalchemy.select(CurationProposal).order_by(
                CurationProposal.created_at.desc()
            )
            if verdict:
                stmt = stmt.where(CurationProposal.verdict == verdict)
            if doc_id:
                job_ids = (
                    (
                        await session.execute(
                            sqlalchemy.select(CurationJob.id).where(
                                CurationJob.doc_id == doc_id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                stmt = stmt.where(CurationProposal.job_id.in_(job_ids))
            rows = (await session.execute(stmt.limit(limit))).scalars().all()
            job_ids = {row.job_id for row in rows}
            jobs = {
                job.id: job
                for job in (
                    (
                        await session.execute(
                            sqlalchemy.select(CurationJob).where(
                                CurationJob.id.in_(job_ids)
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            } if job_ids else {}
            payload = [
                {
                    "id": row.id,
                    "jobId": row.job_id,
                    "docId": jobs[row.job_id].doc_id if row.job_id in jobs else None,
                    "kind": jobs[row.job_id].kind if row.job_id in jobs else None,
                    "strategy": row.strategy,
                    "verdict": row.verdict,
                    "rationale": row.rationale,
                    "ops": json.loads(row.payload_json or "{}").get("ops", []),
                    "results": json.loads(row.results_json or "[]"),
                    "appliedOps": row.applied_ops,
                    "droppedOps": row.dropped_ops,
                    "createdAt": row.created_at,
                }
                for row in rows
            ]
        return {"proposals": payload, "count": len(payload)}

    @app.post("/api/curation/{proposal_id}/approve")
    async def api_curation_approve(proposal_id: str) -> dict:
        """Approve the pending ops of one proposal.

        Ops are re-validated at approval time; anything that no longer
        passes is reported rather than applied blind.
        """
        from .curation.dispatch import Dispatcher
        from .models import CurationJob, CurationProposal

        async with session_factory(engine())() as session:
            proposal = await session.get(CurationProposal, proposal_id)
            if proposal is None:
                raise HTTPException(status_code=404, detail="proposal not found")
            job = await session.get(CurationJob, proposal.job_id)
            if job is None:
                raise HTTPException(status_code=404, detail="job not found")
            dispatcher = Dispatcher(
                session, app.state.data_dir, job.doc_id, job_id=job.id
            )
            try:
                result = await dispatcher.apply_proposal(proposal_id)
            except Exception as exc:
                await session.rollback()
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if job.status == "proposed":
                job.status = "applied"
            await session.commit()
        _invalidate_cache()
        return result

    @app.post("/api/curation/{proposal_id}/reject")
    async def api_curation_reject(proposal_id: str) -> dict:
        """Reject a pending proposal; nothing is applied."""
        from .curation.outcomes import record_outcome
        from .models import CurationJob, CurationProposal

        async with session_factory(engine())() as session:
            proposal = await session.get(CurationProposal, proposal_id)
            if proposal is None:
                raise HTTPException(status_code=404, detail="proposal not found")
            proposal.verdict = "rejected"
            job = await session.get(CurationJob, proposal.job_id)
            if job is not None:
                job.status = "rejected"
                await record_outcome(
                    session,
                    doc_id=job.doc_id,
                    issue_id=job.issue_id,
                    kind=job.kind,
                    strategy=proposal.strategy,
                    action="rejected",
                    actor="human:web",
                    note=f"proposal {proposal_id}",
                )
            await session.commit()
        return {"proposal_id": proposal_id, "verdict": "rejected"}

    @app.post("/api/curation/{proposal_id}/undo")
    async def api_curation_undo(proposal_id: str) -> dict:
        """Restore the state before one applied proposal.

        Uses the proposal's pre-write snapshot. Proposals written before
        snapshots existed fall back to the document's adoption baseline and
        say so (``mode: "baseline"``, ``stepwise: false``) instead of
        pretending it was a surgical undo.
        """
        from .curation.undo import undo_proposal

        async with session_factory(engine())() as session:
            try:
                result = await undo_proposal(
                    session, app.state.data_dir, proposal_id
                )
            except LookupError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ValueError as exc:
                await session.rollback()
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            await session.commit()
        _invalidate_cache()
        return result

    @app.get("/api/settings")
    async def api_settings() -> dict:
        """Effective ingest settings, so the UI shows what actually runs.

        The settings page used to hard-code "1200 / 100" as literal HTML;
        changing .env left the page unchanged. These are read from the same
        Settings object the ingest pipeline uses. The LLM log level is the
        Web-settings override when one exists, else ``.env``.
        """
        from .config import get_settings as _get_settings
        from .llm.purpose import load_global

        settings = _get_settings()
        async with session_factory(engine())() as session:
            global_cfg = await load_global(session)
        return {
            "chunk": {
                "maxChars": settings.tocmemex_max_chunk_chars,
                "overlap": settings.tocmemex_chunk_overlap,
            },
            "llm": {
                "configured": bool(
                    settings.tocmemex_llm_api_key
                    and settings.tocmemex_llm_base_url
                    and settings.tocmemex_llm_model
                ),
                "model": settings.tocmemex_llm_model or "",
                "reasoningEffort": settings.tocmemex_llm_reasoning_effort or "",
                "logMode": global_cfg.get("log_mode") or settings.tocmemex_llm_log,
            },
            "readOnly": True,
        }

    # ── agents (纲目 / 编修) ──
    @app.get("/api/agents")
    async def api_agents() -> dict:
        """Effective config for both agents plus the shipped defaults.

        The UI diffing against ``defaults`` is what powers "已修改" badges and
        "恢复默认"; prompt text therefore only ever lives in one place.
        """
        from .llm.purpose import AGENTS, defaults_for, load_config

        async with session_factory(engine())() as session:
            agents = {name: await load_config(session, name) for name in AGENTS}
        return {
            "agents": agents,
            "defaults": {name: defaults_for(name) for name in AGENTS},
        }

    @app.put("/api/agents/{name}")
    async def api_agents_update(name: str, body: dict) -> dict:
        """Persist one agent's settings; validation errors are 400 with a reason."""
        from .llm.purpose import AGENTS, save_config

        if name not in AGENTS:
            raise HTTPException(status_code=404, detail="unknown agent")
        try:
            async with session_factory(engine())() as session:
                cfg = await save_config(session, name, body)
                await session.commit()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"agent": cfg}

    @app.put("/api/settings/llm-log")
    async def api_llm_log_update(body: dict) -> dict:
        from .llm.purpose import save_log_mode

        try:
            async with session_factory(engine())() as session:
                mode = await save_log_mode(session, str(body.get("mode") or ""))
                await session.commit()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"logMode": mode}

    @app.get("/api/jobs")
    def api_jobs() -> dict:
        """The ingest queue: documents that are not ready yet.

        Rows now carry the live stage/percent/scheme written by ingest, so
        the page can show what is happening instead of a bare "解析中".
        Uploads that have not reached the database yet (the child process
        is still starting) are merged in from the in-memory upload table.
        """
        documents = _doc_summaries(db_path, app.state.data_dir)

        def upload_record_for(file_path: str | None) -> dict | None:
            """The in-memory upload row that produced this document.

            Uploads live under ``data/uploads/<upload_id>/<name>``; the
            record holds what the document does not (the requested
            collection, image mode and 入库备注), which is exactly what the
            「重新上传」 prefill needs. After a server restart it is gone and
            the document's own fields are the fallback.
            """
            if not file_path:
                return None
            parent = Path(file_path).parent
            if parent.parent.name != "uploads":
                return None
            return app.state.uploads.get(parent.name)

        jobs = []
        for doc in documents:
            if doc["status"] == "ready":
                continue
            failed = doc["status"] == "failed"
            record = upload_record_for(doc.get("filePath")) or {}
            plan = doc.get("plan") or ""
            jobs.append(
                {
                    "id": doc["id"],
                    "docId": doc["id"],
                    "name": doc["filename"],
                    "type": doc["type"],
                    "status": "failed" if failed else "parsing",
                    "stage": doc.get("stage") or ("解析失败" if failed else "解析中"),
                    "progress": doc.get("progress")
                    if doc.get("progress") is not None
                    else (100 if failed else 0),
                    "plan": doc.get("plan"),
                    "ns": doc["ns"],
                    "error": _display_ingest_error(doc["error"]),
                    "time": doc["updatedAt"],
                    "startedAt": doc["createdAt"],
                    # Retry prefill: the original file comes from
                    # /api/documents/{id}/file, these are the choices.
                    "canRetry": failed,
                    "collectionId": record.get("collectionId")
                    or doc.get("collectionId"),
                    "imageMode": record.get("imageMode")
                    or ("text" if "纯文本" in plan else "structured"),
                    "ingestNote": record.get("ingestNote")
                    or doc.get("ingestNote"),
                }
            )

        known_names = {job["name"] for job in jobs}
        for record in list(app.state.uploads.values()):
            # Only the pre-database window. Once the child starts, the
            # document row above is the single source of truth.
            if record.get("status") != "queued":
                continue
            if record.get("filename") in known_names:
                continue
            jobs.append(
                {
                    "id": record["id"],
                    "docId": None,
                    "name": record.get("filename") or "未命名",
                    "type": "上传",
                    "status": "parsing",
                    "stage": "排队等待解析",
                    "progress": 2,
                    "plan": "OCR 结构化"
                    if record.get("imageMode") != "text"
                    else "OCR 纯文本",
                    "ns": "shared",
                    "error": None,
                    "time": record.get("createdAt"),
                    "startedAt": record.get("createdAt"),
                    "canRetry": False,
                    "collectionId": record.get("collectionId"),
                    "imageMode": record.get("imageMode") or "structured",
                    "ingestNote": record.get("ingestNote") or "",
                }
            )
        jobs.sort(key=lambda job: job.get("time") or "", reverse=True)
        return {"jobs": jobs}

    @app.get("/api/tasks")
    def api_tasks(limit: int = Query(default=50, le=200)) -> dict:
        """Activity-layer tasks (cross-agent, cross-session work units).

        Deliberately separate from /api/jobs: jobs are ingest queue rows
        derived from document status, tasks are the units agents cooperate
        on. Conflating them made the overview's "recent tasks" panel empty
        whenever every document was ready.
        """
        with closing(_connect(db_path)) as connection:
            rows = connection.execute(
                """
                SELECT id, title, goal, status, owner_agent, created_at, updated_at
                FROM tasks ORDER BY updated_at DESC LIMIT ?
                """,
                (max(1, min(limit, 200)),),
            ).fetchall()
        tasks = [
            {
                "id": row["id"],
                "title": row["title"],
                "goal": row["goal"],
                "status": row["status"],
                "owner": row["owner_agent"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
            }
            for row in rows
        ]
        open_count = sum(1 for task in tasks if task["status"] in ("open", "doing", "blocked"))
        return {
            "tasks": tasks,
            "counts": {
                "open": open_count,
                "done": sum(1 for task in tasks if task["status"] == "done"),
                "archived": sum(1 for task in tasks if task["status"] == "archived"),
            },
        }

    @app.get("/api/tasks/{task_id}")
    async def api_task_detail(task_id: str) -> dict:
        """One task with its whole timeline.

        The task's value is not its title — it is the reads, cites, errata,
        reviews and hand-offs attached to it. Those live in two tables:
        ``activities`` (everything written through activity_write, including
        the auto-logged reads) and ``agent_notes`` (notes carrying task_id).
        ``index.activity.task_timeline`` is the one merge point; MCP uses it
        too, so the two surfaces cannot drift.
        """
        from .index.activity import task_timeline

        async with session_factory(engine())() as session:
            payload = await task_timeline(session, task_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="task not found")
        return payload

    # ── agent hand-off notes ──────────────────────────────────────

    async def _record_restore_outcome(session, doc_id: str, result: dict, actor: str) -> None:
        """Ledger row for a restore; a human rollback is an outcome too."""
        from .curation.outcomes import record_outcome

        await record_outcome(
            session,
            doc_id=doc_id,
            action="restored_forced" if result.get("forced") else "restored_by_human",
            actor=actor,
            strategy="baseline_restore",
            note=f"baseline {result.get('baseline_id')}",
        )

    def _note_out(row) -> dict:
        return {
            "id": row.id,
            "author": row.author,
            "kind": row.kind,
            "body": row.body,
            "docId": row.doc_id,
            "nodeId": row.node_id,
            "chunkId": row.chunk_id,
            "taskId": row.task_id,
            "issueId": row.issue_id,
            "sessionId": row.session_id,
            "replyTo": row.reply_to,
            "createdAt": row.created_at,
        }

    @app.post("/api/notes")
    async def api_note_add(note: NoteIn) -> dict:
        task_id = note.task_id
        if not task_id and note.reply_to is not None:
            # Replies inherit the parent's task, so an answer stays on the
            # task timeline even when the caller omits task_id.
            with closing(_connect(db_path)) as connection:
                parent = connection.execute(
                    "SELECT task_id FROM agent_notes WHERE id = ?",
                    (note.reply_to,),
                ).fetchone()
            if parent is not None and parent["task_id"]:
                task_id = parent["task_id"]
        if note.doc_id:
            with closing(_connect(db_path)) as connection:
                if connection.execute(
                    "SELECT 1 FROM documents WHERE id = ?", (note.doc_id,)
                ).fetchone() is None:
                    raise HTTPException(status_code=404, detail="document not found")
        if task_id:
            with closing(_connect(db_path)) as connection:
                if connection.execute(
                    "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
                ).fetchone() is None:
                    raise HTTPException(status_code=404, detail="task not found")
        async with session_factory(engine())() as session:
            row = AgentNote(
                author=note.author,
                kind=note.kind,
                body=note.body,
                doc_id=note.doc_id,
                node_id=note.node_id,
                chunk_id=note.chunk_id,
                task_id=task_id,
                issue_id=note.issue_id,
                session_id=note.session_id,
                reply_to=note.reply_to,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _note_out(row)

    @app.get("/api/notes")
    async def api_notes(
        doc_id: str | None = None,
        task_id: str | None = None,
        author: str | None = None,
        kind: str | None = None,
        issue_id: str | None = None,
        limit: int = Query(default=50, le=200),
    ) -> dict:
        async with session_factory(engine())() as session:
            from sqlalchemy import select

            stmt = select(AgentNote).order_by(AgentNote.id.desc()).limit(limit)
            if doc_id:
                stmt = stmt.where(AgentNote.doc_id == doc_id)
            if task_id:
                stmt = stmt.where(AgentNote.task_id == task_id)
            if author:
                stmt = stmt.where(AgentNote.author == author)
            if kind:
                stmt = stmt.where(AgentNote.kind == kind)
            if issue_id:
                stmt = stmt.where(AgentNote.issue_id == issue_id)
            rows = (await session.execute(stmt)).scalars().all()
        return {"notes": [_note_out(row) for row in rows]}

    @app.delete("/api/notes/{note_id}")
    async def api_note_delete(note_id: int) -> dict:
        """Delete one hand-off note, and the replies hanging off it.

        This is the human cleanup path for agent notes. 入库备注 is excluded:
        it is one row per document owned by whoever uploaded the file, and
        the only proper way to remove it is the ingest-note endpoint, which
        also clears what the library list shows.
        """
        from sqlalchemy import delete, select

        async with session_factory(engine())() as session:
            row = await session.get(AgentNote, note_id)
            if row is None:
                raise HTTPException(status_code=404, detail="note not found")
            if row.kind == "ingest":
                raise HTTPException(
                    status_code=400,
                    detail="入库备注由人类维护，请在知识库或手记页编辑/清空",
                )
            # Collect the reply tree before deleting: replies have no FK to
            # their parent, so leaving them would render orphan threads.
            ids = [note_id]
            frontier = [note_id]
            while frontier:
                children = (
                    await session.execute(
                        select(AgentNote.id).where(AgentNote.reply_to.in_(frontier))
                    )
                ).scalars().all()
                fresh = [child for child in children if child not in ids]
                ids.extend(fresh)
                frontier = fresh
            await session.execute(delete(AgentNote).where(AgentNote.id.in_(ids)))
            await session.commit()
        return {"deleted": len(ids)}

    @app.put("/api/documents/{doc_id}/ingest-note")
    async def api_set_ingest_note(doc_id: str, body: IngestNoteIn) -> dict:
        """Set or clear a document's 入库备注 (human-owned, one per document).

        Agents read this note through MCP (list_notes / get_document) and
        have no tool that writes it; this is the only edit path.
        """
        from .index import activity as activity_mod

        async with session_factory(engine())() as session:
            if await session.get(Document, doc_id) is None:
                raise HTTPException(status_code=404, detail="document not found")
            row = await activity_mod.upsert_ingest_note(session, doc_id, body.body)
            await session.commit()
            if row is not None:
                await session.refresh(row)
            return {"note": _note_out(row) if row is not None else None}

    # ── OCR endpoints ──
    def _ocr_endpoint_out(row: OcrEndpoint) -> dict:
        return ocr_endpoint_out(row)

    async def _ocr_endpoint_row(endpoint_id: str) -> OcrEndpoint:
        async with session_factory(engine())() as session:
            row = await session.get(OcrEndpoint, endpoint_id)
        if row is None:
            raise HTTPException(status_code=404, detail="OCR endpoint not found")
        return row

    @app.get("/api/ocr/endpoints")
    async def api_ocr_endpoints_list() -> dict:
        from .config import Settings as _Settings
        from .ocr.endpoints import active_ocr_endpoint

        async with session_factory(engine())() as session:
            rows = (
                (
                    await session.execute(
                        sqlalchemy.select(OcrEndpoint).order_by(OcrEndpoint.created_at)
                    )
                )
                .scalars()
                .all()
            )
            resolved = await active_ocr_endpoint(session, _Settings())
        return {
            "endpoints": [_ocr_endpoint_out(row) for row in rows],
            "active": {
                "name": resolved["name"],
                "provider": resolved["provider"],
                "source": "db" if resolved["name"] != "env" else "env",
            }
            if resolved
            else None,
        }

    @app.post("/api/ocr/endpoints")
    async def api_ocr_endpoints_create(body: OcrEndpointIn) -> dict:
        async with session_factory(engine())() as session:
            row = OcrEndpoint(
                name=body.name,
                provider=body.provider,
                base_url=body.base_url,
                api_key=body.api_key,
                model=body.model,
                enabled=int(body.enabled),
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return {"endpoint": _ocr_endpoint_out(row)}

    @app.put("/api/ocr/endpoints/{endpoint_id}")
    async def api_ocr_endpoints_update(endpoint_id: str, body: OcrEndpointIn) -> dict:
        async with session_factory(engine())() as session:
            row = await session.get(OcrEndpoint, endpoint_id)
            if row is None:
                raise HTTPException(status_code=404, detail="OCR endpoint not found")
            row.name = body.name
            row.provider = body.provider
            row.base_url = body.base_url
            row.model = body.model
            row.enabled = int(body.enabled)
            if body.api_key:  # empty keeps the stored key
                row.api_key = body.api_key
            await session.commit()
            await session.refresh(row)
            return {"endpoint": _ocr_endpoint_out(row)}

    @app.delete("/api/ocr/endpoints/{endpoint_id}")
    async def api_ocr_endpoints_delete(endpoint_id: str) -> dict:
        async with session_factory(engine())() as session:
            row = await session.get(OcrEndpoint, endpoint_id)
            if row is None:
                raise HTTPException(status_code=404, detail="OCR endpoint not found")
            await session.delete(row)
            await session.commit()
        return {"deleted": endpoint_id}

    @app.post("/api/ocr/endpoints/{endpoint_id}/test")
    async def api_ocr_endpoints_test(endpoint_id: str) -> dict:
        row = await _ocr_endpoint_row(endpoint_id)
        from .config import Settings as _Settings
        from .ocr.endpoints import _endpoint_settings
        from .ocr.providers import build_ocr_provider

        settings = _Settings()
        try:
            provider = build_ocr_provider(_endpoint_settings(settings, row))
        except Exception as exc:
            return {"ok": False, "error": str(exc), "elapsed_ms": 0}
        return await provider.probe()

    @app.put("/api/ocr/endpoints/{endpoint_id}/default")
    async def api_ocr_endpoints_default_set(endpoint_id: str) -> dict:
        async with session_factory(engine())() as session:
            row = await session.get(OcrEndpoint, endpoint_id)
            if row is None:
                raise HTTPException(status_code=404, detail="OCR endpoint not found")
            current = (
                await session.execute(
                    sqlalchemy.select(OcrEndpoint).where(OcrEndpoint.is_default.is_(True))
                )
            ).scalars().all()
            for other in current:
                other.is_default = False
            row.is_default = True
            row.enabled = True
            await session.commit()
            return {"default": _ocr_endpoint_out(row)}

    # ── OCR provider catalog (for the endpoint form) ──
    @app.get("/api/ocr/providers")
    def api_ocr_providers() -> dict:
        """Provider catalog: defaults for the "new endpoint" form.

        The default base_url/model per provider comes from .env, so the form
        can pre-fill them; keys are never echoed.
        """
        from .config import Settings as _Settings
        from .ocr.providers import SUPPORTED_PROVIDERS

        settings = _Settings()
        providers = []
        for name in SUPPORTED_PROVIDERS:
            if name == "paddle":
                base_url, model = (
                    settings.paddleocr_api_url,
                    settings.paddleocr_api_model,
                )
            elif name == "mistral":
                base_url, model = settings.mistral_api_url, settings.mistral_ocr_model
            elif name == "mineru":
                base_url, model = settings.mineru_api_url, settings.mineru_model_version
            else:
                base_url, model = settings.azure_di_endpoint, settings.azure_di_model
            providers.append(
                {"name": name, "base_url": base_url, "model": model}
            )
        return {"providers": providers, "supported": list(SUPPORTED_PROVIDERS)}

    # ── model endpoints ──
    async def _endpoint_row(endpoint_id: str) -> ModelEndpoint:
        async with session_factory(engine())() as session:
            row = await session.get(ModelEndpoint, endpoint_id)
        if row is None:
            raise HTTPException(status_code=404, detail="endpoint not found")
        return row

    @app.get("/api/endpoints")
    async def api_endpoints_list() -> dict:
        async with session_factory(engine())() as session:
            rows = (
                (await session.execute(sqlalchemy.select(ModelEndpoint).order_by(ModelEndpoint.created_at)))
                .scalars()
                .all()
            )
        return {"endpoints": [endpoint_out(row) for row in rows]}

    @app.post("/api/endpoints")
    async def api_endpoints_create(body: EndpointIn) -> dict:
        async with session_factory(engine())() as session:
            row = ModelEndpoint(
                name=body.name,
                base_url=body.base_url,
                api_key=body.api_key,
                model=body.model,
                reasoning_effort=body.reasoning_effort or None,
                enabled=int(body.enabled),
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return {"endpoint": endpoint_out(row)}

    @app.put("/api/endpoints/{endpoint_id}")
    async def api_endpoints_update(endpoint_id: str, body: EndpointIn) -> dict:
        async with session_factory(engine())() as session:
            row = await session.get(ModelEndpoint, endpoint_id)
            if row is None:
                raise HTTPException(status_code=404, detail="endpoint not found")
            row.name = body.name
            row.base_url = body.base_url
            row.model = body.model
            row.reasoning_effort = body.reasoning_effort or None
            row.enabled = int(body.enabled)
            if body.api_key:
                row.api_key = body.api_key
            await session.commit()
            await session.refresh(row)
            return {"endpoint": endpoint_out(row)}

    @app.delete("/api/endpoints/{endpoint_id}")
    async def api_endpoints_delete(endpoint_id: str) -> dict:
        async with session_factory(engine())() as session:
            row = await session.get(ModelEndpoint, endpoint_id)
            if row is None:
                raise HTTPException(status_code=404, detail="endpoint not found")
            await session.delete(row)
            await session.commit()
        return {"deleted": endpoint_id}

    @app.post("/api/endpoints/{endpoint_id}/test")
    async def api_endpoints_test(endpoint_id: str) -> dict:
        row = await _endpoint_row(endpoint_id)
        import time as _time

        from .config import Settings as _Settings

        started = _time.monotonic()
        settings = _Settings(
            tocmemex_llm_api_key=row.api_key,
            tocmemex_llm_base_url=row.base_url,
            tocmemex_llm_model=row.model,
            tocmemex_llm_reasoning_effort=row.reasoning_effort or "",
        )
        try:
            # sync httpx call — run in a thread so the event loop keeps serving;
            # reasoning models may spend the whole 1-token budget on reasoning,
            # so a null content still proves the endpoint is reachable
            await asyncio.to_thread(
                llm_complete, "ping", settings, max_tokens=1, retries=0, require_content=False
            )
        except LLMError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "elapsed_ms": int((_time.monotonic() - started) * 1000),
            }
        return {
            "ok": True,
            "elapsed_ms": int((_time.monotonic() - started) * 1000),
            "model": row.model,
            "base_url": row.base_url,
        }

    @app.get("/api/endpoints/default")
    async def api_endpoints_default_get() -> dict:
        async with session_factory(engine())() as session:
            row = (
                await session.execute(
                    sqlalchemy.select(ModelEndpoint).where(
                        ModelEndpoint.enabled.is_(True),
                        ModelEndpoint.is_default.is_(True),
                    )
                )
            ).scalar_one_or_none()
        return {"default": endpoint_out(row) if row else None}

    @app.get("/api/endpoints/magpie/probe")
    async def api_endpoints_magpie_probe() -> dict:
        """Detect a magpie local model gateway on the default port.

        Never raises: gateway down, a non-magpie service on the port, or an
        unreadable model catalog all collapse to ``available: false`` — the
        user then adds the endpoint by hand as usual.
        """
        import httpx

        host = "http://127.0.0.1:3425"

        def _probe() -> dict:
            with httpx.Client(timeout=1.5) as client:
                try:
                    hello = client.get(f"{host}/api/hello").json()
                except (httpx.HTTPError, OSError, ValueError):
                    return {"available": False, "reason": "gateway not reachable"}
                if not isinstance(hello, dict) or hello.get("name") != "magpie":
                    return {"available": False, "reason": "port occupied by another service"}
                try:
                    catalog = client.get(f"{host}/v1/models").json()
                except (httpx.HTTPError, OSError, ValueError):
                    return {"available": False, "reason": "models catalog not readable"}
                raw = catalog.get("data") if isinstance(catalog, dict) else None
                models = []
                if isinstance(raw, list):
                    for item in raw:
                        if isinstance(item, dict) and item.get("id"):
                            levels = [
                                lv.get("effort")
                                for lv in (
                                    item.get("supported_reasoning_levels") or []
                                )
                                if isinstance(lv, dict) and lv.get("effort")
                            ]
                            models.append(
                                {
                                    "id": item["id"],
                                    "reasoning_levels": levels,
                                    **(
                                        {"display_name": item["display_name"]}
                                        if item.get("display_name")
                                        else {}
                                    ),
                                }
                            )
                return {"available": True, "base_url": f"{host}/v1", "models": models}

        return await asyncio.to_thread(_probe)

    @app.put("/api/endpoints/{endpoint_id}/default")
    async def api_endpoints_default_set(endpoint_id: str) -> dict:
        async with session_factory(engine())() as session:
            row = await session.get(ModelEndpoint, endpoint_id)
            if row is None:
                raise HTTPException(status_code=404, detail="endpoint not found")
            current = (
                await session.execute(
                    sqlalchemy.select(ModelEndpoint).where(ModelEndpoint.is_default.is_(True))
                )
            ).scalars().all()
            for other in current:
                other.is_default = False
            row.is_default = True
            row.enabled = True
            await session.commit()
            return {"default": endpoint_out(row)}

    # ── TOC bench ──
    def _scenario_out(row: TocBenchScenario) -> dict:
        return {
            "id": row.id,
            "doc_ids": json.loads(row.doc_ids),
            "endpoint_ids": json.loads(row.endpoint_ids),
            "ocr_endpoint_ids": json.loads(row.ocr_endpoint_ids)
            if row.ocr_endpoint_ids
            else ["cached"],
            "status": row.status,
            "error": row.error,
            "created_at": row.created_at,
            "finished_at": row.finished_at,
        }

    def _run_out(row: TocBenchRun) -> dict:
        return {
            "id": row.id,
            "scenario_id": row.scenario_id,
            "doc_id": row.doc_id,
            "endpoint_id": row.endpoint_id,
            "side": row.side,
            "status": row.status,
            "error": row.error,
            "node_count": row.node_count,
            "validate_errors": row.validate_errors,
            "validate_warnings": row.validate_warnings,
            "coverage": row.coverage,
            "score": json.loads(row.score_json) if row.score_json else None,
            "elapsed_ms": row.elapsed_ms,
            "endpoint_name": row.endpoint_name,
            "base_url": row.base_url,
            "model": row.model,
            "ocr_endpoint_id": row.ocr_endpoint_id,
            "ocr_label": row.ocr_label,
            "ocr_status": row.ocr_status,
            "ocr_elapsed_ms": row.ocr_elapsed_ms,
            "ocr_error": row.ocr_error,
        }

    @app.post("/api/bench")
    async def api_bench_submit(body: BenchIn, background_tasks: BackgroundTasks) -> dict:
        from .config import Settings as _Settings
        from .llm.endpoints import active_endpoint

        async with session_factory(engine())() as session:
            # the default endpoint must exist: no silent rule-only runs
            resolved = await active_endpoint(session, _Settings())
            if resolved is None:
                raise HTTPException(
                    status_code=400,
                    detail="no active endpoint configured; set a default endpoint first",
                )
            for doc_id in body.doc_ids:
                if await session.get(Document, doc_id) is None:
                    raise HTTPException(status_code=404, detail=f"document not found: {doc_id}")
            for endpoint_id in body.endpoint_ids:
                if await session.get(ModelEndpoint, endpoint_id) is None:
                    raise HTTPException(status_code=404, detail=f"endpoint not found: {endpoint_id}")
            ocr_choices = body.ocr_endpoint_ids or ["cached"]
            for choice in ocr_choices:
                if choice in {"cached", "text"}:
                    continue
                if await session.get(OcrEndpoint, choice) is None:
                    raise HTTPException(
                        status_code=404, detail=f"OCR endpoint not found: {choice}"
                    )

            scenario = TocBenchScenario(
                doc_ids=json.dumps(body.doc_ids),
                endpoint_ids=json.dumps(body.endpoint_ids),
                ocr_endpoint_ids=json.dumps(ocr_choices),
                status="processing",
            )
            session.add(scenario)
            await session.commit()
            await session.refresh(scenario)
            scenario_id = scenario.id

        background_tasks.add_task(
            run_scenario,
            scenario_id,
            list(body.doc_ids),
            list(body.endpoint_ids),
            _Settings(),
            session_factory(engine()),
            None,
            ocr_choices,
        )
        async with session_factory(engine())() as session:
            row = await session.get(TocBenchScenario, scenario_id)
            return {"scenario": _scenario_out(row)}

    @app.get("/api/bench")
    async def api_bench_list() -> dict:
        async with session_factory(engine())() as session:
            rows = (
                (
                    await session.execute(
                        sqlalchemy.select(TocBenchScenario).order_by(TocBenchScenario.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )
        return {"scenarios": [_scenario_out(row) for row in rows]}

    @app.get("/api/bench/{scenario_id}")
    async def api_bench_detail(scenario_id: str) -> dict:
        async with session_factory(engine())() as session:
            scenario = await session.get(TocBenchScenario, scenario_id)
            if scenario is None:
                raise HTTPException(status_code=404, detail="scenario not found")
            runs = (
                (
                    await session.execute(
                        sqlalchemy.select(TocBenchRun)
                        .where(TocBenchRun.scenario_id == scenario_id)
                        .order_by(TocBenchRun.id)
                    )
                )
                .scalars()
                .all()
            )
        return {"scenario": _scenario_out(scenario), "runs": [_run_out(row) for row in runs]}

    @app.delete("/api/bench/{scenario_id}")
    async def api_bench_delete(scenario_id: str) -> dict:
        async with session_factory(engine())() as session:
            scenario = await session.get(TocBenchScenario, scenario_id)
            if scenario is None:
                raise HTTPException(status_code=404, detail="scenario not found")
            await session.delete(scenario)
            await session.commit()
        return {"deleted": scenario_id}

    # ── agent console: runs ─────────────────────────────────────────────
    # One question = one run. The loop runs as a detached task (LLM calls are
    # in a worker thread; the event loop stays responsive) and persists every
    # step, so GET /api/runs/{id} is a complete replay and the SSE endpoint is
    # only a live view over the same rows.
    @app.post("/api/runs")
    async def api_start_run(body: RunIn) -> dict:
        async with session_factory(engine())() as session:
            # The console answers from the 编修 agent's bound endpoint by
            # default: same model the curation loop uses, one place to
            # configure. Falls back to the shared default endpoint.
            from .llm.purpose import resolve_runtime

            resolved, endpoint_name, _cfg = await resolve_runtime(session, "bianxiu")
            if resolved is None:
                raise HTTPException(
                    status_code=409,
                    detail="尚未配置模型端点。请先在「设置 → 模型端点」添加并启用一个端点。",
                )
            if body.priorRunId and await session.get(AgentRun, body.priorRunId) is None:
                raise HTTPException(status_code=404, detail="prior run not found")
            run = AgentRun(
                question=body.question,
                allow_notes=body.allowNotes,
                max_calls=body.maxCalls,
                max_tokens=body.maxTokens,
                prior_run_id=body.priorRunId,
                endpoint=endpoint_name,
                model=resolved.tocmemex_llm_model,
            )
            session.add(run)
            await session.commit()
            await session.refresh(run)
        task = asyncio.create_task(
            agent_execute_run(run.id, engine=engine(), settings=resolved)
        )
        app.state.run_tasks.add(task)
        task.add_done_callback(app.state.run_tasks.discard)
        return {"run": agent_run_out(run, [])}

    @app.get("/api/runs")
    async def api_runs(limit: int = Query(default=20, ge=1, le=100)) -> dict:
        async with session_factory(engine())() as session:
            rows = (
                (
                    await session.execute(
                        sqlalchemy.select(AgentRun)
                        .order_by(AgentRun.created_at.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return {"runs": [agent_run_out(row, []) for row in rows]}

    @app.get("/api/runs/{run_id}")
    async def api_run_detail(run_id: str) -> dict:
        async with session_factory(engine())() as session:
            run = await session.get(AgentRun, run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="run not found")
            steps = (
                (
                    await session.execute(
                        sqlalchemy.select(AgentStep)
                        .where(AgentStep.run_id == run_id)
                        .order_by(AgentStep.seq)
                    )
                )
                .scalars()
                .all()
            )
        return {"run": agent_run_out(run, steps)}

    @app.post("/api/runs/{run_id}/cancel")
    async def api_cancel_run(run_id: str) -> dict:
        async with session_factory(engine())() as session:
            run = await session.get(AgentRun, run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="run not found")
            if run.status not in ("queued", "running"):
                raise HTTPException(status_code=409, detail="run 已结束，不能取消")
            run.status = "cancelled"
            run.error = "用户取消"
            run.finished_at = utcnow_iso()
            await session.commit()
            await session.refresh(run)
        return {"run": agent_run_out(run, [])}

    @app.get("/api/runs/{run_id}/events")
    async def api_run_events(run_id: str) -> StreamingResponse:
        async with session_factory(engine())() as session:
            if await session.get(AgentRun, run_id) is None:
                raise HTTPException(status_code=404, detail="run not found")

        async def stream():
            last = 0
            deadline = time.monotonic() + 600
            while True:
                async with session_factory(engine())() as session:
                    run = await session.get(AgentRun, run_id)
                    if run is None:
                        yield 'event: error\ndata: {"error":"run not found"}\n\n'
                        return
                    status = run.status
                    steps = (
                        (
                            await session.execute(
                                sqlalchemy.select(AgentStep)
                                .where(
                                    AgentStep.run_id == run_id,
                                    AgentStep.seq > last,
                                )
                                .order_by(AgentStep.seq)
                            )
                        )
                        .scalars()
                        .all()
                    )
                for step in steps:
                    last = step.seq
                    yield (
                        "data: "
                        + json.dumps(agent_step_out(step), ensure_ascii=False)
                        + "\n\n"
                    )
                if status in {"done", "failed"}:
                    yield "event: done\ndata: {}\n\n"
                    return
                if time.monotonic() > deadline:
                    yield 'event: timeout\ndata: {}\n\n'
                    return
                await asyncio.sleep(0.3)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # ── static frontend (built bundle) ──
    if web_dir is not None and (web_dir / "index.html").exists():
        if (web_dir / "assets").is_dir():
            app.mount(
                "/assets",
                StaticFiles(directory=web_dir / "assets"),
                name="assets",
            )

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str) -> Response:
            web_root = web_dir.resolve()
            candidate = (web_dir / full_path).resolve()
            if full_path and candidate.is_file() and candidate.is_relative_to(web_root):
                return FileResponse(candidate)
            # index.html fallback: a stale cached copy of an API path (served
            # before the backend had that route) would shadow the real route,
            # so the fallback must never be cacheable.
            return FileResponse(
                web_dir / "index.html",
                headers={"Cache-Control": "no-store"},
            )

    return app


def _doc_row(db_path: Path, doc_id: str) -> sqlite3.Row:
    with closing(_connect(db_path)) as connection:
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
        raise HTTPException(status_code=404, detail="document not found")
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.web_api")
    parser.add_argument("--db", default="data/tocmemex.db")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--web-dir", default="web/dist")
    args = parser.parse_args(argv)

    enforce_bind_host(args.host)

    import uvicorn

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(
            f"database not found: {db_path}\n"
            "Run `python -m app.cli init` first, or double-click start-web.bat "
            "(it initializes an empty library on first launch)."
        )
    web_dir = Path(args.web_dir) if args.web_dir else None
    app = create_app(db_path, Path(args.data_dir) if args.data_dir else None, web_dir)
    print(f"Toc Memex web: http://{args.host}:{args.port}")
    print(f"database: {db_path}")
    print("Ctrl+C to stop.")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
