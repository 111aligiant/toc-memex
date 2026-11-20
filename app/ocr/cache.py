"""Provider-neutral parse cache.

Every OCR provider writes its vendor response to ``raw.jsonl`` (audit /
replay) and the normalized result to ``pages.json``. Downstream code
(TOC, chunker, reindex, web API, inspector) reads ``pages.json`` first and
falls back to the Paddle JSONL parser for documents ingested before this
cache existed, so no migration is needed.

``pages.json`` is deliberately lossless for our model and lossy for the
vendor: blocks carry label/content/bbox/order, pages carry dimensions,
Markdown and image URLs. A re-parse is never required to rebuild TOC.
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import OcrBlock, OcrPage, ParsedDocument
from .parser import OcrParseError, parse_jsonl

PAGES_FILE = "pages.json"
RAW_FILE = "raw.jsonl"
_VERSION = 1


class OcrCacheError(ValueError):
    """The cached parse output is unreadable or was written by another model."""


def _page_to_dict(page: OcrPage) -> dict:
    return {
        "page_index": page.page_index,
        "width": page.width,
        "height": page.height,
        "markdown": page.markdown,
        "blocks": [
            {
                "label": block.label,
                "content": block.content,
                "bbox": list(block.bbox),
                "order": block.order,
            }
            for block in page.blocks
        ],
        "images": dict(page.images),
    }


def _page_from_dict(data: dict) -> OcrPage:
    return OcrPage(
        page_index=int(data.get("page_index") or 0),
        width=float(data.get("width") or 0),
        height=float(data.get("height") or 0),
        markdown=str(data.get("markdown") or ""),
        blocks=[
            OcrBlock(
                label=str(block.get("label") or ""),
                content=str(block.get("content") or ""),
                bbox=tuple(float(v) for v in (block.get("bbox") or (0, 0, 0, 0))[:4]),
                order=block.get("order"),
            )
            for block in (data.get("blocks") or [])
        ],
        images=dict(data.get("images") or {}),
        raw={},
    )


def pages_payload(parsed: ParsedDocument) -> dict:
    """The ``pages.json`` shape, so exports and the cache cannot drift."""
    return {
        "_meta": {
            "version": _VERSION,
            "provider": parsed.metadata.get("ocr_provider"),
            "model": parsed.metadata.get("ocr_model"),
            "job_id": parsed.metadata.get("job_id"),
        },
        "bookmarks": [
            {
                "level": bookmark.level,
                "title": bookmark.title,
                "page_index": bookmark.page_index,
                "heading_num": bookmark.heading_num,
            }
            for bookmark in parsed.bookmarks
        ],
        "pages": [_page_to_dict(page) for page in parsed.pages],
    }


def save_pages(out_dir: Path, parsed: ParsedDocument) -> Path:
    """Write the normalized cache; the caller still writes ``raw.jsonl``."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = pages_payload(parsed)
    path = out_dir / PAGES_FILE
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return path


def load_pages(out_dir: Path) -> ParsedDocument | None:
    """Read ``pages.json``; None when it is absent (old cache)."""
    path = Path(out_dir) / PAGES_FILE
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OcrCacheError(f"unreadable OCR cache {path}: {exc}") from exc
    pages = payload.get("pages")
    if not isinstance(pages, list) or not pages:
        raise OcrCacheError(f"OCR cache {path} has no pages")
    from .models import Bookmark

    bookmarks = [
        Bookmark(
            level=int(item.get("level") or 1),
            title=str(item.get("title") or ""),
            page_index=int(item.get("page_index") or 0),
            heading_num=item.get("heading_num"),
        )
        for item in (payload.get("bookmarks") or [])
        if str(item.get("title") or "").strip()
    ]
    meta = payload.get("_meta") or {}
    return ParsedDocument(
        pages=[_page_from_dict(item) for item in pages],
        bookmarks=bookmarks,
        metadata={
            "ocr_provider": meta.get("provider"),
            "ocr_model": meta.get("model"),
            "job_id": meta.get("job_id"),
            "cache": "pages.json",
        },
    )


def load_cached_pages(out_dir: Path) -> list[OcrPage] | None:
    """Pages for read-only consumers (web API / inspector).

    Prefers ``pages.json``; falls back to Paddle ``raw.jsonl``. Returns None
    when neither exists so callers can render "page not available".
    """
    parsed = load_pages(out_dir)
    if parsed is not None:
        return parsed.pages
    raw_path = Path(out_dir) / RAW_FILE
    if not raw_path.exists():
        return None
    try:
        return parse_jsonl(raw_path.read_text(encoding="utf-8"))
    except (OSError, OcrParseError) as exc:
        raise OcrCacheError(f"unreadable OCR cache {raw_path}: {exc}") from exc
