"""Extract figures and tables from text-layer PDFs.

The text-layer parser used to return text/number lines only, so every PDF
whose embedded text layer was usable ended up with zero rows in
``resources``: the reading stream had no figures or tables
even though the pages clearly contained them. OCR was never needed for these
files — the geometry is in the PDF.

pdfplumber supplies table cell rectangles (which let us rebuild the original
``rowspan``/``colspan`` HTML instead of a flattened grid) and image placement
rectangles. pypdfium2 renders the crop as it appears on the page. The result
is attached to the existing ``ParsedDocument`` exactly like an OCR block, so
``save_resources`` and every downstream consumer work unchanged.

Both libraries are permissively licensed (MIT / Apache-2.0), unlike PyMuPDF's
AGPL, which is why rendering is done with pypdfium2 rather than a single
all-in-one PDF toolkit.
"""

from __future__ import annotations

import html
import logging
import re
from pathlib import Path

import pdfplumber
import pypdfium2 as pdfium
from PIL import Image

from ..ocr.models import OcrBlock, OcrPage, ParsedDocument
from ..textnorm import normalize_text
from .pdf_text import _toc_pages

logger = logging.getLogger(__name__)

_CAPTION = re.compile(
    r"^\s*(图|表|Figure|Fig\.?|Table|Tab\.?)\s*[0-9A-Za-z]",
    re.IGNORECASE,
)
_FIGURE_CAPTION = re.compile(
    r"^\s*(图|Figure|Fig\.?)\s*[0-9A-Za-z]",
    re.IGNORECASE,
)
_TABLE_CAPTION = re.compile(
    r"^\s*(表|Table|Tab\.?)\s*[0-9A-Za-z]",
    re.IGNORECASE,
)
# Captions sit next to their figure/table; a sentence that merely mentions
# "图1" in the body is not one. Bound both the length and the distance.
_MAX_CAPTION_CHARS = 120
_CAPTION_DISTANCE = 90
_MIN_IMAGE_W = 40
_MIN_IMAGE_H = 30
_MAX_FIGURE_PAGE_RATIO = 0.72
_RENDER_DPI = 150
_FURNITURE_RATIO = 0.05


def _rect_distance(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    dx = max(0.0, ax0 - bx1, bx0 - ax1)
    dy = max(0.0, ay0 - by1, by0 - ay1)
    return (dx * dx + dy * dy) ** 0.5


def _clean(value: str) -> str:
    return " ".join(normalize_text(value).split())


def _is_caption(text: str) -> bool:
    compact = text.strip()
    if not compact or len(compact) > _MAX_CAPTION_CHARS:
        return False
    if not _CAPTION.match(compact):
        return False
    return not compact.endswith(("。", "；", "，", ",", ";", ":", "："))


def _has_ink(image: Image.Image) -> bool:
    """Cheap non-white check so blank crops never become figure resources."""
    gray = image.convert("L")
    histogram = gray.histogram()
    dark = sum(histogram[:245])
    return dark > max(8, gray.width * gray.height * 0.003)


def _render(
    pdf_page,
    page_width: float,
    page_height: float,
    rect: tuple[float, float, float, float],
    target: Path,
) -> bool:
    x0, top, x1, bottom = rect
    left = max(0.0, min(x0, page_width))
    right = max(0.0, min(x1, page_width))
    upper = max(0.0, min(top, page_height))
    lower = max(0.0, min(bottom, page_height))
    if right - left < _MIN_IMAGE_W or lower - upper < _MIN_IMAGE_H:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    # pypdfium2's render(crop=...) takes cut-off amounts in PDF bottom-up
    # space: (left, bottom, right, top). ``_render`` receives pdfplumber
    # top-down coordinates (x0, top, x1, bottom), so ``upper``/``lower`` are
    # distances from the page *top*. Converting to bottom-up cut-offs:
    #   cut from bottom = page_height - lower  (keep everything above ``lower``)
    #   cut from top    = upper                (keep everything below ``upper``)
    # Passing ``upper`` as the bottom cut (the old bug) mirrored the crop
    # vertically, so figures in the top half were rendered blank and dropped
    # by ``_has_ink``. Verified on a synthetic PDF: wrong formula renders a
    # top-half black block to 0 dark pixels, correct formula keeps it whole.
    bitmap = pdf_page.render(
        scale=_RENDER_DPI / 72,
        crop=(left, page_height - lower, page_width - right, upper),
    )
    image = bitmap.to_pil()
    if not _has_ink(image):
        return False
    image.save(target, "JPEG", quality=88)
    return True


def _words_in(
    words: list[dict],
    rect: tuple[float, float, float, float],
) -> list[dict]:
    x0, top, x1, bottom = rect
    found = [
        word
        for word in words
        if x0 - 1 <= (word["x0"] + word["x1"]) / 2 <= x1 + 1
        and top - 1 <= (word["top"] + word["bottom"]) / 2 <= bottom + 1
    ]
    found.sort(key=lambda word: (round(word["top"], 1), word["x0"]))
    return found


def _table_html(page, table, words: list[dict]) -> str:
    """Rebuild the table with its original rowspan/colspan.

    ``Table.extract()`` represents a merged cell as one value and ``None``
    in the covered positions, which is exactly the information a text reader
    cannot recover. The cell rectangles in ``Table.cells`` do have the span
    geometry, so map them onto a row/column boundary grid and emit HTML.
    """
    cells = [cell for cell in getattr(table, "cells", []) if cell]
    if not cells:
        return ""

    x_values = sorted({round(value, 1) for cell in cells for value in (cell[0], cell[2])})
    y_values = sorted({round(value, 1) for cell in cells for value in (cell[1], cell[3])})
    if len(x_values) < 3 or len(y_values) < 3:
        return ""
    x_index = {value: index for index, value in enumerate(x_values)}
    y_index = {value: index for index, value in enumerate(y_values)}

    grid: dict[tuple[int, int], tuple[int, int, int, int]] = {}
    for cell in cells:
        x0, top, x1, bottom = (round(value, 1) for value in cell[:4])
        if x0 not in x_index or x1 not in x_index or top not in y_index or bottom not in y_index:
            continue
        column, row = x_index[x0], y_index[top]
        column_end, row_end = x_index[x1] - 1, y_index[bottom] - 1
        if row_end < row or column_end < column:
            continue
        for row_index in range(row, row_end + 1):
            for column_index in range(column, column_end + 1):
                grid[(row_index, column_index)] = (row, column, row_end, column_end)

    if not grid:
        return ""

    rows: list[str] = []
    for row in range(len(y_values) - 1):
        cells_html: list[str] = []
        for column in range(len(x_values) - 1):
            owner = grid.get((row, column))
            if owner is None:
                continue
            row_start, column_start, row_end, column_end = owner
            if (row, column) != (row_start, column_start):
                continue
            rect = (
                x_values[column_start],
                y_values[row_start],
                x_values[column_end + 1],
                y_values[row_end + 1],
            )
            cell_words = _words_in(words, rect)
            text = html.escape(_clean(" ".join(word["text"] for word in cell_words)))
            attributes = []
            if row_end > row_start:
                attributes.append(f'rowspan="{row_end - row_start + 1}"')
            if column_end > column_start:
                attributes.append(f'colspan="{column_end - column_start + 1}"')
            attr_text = (" " + " ".join(attributes)) if attributes else ""
            cells_html.append(f"<td{attr_text}>{text}</td>")
        if cells_html:
            rows.append("<tr>" + "".join(cells_html) + "</tr>")
    if len(rows) < 2:
        return ""
    return "<table>" + "".join(rows) + "</table>"


def _image_rects(page) -> list[tuple[float, float, float, float]]:
    rects: list[tuple[float, float, float, float]] = []
    seen: set[tuple[float, float, float, float]] = set()
    for image in page.images:
        rect = (
            round(float(image["x0"]), 1),
            round(float(image["top"]), 1),
            round(float(image["x1"]), 1),
            round(float(image["bottom"]), 1),
        )
        if rect in seen:
            continue
        seen.add(rect)
        width, height = rect[2] - rect[0], rect[3] - rect[1]
        if width < _MIN_IMAGE_W or height < _MIN_IMAGE_H:
            continue
        if width * height > page.width * page.height * _MAX_FIGURE_PAGE_RATIO:
            continue
        rects.append(rect)
    return rects


def _nearest_caption(
    caption_rects: list[tuple[OcrBlock, tuple[float, float, float, float]]],
    target: tuple[float, float, float, float],
    *,
    figure_only: bool = False,
    exclude: set[int] | None = None,
) -> OcrBlock | None:
    best: OcrBlock | None = None
    best_distance = _CAPTION_DISTANCE + 1
    for block, rect in caption_rects:
        if figure_only and not _FIGURE_CAPTION.match(block.content):
            continue
        if exclude and id(block) in exclude:
            continue
        distance = _rect_distance(target, rect)
        if distance < best_distance:
            best = block
            best_distance = distance
    return best


def _attach_page(
    page,
    pdf_page,
    model: OcrPage,
    page_index: int,
    work_dir: Path,
    toc_pages: set[int],
) -> None:
    blocks = list(model.blocks)
    block_rects = {id(block): block.bbox for block in blocks}

    # ── tables ──────────────────────────────────────────────────────────
    # find_tables() is much cheaper than OCR but still not free; run it only
    # where a 表/Table caption says a table exists. Without this guard a
    # 323-page manual spent minutes probing prose pages.
    has_table_caption = any(
        block.label in {"text", "number"}
        and _is_caption(block.content)
        and _TABLE_CAPTION.match(block.content.strip())
        for block in blocks
    )
    # Caption-less tables still exist. A page that is mostly ruling lines is
    # a cheap second signal; it costs far less than probing every prose page.
    try:
        has_rule_grid = len(page.edges) >= 8
    except Exception:
        has_rule_grid = False
    table_rects: list[tuple[float, float, float, float]] = []
    if page_index not in toc_pages and (has_table_caption or has_rule_grid):
        try:
            found = page.find_tables()
        except Exception:
            found = []
        words = page.extract_words() if found else []
        for table in found:
            box = tuple(float(value) for value in table.bbox)
            rect = (box[0], box[1], box[2], box[3])
            if rect[2] - rect[0] < 60 or rect[3] - rect[1] < 30:
                continue
            content = _table_html(page, table, words)
            if not content:
                continue
            table_rects.append(rect)
            inside = [
                block
                for block in blocks
                if block.label not in {"header", "footer", "number"}
                and rect[0] <= block_rects[id(block)][0]
                and rect[1] <= block_rects[id(block)][1]
                and block_rects[id(block)][2] <= rect[2]
                and block_rects[id(block)][3] <= rect[3]
            ]
            inside_ids = {id(block) for block in inside}
            orders = [block.order for block in inside if block.order is not None]
            order = (min(orders) - 0.4) if orders else (max(
                (block.order for block in blocks if block.order is not None),
                default=0,
            ) + 1)
            blocks = [block for block in blocks if id(block) not in inside_ids]
            block_rects = {id(block): block.bbox for block in blocks}
            blocks.append(
                OcrBlock(label="table", content=content, bbox=rect, order=order)
            )
            block_rects[id(blocks[-1])] = rect

    # ── figures ─────────────────────────────────────────────────────────
    work_dir.mkdir(parents=True, exist_ok=True)
    image_rects = [
        rect
        for rect in _image_rects(page)
        if not any(
            rect[0] < table_rect[2] and table_rect[0] < rect[2]
            and rect[1] < table_rect[3] and table_rect[1] < rect[3]
            for table_rect in table_rects
        )
    ]

    # Captions are ordinary text lines in the text layer; relabel the ones
    # that sit next to a table or image so save_resources can title it.
    media_rects = table_rects + image_rects
    caption_blocks: list[tuple[OcrBlock, tuple[float, float, float, float]]] = []
    for index, block in enumerate(blocks):
        if block.label not in {"text", "number"} or not _is_caption(block.content):
            continue
        rect = block_rects[id(block)]
        if not any(_rect_distance(rect, media) <= _CAPTION_DISTANCE for media in media_rects):
            continue
        blocks[index] = OcrBlock("figure_title", block.content, block.bbox, block.order)
        caption_blocks.append((blocks[index], rect))

    used_rects: list[tuple[float, float, float, float]] = []
    used_caption_ids: set[int] = set()
    figures = 0
    for rect in image_rects:
        if rect[3] < page.height * _FURNITURE_RATIO or rect[1] > page.height * (1 - _FURNITURE_RATIO):
            continue
        caption = _nearest_caption(
            caption_blocks, rect, figure_only=True, exclude=used_caption_ids
        )
        target = work_dir / f"fig_p{page_index}_{figures}.jpg"
        if not _render(pdf_page, page.width, page.height, rect, target):
            continue
        key = f"{int(rect[0])}_{int(rect[1])}_{int(rect[2])}_{int(rect[3])}.png"
        model.images[key] = target.resolve().as_uri()
        order = caption.order if caption is not None and caption.order is not None else len(blocks) + figures
        blocks.append(
            OcrBlock(label="figure", content="", bbox=rect, order=order)
        )
        used_rects.append(rect)
        if caption is not None:
            used_caption_ids.add(id(caption))
        figures += 1

    # Vector figures have no raster placement. Crop the region above their
    # caption, stopping at the previous body line.
    for caption, caption_rect in caption_blocks:
        if not _FIGURE_CAPTION.match(caption.content):
            continue
        if id(caption) in used_caption_ids:
            continue
        if any(_rect_distance(caption_rect, used) <= 24 for used in used_rects):
            continue
        prior_bottoms = [
            block.bbox[3]
            for block in blocks
            if block is not caption
            and block.label not in {"figure", "table"}
            and block.bbox[3] <= caption_rect[1] - 2
        ]
        start = (max(prior_bottoms) + 6) if prior_bottoms else caption_rect[1] - 260
        if caption_rect[1] - start < 50:
            start = caption_rect[1] - 260
        clip = (
            min(page.width * 0.04, caption_rect[0]),
            max(page.height * 0.03, start),
            max(page.width * 0.96, caption_rect[2]),
            caption_rect[1] - 3,
        )
        if any(
            clip[0] < table_rect[2] and table_rect[0] < clip[2]
            and clip[1] < table_rect[3] and table_rect[1] < clip[3]
            for table_rect in table_rects
        ):
            continue
        target = work_dir / f"fig_p{page_index}_{figures}.jpg"
        if not _render(pdf_page, page.width, page.height, clip, target):
            continue
        key = f"{int(clip[0])}_{int(clip[1])}_{int(clip[2])}_{int(clip[3])}.png"
        model.images[key] = target.resolve().as_uri()
        blocks.append(
            OcrBlock(
                label="figure",
                content="",
                bbox=clip,
                order=(caption.order if caption.order is not None else len(blocks)) + 0.1,
            )
        )
        used_rects.append(clip)
        figures += 1

    blocks.sort(
        key=lambda block: (
            block.order is None,
            block.order if block.order is not None else 0,
            block.bbox[1],
            block.bbox[0],
        )
    )
    model.blocks = blocks


def attach_pdf_text_media(
    path: Path,
    parsed: ParsedDocument,
    work_dir: Path,
) -> None:
    """Add figure/table blocks and rendered images to a text-layer parse.

    ``work_dir`` must be inside the document's OCR cache directory
    (``data/ocr/<doc_id>``): ``save_resources`` copies ``file:`` image URLs
    only from within that root.
    """
    if not parsed.pages:
        return
    toc_pages = set(parsed.metadata.get("toc_pages") or [])
    if not toc_pages:
        toc_pages = _toc_pages(parsed.pages)
    try:
        document = pdfplumber.open(str(path))
    except Exception:
        # Media enrichment must never make a readable text layer fail.
        logger.warning("text-layer media: cannot open %s", path, exc_info=True)
        return
    try:
        try:
            pdfium_document = pdfium.PdfDocument(str(path))
        except Exception:
            logger.warning("text-layer media: cannot render %s", path, exc_info=True)
            return
        try:
            for page_index, model in enumerate(parsed.pages):
                if page_index >= len(document.pages):
                    break
                try:
                    _attach_page(
                        document.pages[page_index],
                        pdfium_document[page_index],
                        model,
                        page_index,
                        work_dir,
                        toc_pages,
                    )
                except Exception:
                    # A malformed page must not take the whole text-layer
                    # parse (and therefore the document) down with it. Log it
                    # so a systematic render failure (e.g. a bad crop) is
                    # visible instead of silently yielding zero figures.
                    logger.warning(
                        "text-layer media: page %d failed in %s",
                        page_index,
                        path,
                        exc_info=True,
                    )
                    continue
        finally:
            pdfium_document.close()
    finally:
        document.close()
    parsed.metadata = {
        **(parsed.metadata or {}),
        "media_extracted": True,
    }