from __future__ import annotations

import bisect
import re
from pathlib import Path

from ..ocr.models import Bookmark, OcrBlock, OcrPage, ParsedDocument
from .heading import parse_heading
from .word import _heading_num

# Word constants.
_WD_STATISTIC_PAGES = 2
_WD_GOTO_PAGE = 1
_WD_GOTO_ABSOLUTE = 1

# Synthetic page geometry. Printed-TOC detection and page skips require a
# positive page size; real block coordinates come from paragraph order, so the
# absolute values only need to satisfy "is this line full width" checks.
_PAGE_WIDTH = 100.0
_PAGE_HEIGHT = 100.0

# A printed-TOC line ends with a tab/spaces and a page number. Those lines are
# navigation entries, not body headings, even though they start with "1 ...".
_TOC_LINE_PAGE = re.compile(r"(?:\t| {2,})\s*[0-9ivxlcdmIVXLCDM]+\s*$")
_SENTENCE_END = ("。", "；", "，", ".", ";")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f]")
_UNNUMBERED_TOP_LEVEL = {
    "前言",
    "引言",
    "参考文献",
    "索引",
    "缩略语",
    "目录",
    "目 录",
}


def _clean_paragraph(raw: str) -> str:
    """Drop Word control marks while keeping tabs (TOC lines need them)."""
    return _CONTROL_CHARS.sub("", raw.replace("\x0b", " ")).strip()


def _text_heading(text: str) -> tuple[str | None, str, int] | None:
    """Detect a heading from paragraph text when Word has no outline level.

    Many standards are exported with every paragraph in ``Normal`` style, so
    the outline level is body for real headings too. Accept only numbered
    headings or a small set of unnumbered top-level sections; reject body
    sentences and printed-TOC lines.
    """
    if _TOC_LINE_PAGE.search(text):
        return None
    candidate = text
    # Word layouts often letter-space the appendix label ("附 录 B").
    if re.search(r"附\s+录", text):
        candidate = re.sub(r"\s+", "", text)
    parsed = parse_heading(candidate)
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
    appendix = num.startswith(("附录", "Annex", "Appendix"))
    if title[0].isdigit() or title[0].isascii() and title[0].islower():
        return None
    if not (
        "一" <= title[0] <= "鿿"
        or (title[0].isascii() and title[0].isalpha())
        or (appendix and title[0] in "(（")
    ):
        return None
    # A lone letter heading ("N 个天线位置", "E .5 5 μH ...") is almost always
    # a table/figure label. Appendix subsections carry a dot ("A.1").
    if (
        num
        and len(num) == 1
        and num.isascii()
        and num.isalpha()
    ):
        return None
    # Word headings normally separate the number from the title with a space
    # or tab; values such as "5cm" / "25Hz～1kHz" are table cells.
    match = re.match(rf"^\s*{re.escape(num)}", text)
    if match:
        after = text[match.end() :]
        if after and not after[0].isspace():
            return None
    return parsed


def _heading_number(text: str) -> str | None:
    """Heading number for a Word outline heading, spacing tolerated."""
    parsed = parse_heading(text)
    if parsed is not None and parsed[0]:
        return parsed[0]
    if re.search(r"附\s+录", text):
        parsed = parse_heading(re.sub(r"\s+", "", text))
        if parsed is not None and parsed[0]:
            return parsed[0]
    return _heading_num(text)


def _page_starts(doc) -> list[int]:
    """Character offset where each Word page starts, via GoTo.

    ``Range.Information(wdActiveEndPageNumber)`` forces a layout query per
    paragraph and is unusably slow on long documents. Page starts plus
    paragraph offsets give the same mapping in one pass over the pages.
    """
    try:
        count = int(doc.ComputeStatistics(_WD_STATISTIC_PAGES))
    except Exception:
        return []
    starts: list[int] = []
    for page in range(1, count + 1):
        try:
            starts.append(int(doc.GoTo(_WD_GOTO_PAGE, _WD_GOTO_ABSOLUTE, page).Start))
        except Exception:
            continue
    return starts


_TOC_MARKERS = ("目录", "目次", "contents", "tableofcontents")


def _toc_page_indices(pages: dict[int, list[OcrBlock]]) -> set[int]:
    indices: set[int] = set()
    for page_index, blocks in pages.items():
        if not blocks:
            continue
        text = " ".join(block.content for block in blocks)
        head = re.sub(r"\s+", "", text[:400].lower())
        if any(marker in head for marker in _TOC_MARKERS):
            indices.add(page_index)
            continue
        toc_lines = sum(
            1 for block in blocks if _TOC_LINE_PAGE.search(block.content)
        )
        if toc_lines >= 5 and toc_lines * 2 >= len(blocks):
            indices.add(page_index)
    return indices


def parse_word_document(doc) -> ParsedDocument:
    """Read a Word COM document into pages/blocks without PDF or OCR.

    Word gives us paragraph text, outline level and page layout. That is
    enough for TOC construction and chunking, and it avoids sending a
    text-native DOCX through PDF export + cloud OCR.
    """
    paragraphs = list(doc.Paragraphs)
    starts: list[int] = []
    texts: list[str] = []
    for para in paragraphs:
        span = para.Range
        texts.append(span.Text)
        starts.append(int(span.Start))
    page_starts = _page_starts(doc)

    pages: dict[int, list[OcrBlock]] = {}
    headings: list[tuple[int, int, str, str | None]] = []
    order = 0

    for index, para in enumerate(paragraphs):
        start = starts[index]
        text = _clean_paragraph(texts[index])
        if not text:
            continue

        page = bisect.bisect_right(page_starts, start) - 1 if page_starts else 0
        page = max(0, page)

        level = 10
        toc_line = bool(_TOC_LINE_PAGE.search(text))
        if (
            not toc_line
            and len(text) <= 100
            and not text.endswith(_SENTENCE_END)
        ):
            try:
                level = int(para.OutlineLevel)
            except Exception:
                level = 10
        is_heading = 1 <= level <= 9
        heading_num = _heading_number(text)
        if not is_heading and not toc_line:
            detected = _text_heading(text)
            if detected is not None:
                is_heading = True
                heading_num, _, level = detected
        # "（规范性附录）导则" is the subtitle of the appendix heading on the
        # line before it, not a heading of its own.
        if text.startswith(("(", "（")) and "附录" in re.sub(r"\s+", "", text):
            is_heading = False

        label = "paragraph_title" if is_heading else "text"
        pages.setdefault(page, []).append(
            OcrBlock(
                label=label,
                content=text,
                bbox=(0.0, float(order), _PAGE_WIDTH, float(order + 1)),
                order=order,
            )
        )
        order += 1
        if is_heading:
            headings.append((page, level, text, heading_num))

    # Printed-TOC pages are navigation, not body: their entries must not be
    # offered as body headings or matching paragraph_title blocks.
    toc_pages = _toc_page_indices(pages)
    for page_index in toc_pages:
        pages[page_index] = [
            OcrBlock("text", block.content, block.bbox, block.order)
            if block.label == "paragraph_title"
            else block
            for block in pages[page_index]
        ]
    bookmarks = [
        Bookmark(level=level, title=text, page_index=page, heading_num=num)
        for page, level, text, num in headings
        if page not in toc_pages
    ]

    total_pages = max(max(pages) + 1 if pages else 0, len(page_starts))
    ocr_pages = [
        OcrPage(
            page_index=index,
            width=_PAGE_WIDTH,
            height=_PAGE_HEIGHT,
            markdown="",
            blocks=pages.get(index, []),
            images={},
            raw={},
        )
        for index in range(total_pages)
    ]
    return ParsedDocument(
        pages=ocr_pages,
        bookmarks=bookmarks,
        metadata={"format": "docx-direct"},
    )


def parse_docx_direct(src: Path) -> ParsedDocument:
    """Open a DOCX/DOC with Word COM and parse it without OCR."""
    from .word import close_word_document, open_word_document

    app, doc, temp_copy = open_word_document(src)
    try:
        return parse_word_document(doc)
    finally:
        close_word_document(app, doc, temp_copy)
