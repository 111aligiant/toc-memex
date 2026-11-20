from __future__ import annotations

import html
import json
import re

from .models import OcrBlock, OcrPage
from ..textnorm import normalize_text


class OcrParseError(ValueError):
    """PaddleOCR returned an error or an incomplete result payload."""


# Layout labels that are page furniture, not document content.
SKIP_LABELS = {
    "header",
    "footer",
    "page_number",
    "aside_text",
    "header_image",
    "footer_image",
}

_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_CELL_RE = re.compile(r"</t[dh]>", re.IGNORECASE)
_ROW_RE = re.compile(r"</tr>", re.IGNORECASE)
_TABLE_OPEN = re.compile(r"<table\b", re.IGNORECASE)


def _html_to_text(value: str) -> str:
    """Convert OCR table HTML to text for BM25 and LLM prompts.

    Tables are expanded into a rectangular Markdown grid first: stripping
    tags directly loses the rows a ``rowspan`` covers, so merged groups such
    as "基本检查" appeared only once and every following row lost a column.
    Non-table HTML keeps the old whitespace-separated conversion.
    """
    if "<" not in value:
        return value
    if _TABLE_OPEN.search(value):
        from ..ingest.tabletext import table_html_to_markdown

        markdown = table_html_to_markdown(value)
        if markdown:
            return markdown
    text = _BR_RE.sub("\n", value)
    text = _CELL_RE.sub("\t", text)
    text = _ROW_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    return html.unescape(text)


def _block_from_raw(raw: dict) -> OcrBlock:
    bbox = raw.get("block_bbox") or (0, 0, 0, 0)
    return OcrBlock(
        label=str(raw.get("block_label") or ""),
        content=normalize_text(str(raw.get("block_content") or "")),
        bbox=tuple(float(v) for v in bbox[:4]),
        order=raw.get("block_order"),
    )


def parse_jsonl(raw_text: str) -> list[OcrPage]:
    """Parse PaddleOCR result JSONL.

    We intentionally consume ``prunedResult.parsing_res_list`` rather than
    ``markdown.text``: TOC detection, heading matching and furniture
    filtering need block labels, bboxes and reading order. Markdown is kept
    only for image URLs.

    One JSONL line can contain multiple pages in ``layoutParsingResults``.
    Page indices are global across all lines, not reset per line.
    """
    pages: list[OcrPage] = []
    page_index = 0
    lines = [
        (number, line.strip())
        for number, line in enumerate(raw_text.splitlines(), 1)
    ]
    lines = [(number, line) for number, line in lines if line]
    if not lines:
        raise OcrParseError("empty PaddleOCR response")

    for line_number, line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OcrParseError(
                f"invalid PaddleOCR JSON on line {line_number}: {exc}"
            ) from exc
        if not isinstance(obj, dict):
            raise OcrParseError(
                f"PaddleOCR JSON line {line_number} is not an object"
            )

        log_id = obj.get("logId")
        error_code = obj.get("errorCode")
        error_msg = str(obj.get("errorMsg") or "").strip()
        if error_code is not None and str(error_code).strip() not in {"", "0"}:
            raise OcrParseError(
                f"PaddleOCR error {error_code}: "
                f"{error_msg or 'unknown'} (logId={log_id})"
            )
        if error_msg and error_msg.lower() not in {"success", "ok"}:
            raise OcrParseError(
                f"PaddleOCR error: {error_msg} (logId={log_id})"
            )

        result = obj.get("result")
        if not isinstance(result, dict):
            raise OcrParseError(f"PaddleOCR response missing result (logId={log_id})")
        results = result.get("layoutParsingResults")
        if not isinstance(results, list):
            raise OcrParseError(
                f"PaddleOCR response missing layoutParsingResults (logId={log_id})"
            )

        expected_pages = (result.get("dataInfo") or {}).get("numPages")
        if expected_pages is not None:
            try:
                expected_pages = int(expected_pages)
            except (TypeError, ValueError):
                expected_pages = None
        if expected_pages is not None and expected_pages != len(results):
            raise OcrParseError(
                f"PaddleOCR page count mismatch: parsed {len(results)}, "
                f"dataInfo.numPages={expected_pages} (logId={log_id})"
            )
        if not results:
            raise OcrParseError(f"PaddleOCR returned no pages (logId={log_id})")

        data_pages = (result.get("dataInfo") or {}).get("pages") or []
        for offset, rp in enumerate(results):
            pruned = rp.get("prunedResult") or {}
            markdown = rp.get("markdown") or {}
            width = float(pruned.get("width") or 0)
            height = float(pruned.get("height") or 0)
            if (not width or not height) and offset < len(data_pages):
                info_page = data_pages[offset] or {}
                width = width or float(info_page.get("width") or 0)
                height = height or float(info_page.get("height") or 0)
            pages.append(
                OcrPage(
                    page_index=page_index,
                    width=width,
                    height=height,
                    markdown=str(markdown.get("text") or ""),
                    blocks=[
                        _block_from_raw(b)
                        for b in (pruned.get("parsing_res_list") or [])
                    ],
                    images=dict(markdown.get("images") or {}),
                    raw=rp,
                )
            )
            page_index += 1

    if not pages:
        raise OcrParseError("PaddleOCR returned no pages")
    return pages


def _sort_key(block: OcrBlock) -> tuple:
    y = block.bbox[1] if len(block.bbox) > 1 else 0.0
    x = block.bbox[0] if block.bbox else 0.0
    return (block.order is None, block.order or 0, y, x)


def page_text(page: OcrPage) -> str:
    """Join content blocks in reading order, skipping page furniture.

    Tables are kept as text (HTML stripped) so BM25 sees their cell values.
    """
    parts: list[str] = []
    for block in sorted(page.blocks, key=_sort_key):
        if block.label in SKIP_LABELS or block.is_empty:
            continue
        content = _html_to_text(block.content).strip()
        if content:
            parts.append(content)
    return "\n\n".join(parts)