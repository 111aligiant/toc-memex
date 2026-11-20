"""Turn provider-specific output into the project's block model.

PaddleOCR-VL gives us semantic blocks directly. Mistral OCR and most VLM
providers give page-level Markdown instead: headings, tables, images and
LaTeX survive, but per-block bboxes do not. These helpers recover as much
structure as Markdown allows so TOC detection, printed-page mapping and
chunking keep working across providers.

Everything here is deliberately conservative: a block is only given a
semantic label when the Markdown makes it unambiguous. Line order is always
preserved, and synthetic bboxes keep vertical order (and page-bottom page
numbers) usable by ``printed_toc``.
"""

from __future__ import annotations

import re

from .models import OcrBlock

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)[^)]*\)")
_TABLE_OPEN = re.compile(r"<table\b", re.IGNORECASE)
_TABLE_CLOSE = re.compile(r"</table\s*>", re.IGNORECASE)
_FENCE = re.compile(r"^(```|~~~)")
_SEPARATOR = re.compile(r"^[-=_*~]{3,}$")
_PIPE_TABLE = re.compile(r"^\|.*\|$")
# A bare printed page number, optionally decorated ("- 12 -", "— 3 —").
_PAGE_LABEL = re.compile(r"^[-–—\s]*\d{1,4}[-–—\s]*$")


def _bbox(line_no: int, total: int, width: float, height: float) -> tuple:
    """Synthetic bbox: proportional vertical position, full text width."""
    if total <= 0:
        return (0.0, float(line_no), width, float(line_no) + 1.0)
    if height <= 0:
        return (0.0, float(line_no), width, float(line_no) + 1.0)
    y = height * line_no / total
    return (0.0, y, width, y + height / total)


def markdown_to_blocks(
    markdown: str, width: float = 0.0, height: float = 0.0
) -> list[OcrBlock]:
    """Split page Markdown into ordered blocks with coarse labels."""
    lines = (markdown or "").splitlines()
    total = max(1, len(lines))
    blocks: list[OcrBlock] = []
    index = 0

    def emit(label: str, content: str, line_no: int) -> None:
        text = content.strip()
        if not text:
            return
        blocks.append(
            OcrBlock(
                label=label,
                content=text,
                bbox=_bbox(line_no, total, width, height),
                order=len(blocks) + 1,
            )
        )

    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        if not stripped:
            index += 1
            continue

        if _TABLE_OPEN.search(stripped):
            start = index
            collected = [raw]
            while index + 1 < len(lines) and not _TABLE_CLOSE.search(lines[index]):
                index += 1
                collected.append(lines[index])
            emit("table", "\n".join(collected), start)
            index += 1
            continue

        if _FENCE.match(stripped):
            start = index
            marker = stripped[:3]
            collected = [raw]
            index += 1
            while index < len(lines):
                collected.append(lines[index])
                if lines[index].strip().startswith(marker):
                    index += 1
                    break
                index += 1
            emit("text", "\n".join(collected), start)
            continue

        heading = _HEADING.match(stripped)
        if heading:
            emit("paragraph_title", heading.group(2), index)
            index += 1
            continue

        image = _IMAGE.search(stripped)
        if image:
            emit("figure", image.group(1) or image.group(2), index)
            index += 1
            continue

        if stripped.startswith("$$"):
            start = index
            collected = [raw]
            if stripped.count("$$") == 1:
                while index + 1 < len(lines):
                    index += 1
                    collected.append(lines[index])
                    if "$$" in lines[index]:
                        break
            emit("display_formula", "\n".join(collected), start)
            index += 1
            continue

        if _SEPARATOR.match(stripped):
            index += 1
            continue

        label = "table" if _PIPE_TABLE.match(stripped) else "text"
        emit(label, stripped, index)
        index += 1

    # Mark a trailing bare number as a page number: printed-page mapping
    # (``build_page_number_map``) and furniture filtering both rely on it.
    if blocks and _PAGE_LABEL.match(blocks[-1].content):
        blocks[-1] = OcrBlock(
            label="number",
            content=blocks[-1].content,
            bbox=blocks[-1].bbox,
            order=blocks[-1].order,
        )
    return blocks


def polygon_to_bbox(
    polygon: list[float] | tuple[float, ...] | None,
    *,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
) -> tuple[float, float, float, float]:
    """Azure/MinerU polygon (x0,y0,x1,y1,...) -> (x0,y0,x1,y1)."""
    if not polygon or len(polygon) < 4:
        return (0.0, 0.0, 0.0, 0.0)
    xs = [float(value) for value in polygon[0::2]]
    ys = [float(value) for value in polygon[1::2]]
    return (
        min(xs) * scale_x,
        min(ys) * scale_y,
        max(xs) * scale_x,
        max(ys) * scale_y,
    )
