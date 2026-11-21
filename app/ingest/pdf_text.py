from __future__ import annotations

import re
from pathlib import Path

import pypdfium2 as pdfium

from ..ocr.models import Bookmark, OcrBlock, OcrPage, ParsedDocument
from ..textnorm import normalize_text
from .heading import parse_heading
from .printed_toc import _TOC_MARKERS, parse_toc_page

# Sample this many pages before deciding whether the text layer is usable.
_SAMPLE_PAGES = 6
_MIN_CHARS_PER_PAGE = 200
_MIN_MEANINGFUL_RATIO = 0.80
_SENTENCE_END = ("。", "；", "，", ".", ";")
_PAGE_TOKEN = re.compile(
    r"^(?:\d+(?:[-－–—−]\d+)?|[A-Za-z][-－–—−]\d+|[ivxlcdmIVXLCDM]+)$"
)
_UNNUMBERED_TOP_LEVEL = {"前言", "引言", "参考文献", "索引", "缩略语", "目录"}
_MEANINGFUL_PUNCT = set(
    ".,;:!?()[]{}<>/\\-+=*%#@&'\"，。；：！？（）【】《》、—“”‘’…·－～"
)


def _meaningful_ratio(text: str) -> float:
    if not text:
        return 0.0
    ok = 0
    for char in text:
        if char.isspace() or "一" <= char <= "鿿":
            ok += 1
        elif char.isascii() and (char.isalnum() or char in _MEANINGFUL_PUNCT):
            ok += 1
        elif char in _MEANINGFUL_PUNCT:
            ok += 1
    return ok / len(text)


# Unit and math symbols that only make sense next to a number. A CJK text
# layer full of them but without any digit has lost its digit glyphs — the
# signature of a broken CID/ToUnicode mapping (every "1300 ℃" becomes
# "高熔点 ℃").
_MEASURE_SYMBOLS = ("℃", "℉", "％", "%", "±", "×", "÷", "μ", "µ", "Ω", "°", "‰")
_PUA_OR_REPLACEMENT = re.compile(r"[-�]")
_DIGITS = set("0123456789０１２３４５６７８９")


def _digit_signal_broken(text: str) -> bool:
    """True when the layer kept measurement symbols but dropped every digit.

    A valid text layer for a technical document always carries digits next
    to ℃/%/±; their total absence means the font's character mapping is
    broken and the surviving CJK is unreliable (wrong near-homoglyphs).
    """
    cjk = sum(1 for char in text if "一" <= char <= "鿿")
    if cjk / max(len(text), 1) < 0.30:
        return False  # only meaningful for CJK-dominant documents
    symbols = sum(text.count(symbol) for symbol in _MEASURE_SYMBOLS)
    if symbols < 3:
        return False
    digits = sum(1 for char in text if char in _DIGITS)
    return digits <= 2


def text_layer_is_usable(doc) -> bool:
    """Reject scanned PDFs and PDFs with broken CID/font encodings.

    Broken text layers still return characters, but many are C1 controls or
    Latin-1 mojibake; meaningful-ratio and CJK/ASCII checks separate those
    from real text layers. Mappings that survive those checks can still be
    wrong — near-homoglyph CJK with every digit swallowed — so the digit
    signal and private-use/replacement characters catch the rest.
    """
    sample = min(_SAMPLE_PAGES, len(doc))
    text = "".join(
        doc[index].get_textpage().get_text_range() for index in range(sample)
    )
    if len(text) < max(200, sample * _MIN_CHARS_PER_PAGE):
        return False
    if _meaningful_ratio(text) < _MIN_MEANINGFUL_RATIO:
        return False
    if _PUA_OR_REPLACEMENT.search(text):
        return False
    if _digit_signal_broken(text):
        return False
    cjk = sum(1 for char in text if "一" <= char <= "鿿")
    ascii_letters = sum(1 for char in text if char.isascii() and char.isalpha())
    if cjk:
        return cjk / len(text) >= 0.15
    return ascii_letters / len(text) >= 0.30


def _glyph_rects(textpage) -> list[tuple[float, float, float, float, str]]:
    glyphs: list[tuple[float, float, float, float, str]] = []
    for index in range(textpage.count_rects()):
        left, bottom, right, top = textpage.get_rect(index)
        if top - bottom <= 0 or right - left <= 0:
            continue
        text = textpage.get_text_bounded(left, bottom, right, top)
        if not text or text.isspace():
            continue
        glyphs.append((left, bottom, right, top, text))
    return glyphs


def _page_lines(textpage, page_height: float) -> list[OcrBlock]:
    """Group glyph rects into lines and read each line's text in one call."""
    glyphs = _glyph_rects(textpage)
    if not glyphs:
        return []

    glyphs.sort(key=lambda item: (-item[3], item[0]))
    lines: list[list[tuple[float, float, float, float, str]]] = []
    centers: list[float] = []
    heights: list[float] = []
    for glyph in glyphs:
        center = (glyph[1] + glyph[3]) / 2
        height = glyph[3] - glyph[1]
        if lines:
            tolerance = max(heights[-1], height) * 0.6
            if abs(center - centers[-1]) <= tolerance:
                lines[-1].append(glyph)
                centers[-1] = (centers[-1] + center) / 2
                heights[-1] = max(heights[-1], height)
                continue
        lines.append([glyph])
        centers.append(center)
        heights.append(height)

    blocks: list[OcrBlock] = []
    for order, line in enumerate(lines):
        left = min(item[0] for item in line)
        bottom = min(item[1] for item in line)
        right = max(item[2] for item in line)
        top = max(item[3] for item in line)
        text = textpage.get_text_bounded(left, bottom, right, top).strip()
        if not text:
            continue
        text = normalize_text(text)
        label = "text"
        if _PAGE_TOKEN.fullmatch(text) and page_height - top > page_height * 0.85:
            label = "number"
        blocks.append(
            OcrBlock(
                label=label,
                content=text,
                bbox=(left, page_height - top, right, page_height - bottom),
                order=order,
            )
        )
    return blocks


def _text_heading(text: str) -> tuple[str | None, str, int] | None:
    parsed = parse_heading(text)
    if parsed is None:
        return None
    num, title, level = parsed
    if num is None:
        if "".join(text.split()) not in _UNNUMBERED_TOP_LEVEL:
            return None
        return parsed
    if not title or len(title) < 2 or len(title) > 80:
        return None
    if title.endswith(_SENTENCE_END):
        return None
    if not (
        "一" <= title[0] <= "鿿"
        or (title[0].isascii() and title[0].isalpha())
    ):
        return None
    if title[0].isascii() and title[0].islower():
        return None
    if len(num) == 1 and num.isascii() and num.isalpha():
        return None
    match = re.match(rf"^\s*{re.escape(num)}", text)
    if match:
        after = text[match.end() :]
        if after and not after[0].isspace() and not (
            "一" <= after[0] <= "鿿"
        ):
            return None
    return parsed


def _toc_pages(pages: list[OcrPage]) -> set[int]:
    indices: set[int] = set()
    for page in pages:
        text = " ".join(block.content for block in page.blocks)
        compact = re.sub(r"\s+", "", text[:400].lower())
        if any(marker in compact for marker in _TOC_MARKERS):
            indices.add(page.page_index)
            continue
        if len(parse_toc_page(page)) >= 5:
            indices.add(page.page_index)
    return indices


def parse_pdf_text(path: Path) -> ParsedDocument | None:
    """Read a PDF's embedded text layer. Returns None when it is unusable."""
    doc = pdfium.PdfDocument(str(path))
    try:
        if not text_layer_is_usable(doc):
            return None
        pages: list[OcrPage] = []
        for index in range(len(doc)):
            page = doc[index]
            width = float(page.get_width())
            height = float(page.get_height())
            blocks = _page_lines(page.get_textpage(), height)
            pages.append(
                OcrPage(
                    page_index=index,
                    width=width,
                    height=height,
                    markdown="",
                    blocks=blocks,
                    images={},
                    raw={},
                )
            )
    finally:
        doc.close()

    toc_pages = _toc_pages(pages)
    for page_index in toc_pages:
        pages[page_index].blocks = [
            OcrBlock("text", block.content, block.bbox, block.order)
            if block.label == "paragraph_title"
            else block
            for block in pages[page_index].blocks
        ]

    bookmarks: list[Bookmark] = []
    for page in pages:
        if page.page_index in toc_pages:
            continue
        blocks: list[OcrBlock] = []
        for block in page.blocks:
            if block.label == "text":
                parsed = _text_heading(block.content)
                if parsed is not None:
                    num, _, level = parsed
                    block = OcrBlock(
                        "paragraph_title",
                        block.content,
                        block.bbox,
                        block.order,
                    )
                    bookmarks.append(
                        Bookmark(
                            level=level,
                            title=block.content,
                            page_index=page.page_index,
                            heading_num=num,
                        )
                    )
            blocks.append(block)
        page.blocks = blocks

    return ParsedDocument(
        pages=pages,
        bookmarks=bookmarks,
        metadata={"format": "pdf-text", "toc_pages": sorted(toc_pages)},
    )
