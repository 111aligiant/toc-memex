"""Export the parse output a document was built from, as one ZIP.

The archive is the honest answer to "give me the raw data behind this
document". What lands in it depends on how the document was parsed:

- OCR (``raw.jsonl`` and/or ``pages.json`` in ``data/ocr/<doc_id>/``):
  the vendor response verbatim, the normalized pages, and the bookmarks.
- Text layer / Word / Markdown / Excel: the original file plus the pages
  re-parsed from it right now (the text-layer equivalent of OCR output).
  Nothing is invented: the manifest says which channel produced the pages.

A manifest names every file and repeats the provenance row, so an archive
found on disk months later still explains itself.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from ..models import utcnow_iso
from ..ocr.cache import PAGES_FILE, RAW_FILE, pages_payload
from ..ocr.models import ParsedDocument
from .pipeline import BOOKMARKS_FILE

_REPARSABLE = {
    ".pdf", ".docx", ".doc", ".md", ".markdown", ".txt",
    ".xlsx", ".xls", ".csv", ".tsv",
}
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class ExportError(ValueError):
    """The document has no parse output that can be exported."""


@dataclass
class ExportBundle:
    filename: str
    content: bytes
    kind: str  # "ocr" | "text-layer"


def _safe_stem(name: str) -> str:
    """A filename safe on Windows, short enough for long source titles."""
    stem = _UNSAFE.sub("_", name).strip(" .")
    stem = stem[:80].strip(" .")
    return stem or "document"


def _manifest(
    doc: dict,
    *,
    kind: str,
    channel: str | None,
    run: dict | None,
    files: list[dict],
    note: str,
) -> dict:
    run = run or {}
    return {
        "doc": {
            "id": doc.get("id"),
            "title": doc.get("title"),
            "filename": doc.get("filename"),
            "pages": doc.get("page_count"),
        },
        "export_kind": kind,
        "parse_channel": channel,
        "ocr": {
            "used": bool(run.get("ocr_used")),
            "provider": run.get("ocr_provider"),
            "model": run.get("ocr_model"),
            "endpoint": run.get("ocr_endpoint_name"),
            "job_id": run.get("ocr_job_id"),
            "cache": run.get("ocr_cache"),
        },
        "files": files,
        "note": note,
        "exported_at": utcnow_iso(),
    }


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _add_file(
    files: dict[str, bytes],
    listed: list[dict],
    name: str,
    data: bytes,
    role: str,
) -> None:
    files[name] = data
    listed.append({"name": name, "bytes": len(data), "role": role})


def _cached_ocr_files(
    cache_dir: Path,
    files: dict[str, bytes],
    listed: list[dict],
) -> None:
    raw = cache_dir / RAW_FILE
    if raw.is_file():
        _add_file(files, listed, RAW_FILE, raw.read_bytes(), "ocr-raw")
    pages = cache_dir / PAGES_FILE
    if pages.is_file():
        _add_file(files, listed, PAGES_FILE, pages.read_bytes(), "ocr-pages")
    bookmarks = cache_dir / BOOKMARKS_FILE
    if bookmarks.is_file():
        _add_file(files, listed, BOOKMARKS_FILE, bookmarks.read_bytes(), "bookmarks")


def _reparse_original(path: Path) -> tuple[ParsedDocument, str] | None:
    """Parse the original the way ingest would have, without OCR."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from .pdf_text import parse_pdf_text

        parsed = parse_pdf_text(path)
        return (parsed, "pdf-text") if parsed is not None else None
    if suffix in {".docx", ".doc"}:
        from .docx_direct import parse_docx_direct

        return parse_docx_direct(path), "docx-direct"
    if suffix in {".md", ".markdown", ".txt"}:
        from .pipeline import parse_markdown

        return parse_markdown(path), "markdown"
    if suffix in {".xlsx", ".xls"}:
        from .pipeline import parse_xlsx

        return parse_xlsx(path), "xlsx"
    if suffix in {".csv", ".tsv"}:
        from .pipeline import parse_csv

        return parse_csv(path), "csv"
    return None


def _has_ocr_cache(cache_dir: Path) -> bool:
    """True when a provider left OCR output here (raw or normalized)."""
    return cache_dir.is_dir() and any(
        (cache_dir / name).is_file() for name in (RAW_FILE, PAGES_FILE)
    )


def export_kind(doc: dict, data_dir: Path) -> str | None:
    """What an export would contain, without building it.

    ``"ocr"`` — vendor raw response and/or normalized pages are on disk;
    ``"text-layer"`` — no OCR cache, but the original can be re-parsed;
    ``None`` — nothing exportable (original missing or unsupported type).
    """
    if _has_ocr_cache(Path(data_dir) / "ocr" / str(doc["id"])):
        return "ocr"
    original = Path(doc["file_path"]) if doc.get("file_path") else None
    if original is None or not original.is_file():
        return None
    return "text-layer" if original.suffix.lower() in _REPARSABLE else None


def build_export(
    doc: dict,
    data_dir: Path,
    run: dict | None = None,
) -> ExportBundle:
    """Assemble the export archive for one document.

    ``doc`` is a ``documents`` row; ``run`` is its newest ``ingest_runs``
    row (optional — the filesystem is the primary evidence).
    """
    doc_id = str(doc["id"])
    cache_dir = Path(data_dir) / "ocr" / doc_id
    files: dict[str, bytes] = {}
    listed: list[dict] = []

    if _has_ocr_cache(cache_dir):
        _cached_ocr_files(cache_dir, files, listed)
        channel = (run or {}).get("parse_channel") or "ocr"
        kind = "ocr"
        note = (
            "OCR 原始响应与归一化结果，原样取自 data/ocr/<doc_id>/。"
            "raw.jsonl 为厂商返回内容，未作任何改写。"
        )
    else:
        original = Path(doc["file_path"]) if doc.get("file_path") else None
        if original is None or not original.is_file():
            raise ExportError("原始文件缺失，无法导出解析数据")
        reparsed = _reparse_original(original)
        if reparsed is None:
            raise ExportError("该文档类型没有可导出的解析数据")
        parsed, channel = reparsed
        payload = pages_payload(parsed)
        _add_file(
            files,
            listed,
            PAGES_FILE,
            json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8"),
            "parsed-pages",
        )
        _add_file(files, listed, original.name, original.read_bytes(), "original")
        bookmarks = cache_dir / BOOKMARKS_FILE
        if bookmarks.is_file():
            _add_file(files, listed, BOOKMARKS_FILE, bookmarks.read_bytes(), "bookmarks")
        kind = "text-layer"
        note = (
            "该文档未调用 OCR，直接解析了文本层。pages.json 是导出时"
            "用同一套解析器重新生成的逐页结果，original 为入库时的原件。"
        )

    if not files:
        raise ExportError("没有可导出的解析数据")

    manifest = _manifest(
        doc, kind=kind, channel=channel, run=run, files=listed, note=note
    )
    files["manifest.json"] = json.dumps(
        manifest, ensure_ascii=False, indent=1
    ).encode("utf-8")

    stem = _safe_stem(Path(doc.get("filename") or doc.get("title") or doc_id).stem)
    suffix = "-ocr-raw.zip" if kind == "ocr" else "-parse-raw.zip"
    return ExportBundle(filename=f"{stem}{suffix}", content=_zip(files), kind=kind)
