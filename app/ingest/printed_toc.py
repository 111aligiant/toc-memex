from __future__ import annotations

import re
from dataclasses import dataclass
from statistics import median

from vendor.pageindex.flash.embedded_toc import (
    _normalize_title,
    _roman_to_arabic,
    _similarity,
)

from ..identifiers import is_bare_designation
from ..ocr.models import OcrBlock, OcrPage, ParsedDocument
from .bookmarks import filter_bookmarks
from .heading import chapter_key, cn_number_to_int, parse_heading

_TOC_MARKERS = ("目录", "目 录", "目次", "contents", "table of contents")
_PAGE_TOKEN = (
    r"(?:\d+(?:[-－–—−]\d+)?|[A-Za-z][-－–—−]\d+|"
    r"[ivxlcdmIVXLCDM]+)"
)
_TOC_LINE_DOT = re.compile(
    rf"^(?P<title>.+?)[\s.…·．]{{2,}}\s*(?P<page>{_PAGE_TOKEN})\s*$"
)
_TOC_LINE_PLAIN = re.compile(
    rf"^(?P<head>(?:\d+(?:\.\d+)*|第[一二三四五六七八九十百零〇\d]+章|"
    rf"附录\s*[A-Z]|(?:Annex|Appendix)\s*[A-Za-z]|[A-Z](?:\.\d+)*))\s+"
    rf"(?P<title>.+?)\s+(?P<page>{_PAGE_TOKEN})\s*$",
    re.IGNORECASE,
)
_TOC_LINE_SPACED = re.compile(
    rf"^(?P<title>.+?)\s{{2,}}(?P<page>{_PAGE_TOKEN})\s*$"
)
_TOC_LINE_CHAPTER = re.compile(
    r"^(?P<head>第\s*[一二三四五六七八九十百零〇\d]+\s*章|附录\s*[A-Z]|"
    r"(?:Annex|Appendix)\s*[A-Za-z])\s*(?P<title>.*)$",
    re.IGNORECASE,
)
_PAGE_NUM = re.compile(rf"^(?:{_PAGE_TOKEN})$")
# Trailing printed-page label on a TOC continuation line, where the leader
# has degraded to "#", stray dots or just spaces. Fullwidth digits included:
# broken text layers often switch digit width mid-document.
_TRAILING_PAGE_LABEL = re.compile(
    r"(?:[\s#＃.。·…．]{1,}|\s{2,})(?P<page>[0-9０-９]+|[ivxIVX]+)\s*$"
)
_DASH_TRANS = str.maketrans(
    {"－": "-", "–": "-", "—": "-", "−": "-"}
)
_ROMAN = re.compile(r"^[ivxlcdmIVXLCDM]+$")
_ROMAN_ASCII = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII"]
_ROMAN_UNICODE = {chr(0x2160 + i): value for i, value in enumerate(_ROMAN_ASCII)}
_ROMAN_UNICODE.update(
    {chr(0x2170 + i): value for i, value in enumerate(_ROMAN_ASCII)}
)

_SKIP_LABELS = {"header", "footer", "page_number", "aside_text"}


@dataclass
class TocEntry:
    title: str
    heading_num: str | None
    level: int
    printed_page: str | None
    page_index: int | None
    confidence: float
    x0: float = 0.0
    is_master: bool = False
    order: int = 0
    evidence: str = "rules"

    @property
    def chapter(self) -> str | None:
        return chapter_key(self.heading_num)


@dataclass
class MappingContext:
    """Everything entry-page mapping needs, shared by both proposal paths."""

    page_map: dict[str, int]
    body_headings: list[tuple[int, str]]
    chapter_toc_ranges: dict[str, tuple[int, int]]


def build_mapping_context(
    pages: list[OcrPage],
    toc_page_indices: set[int],
    single_chapter_pages: list[tuple[int, str]],
) -> MappingContext:
    """Compute page_map / body headings / chapter-TOC ranges once.

    ``single_chapter_pages`` is the sorted list of (page_index, chapter)
    for TOC pages that mention exactly one chapter — the chapter-detail
    runs used to bound each chapter's page range.
    """
    # Consecutive TOC pages for the same chapter are one run: the body
    # starts after the last page of the run, not after the first.
    chapter_toc_runs: list[tuple[str, int, int]] = []
    for page_index, chapter in single_chapter_pages:
        if (
            chapter_toc_runs
            and chapter_toc_runs[-1][0] == chapter
            and page_index == chapter_toc_runs[-1][2] + 1
        ):
            chapter_toc_runs[-1] = (
                chapter,
                chapter_toc_runs[-1][1],
                page_index,
            )
        else:
            chapter_toc_runs.append((chapter, page_index, page_index))
    chapter_toc_ranges: dict[str, tuple[int, int]] = {}
    for index, (chapter, _, last_page) in enumerate(chapter_toc_runs):
        end = (
            chapter_toc_runs[index + 1][1] - 1
            if index + 1 < len(chapter_toc_runs)
            else len(pages) - 1
        )
        start = last_page + 1
        chapter_toc_ranges.setdefault(chapter, (start, max(start, end)))

    page_map = build_page_number_map(pages, toc_page_indices)
    body_headings = _body_headings(pages, toc_page_indices)
    return MappingContext(
        page_map=page_map,
        body_headings=body_headings,
        chapter_toc_ranges=chapter_toc_ranges,
    )


@dataclass
class PrintedToc:
    entries: list[TocEntry]
    source_page: int
    confidence: float


def _roman_ascii(value: str) -> str:
    return "".join(_ROMAN_UNICODE.get(char, char) for char in value.strip()).translate(
        _DASH_TRANS
    )


def _printed_key(value: str) -> str | None:
    """Map a printed page label to a style-aware key.

    Roman I and Arabic 1 are different pages in a document with front matter,
    so the key keeps the style: ``r:i`` vs ``n:1``.
    """
    normalized = _roman_ascii(value)
    if normalized.isdigit():
        return f"n:{int(normalized)}"
    match = re.fullmatch(r"([0-9A-Za-z]+)-(\d+)", normalized)
    if match:
        chapter = match.group(1)
        if chapter.isalpha():
            chapter = chapter.upper()
        return f"h:{chapter}-{int(match.group(2))}"
    if _ROMAN.fullmatch(normalized):
        return f"r:{normalized.lower()}"
    return None


def _printed_value(value: str) -> int | None:
    normalized = _roman_ascii(value)
    if normalized.isdigit():
        return int(normalized)
    if _ROMAN.fullmatch(normalized):
        try:
            return int(_roman_to_arabic(normalized.lower()))
        except KeyError:
            return None
    return None


def _ordered_blocks(page: OcrPage) -> list[OcrBlock]:
    blocks = [
        block
        for block in page.blocks
        if block.label not in _SKIP_LABELS and block.content.strip()
    ]
    if not blocks:
        return []
    full_width = any(
        block.bbox[2] - block.bbox[0] > page.width * 0.6 for block in blocks
    )
    if page.width > 0 and not full_width:
        left = [b for b in blocks if b.bbox[0] < page.width / 2]
        right = [b for b in blocks if b.bbox[0] >= page.width / 2]
        if left and right:
            return sorted(left, key=lambda b: (b.bbox[1], b.bbox[0])) + sorted(
                right, key=lambda b: (b.bbox[1], b.bbox[0])
            )
    return sorted(blocks, key=lambda b: (b.bbox[1], b.bbox[0]))


def _make_entry(line: str, x0: float, confidence: float) -> TocEntry | None:
    match = _TOC_LINE_DOT.match(line)
    if match:
        raw_title = match.group("title").strip()
        page = match.group("page")
    else:
        match = _TOC_LINE_PLAIN.match(line)
        if match:
            raw_title = f"{match.group('head')} {match.group('title')}".strip()
            page = match.group("page")
        else:
            match = _TOC_LINE_SPACED.match(line)
            if match:
                raw_title = match.group("title").strip()
                page = match.group("page")
            else:
                match = _TOC_LINE_CHAPTER.match(line)
                if not match:
                    return None
                raw_title = f"{match.group('head')} {match.group('title')}".strip()
                page = None
    parsed = parse_heading(raw_title) or (None, raw_title, 1)
    num, title, level = parsed
    if not title:
        return None
    if not any(char.isalpha() for char in title):
        # A heading always carries words. Digit-only titles come from merged
        # TOC blobs ("…＃４０４．５烧结性能＃４３" read as one line) and are
        # pure noise; keeping them fakes a healthy chapter tree.
        return None
    if num is None:
        # Figure/table captions and body sentences can end with a page-like
        # number on a TOC page; they are not TOC entries.
        if is_bare_designation(raw_title):
            return None
        if raw_title.startswith(("图", "表", "Figure", "Table")):
            return None
        if len(raw_title) > 60 or raw_title.endswith(("。", "；", "，", ".", ";")):
            return None
    return TocEntry(
        title=f"{num} {title}" if num and not title.startswith(num) else title,
        heading_num=num,
        level=level,
        printed_page=page,
        page_index=None,
        confidence=confidence,
        x0=x0,
    )


def parse_toc_page(page: OcrPage) -> list[TocEntry]:
    entries: list[TocEntry] = []
    for block in _ordered_blocks(page):
        for line in block.content.splitlines():
            raw = line.strip()
            if not raw:
                continue
            collapsed = " ".join(raw.split())
            if any(marker in collapsed.lower() for marker in _TOC_MARKERS):
                continue
            # Keep the raw spacing: dot-leader and spaced-leader TOC lines
            # differ only in how the title is separated from the page number.
            entry = _make_entry(
                raw,
                block.bbox[0],
                0.9 if "." in collapsed or "…" in collapsed else 0.8,
            )
            if entry is None:
                entry = _make_entry(
                    collapsed,
                    block.bbox[0],
                    0.9 if "." in collapsed or "…" in collapsed else 0.8,
                )
            if entry is None:
                continue
            if entries and entries[-1].title == entry.title:
                continue
            entry.order = len(entries)
            entries.append(entry)
    return entries


def _looks_like_toc_page(
    page: OcrPage, entries: list[TocEntry], require_marker: bool = False
) -> bool:
    text = " ".join(block.content for block in page.blocks).lower()
    marker = any(marker in text[:400] for marker in _TOC_MARKERS)
    if marker:
        return True
    if require_marker:
        return False
    return len(entries) >= 5


# How far a single reading may sit from where its own number says it belongs
# before the map refuses to trust it.
_PAGE_MAP_TOLERANCE = 2


def _page_number_blocks(page: OcrPage) -> list[OcrBlock]:
    """Blocks that may carry this page's printed number.

    PaddleOCR labels the folio ``number``; providers that only give Markdown
    keep it as a bare ``text`` block at the page bottom.
    """
    blocks = [
        block
        for block in page.blocks
        if block.label == "number" and block.content.strip()
    ]
    if not blocks and page.height > 0:
        blocks = [
            block
            for block in page.blocks
            if block.label == "text"
            and block.bbox[1] > page.height * 0.85
            and _PAGE_NUM.fullmatch(block.content.strip())
        ]
    return blocks


def build_page_number_map(
    pages: list[OcrPage], toc_pages: set[int] | None = None
) -> dict[str, int]:
    """Map every printed page label to the physical page carrying it.

    A footer reading can be wrong. PaddleOCR turns a roman front-matter
    numeral into an arabic digit (``Ⅰ`` -> ``1``, ``Ⅱ`` -> ``11``), and a
    table cell or figure label can be typed ``number`` on the same page as
    the real folio. Taking the first reading of a label therefore lets a
    misread contents-page folio claim a body page: ``n:1`` binds to the
    contents page, and every TOC entry printed on page 1 then maps one
    front-matter page too early, which surfaces as sibling ranges that
    overlap and as chunks attached to the wrong node.

    Collect every reading instead, then keep the one that agrees with the
    document's dominant printed-to-physical offset. A reading that no
    offset can explain stays unmapped, so ``_map_entries`` falls back to
    heading matching or offset arithmetic rather than trusting it.

    ``toc_pages`` lets the caller pass the printed-TOC page set it already
    computed. Readings there are kept only when a label appears nowhere
    else: a contents page's own folio is the least trustworthy reading in
    the document, but front-matter roman numerals often exist only there.
    """
    readings: dict[str, list[int]] = {}
    # A document's chapter numbers do not walk backwards: a footer claiming
    # an earlier chapter is a source-document error and would poison the map
    # for that whole chapter.
    last_chapter = 0
    for page in pages:
        for block in _page_number_blocks(page):
            key = _printed_key(block.content)
            if key is None:
                continue
            chapter = key[2:].partition("-")[0] if key.startswith("h:") else ""
            if chapter.isdigit():
                value = int(chapter)
                if value < last_chapter:
                    continue
                last_chapter = max(last_chapter, value)
            bucket = readings.setdefault(key, [])
            if page.page_index not in bucket:
                bucket.append(page.page_index)

    if toc_pages is None:
        toc_pages = toc_page_indices(pages)
    pools = {
        key: [page for page in found if page not in toc_pages] or found
        for key, found in readings.items()
    }

    # Only labels read exactly once feed the offset: a label read twice is
    # the ambiguity this function exists to resolve.
    offsets = sorted(
        pool[0] - int(key[2:])
        for key, pool in pools.items()
        if key.startswith("n:") and len(pool) == 1
    )
    offset = median(offsets) if offsets else None

    mapping: dict[str, int] = {}
    for key, pool in pools.items():
        if offset is None or not key.startswith("n:"):
            mapping[key] = pool[0]
            continue
        number = int(key[2:])
        best = min(pool, key=lambda page: abs(page - number - offset))
        if abs(best - number - offset) <= _PAGE_MAP_TOLERANCE:
            mapping[key] = best
    return mapping


def _body_headings(
    pages: list[OcrPage], toc_page_indices: set[int]
) -> list[tuple[int, str]]:
    # Local import avoids a module cycle: toc imports printed_toc at top.
    from .toc import _merge_adjacent_titles

    headings: list[tuple[int, str]] = []
    for page in pages:
        if page.page_index in toc_page_indices:
            continue
        ordered = sorted(
            page.blocks,
            key=lambda b: (b.order is None, b.order or 0, b.bbox[1], b.bbox[0]),
        )
        for block in _merge_adjacent_titles(ordered):
            if block.label == "paragraph_title" and block.content.strip():
                headings.append((page.page_index, block.content.strip()))
    return headings


def _best_heading_score(entry_title: str, heading_title: str) -> float:
    a = _normalize_title(entry_title)
    b = _normalize_title(heading_title)
    if not a or not b:
        return 0.0
    if a == b or a in b or b in a:
        return 1.0
    return _similarity(a, b)


def _chapter_local_page(
    entry: TocEntry,
    page_map: dict[str, int],
    chapter_toc_ranges: dict[str, tuple[int, int]] | None,
) -> int | None:
    """Resolve ``14-2`` using the chapter's own TOC page position.

    Global page maps can pick up a stray ``14-1`` in a table. A
    chapter-specific TOC page tells us the chapter starts right after it, and
    the next chapter's TOC page bounds the range.
    """
    if not chapter_toc_ranges or not entry.printed_page:
        return None
    normalized = _roman_ascii(entry.printed_page)
    match = re.fullmatch(r"([0-9A-Za-z]+)-(\d+)", normalized)
    if not match:
        return None
    chapter, number = match.group(1), int(match.group(2))
    if chapter.isalpha():
        chapter = chapter.upper()
    bounds = chapter_toc_ranges.get(chapter)
    if bounds is None:
        return None
    start, end = bounds
    key = _printed_key(entry.printed_page)
    global_page = page_map.get(key) if key else None
    if global_page is not None and start <= global_page <= end:
        return None
    local_page = start + number - 1
    # A detail page outside the chapter's own range is an OCR/table false
    # positive; let the global page map or heading similarity decide.
    if local_page > end:
        return None
    return max(0, local_page)


def _map_entries(
    entries: list[TocEntry],
    pages: list[OcrPage],
    page_map: dict[str, int],
    body_headings: list[tuple[int, str]],
    bookmark_chapter_pages: dict[str, int] | None = None,
    chapter_toc_ranges: dict[str, tuple[int, int]] | None = None,
) -> list[TocEntry]:
    n_pages = len(pages)
    for entry in entries:
        key = _printed_key(entry.printed_page or "")
        local_page = _chapter_local_page(entry, page_map, chapter_toc_ranges)
        if local_page is not None:
            entry.page_index = local_page
        elif key is not None and key in page_map:
            entry.page_index = page_map[key]

    # Master chapter entries often have no page number. Use the first mapped
    # detail entry of the same chapter (e.g. 4.0 → chapter 4), then the PDF
    # outline's first page for that chapter.
    chapter_pages: dict[str, int] = {}
    for entry in entries:
        if entry.page_index is not None and entry.chapter:
            chapter_pages[entry.chapter] = min(
                chapter_pages.get(entry.chapter, entry.page_index), entry.page_index
            )
    for chapter, page_index in (bookmark_chapter_pages or {}).items():
        chapter_pages.setdefault(chapter, page_index)
    for entry in entries:
        if entry.page_index is None and entry.chapter in chapter_pages:
            entry.page_index = chapter_pages[entry.chapter]
            entry.confidence = min(entry.confidence, 0.85)

    offsets = [
        page_map[key] - int(key[2:])
        for key in page_map
        if key.startswith("n:") and 0 <= page_map[key] < n_pages
    ]
    offset = int(median(offsets)) if offsets else None

    last_page = 0
    for entry in entries:
        if entry.page_index is not None:
            last_page = max(last_page, entry.page_index)
            continue

        best: tuple[int, float] | None = None
        for page_index, heading in body_headings:
            if page_index < last_page:
                continue
            score = _best_heading_score(entry.title, heading)
            if best is None or score > best[1]:
                best = (page_index, score)
        if best is not None and best[1] >= 0.8:
            entry.page_index = best[0]
            entry.confidence = min(entry.confidence, 0.8)
            last_page = max(last_page, best[0])
            continue

        key = _printed_key(entry.printed_page or "")
        if key is not None and key.startswith("n:") and offset is not None:
            page_index = int(key[2:]) + offset
            if 0 <= page_index < n_pages:
                entry.page_index = page_index
                entry.confidence = min(entry.confidence, 0.7)
                last_page = max(last_page, page_index)

    # Last resort for the master chapter list: interpolate between mapped
    # neighbours so a chapter with no detail TOC is not silently dropped.
    master_entries = [entry for entry in entries if entry.is_master]
    for index, entry in enumerate(master_entries):
        if entry.page_index is not None:
            continue
        prev = next(
            (
                item
                for item in reversed(master_entries[:index])
                if item.page_index is not None
            ),
            None,
        )
        nxt = next(
            (
                item
                for item in master_entries[index + 1 :]
                if item.page_index is not None
            ),
            None,
        )
        if prev is None or nxt is None:
            # No evidence on both sides (e.g. appendix headings listed in
            # the master TOC but absent from the PDF). Do not borrow a
            # neighbour's page; the node stays unplaced.
            continue
        if nxt.page_index <= prev.page_index:
            entry.page_index = prev.page_index
        else:
            entry.page_index = prev.page_index + (
                nxt.page_index - prev.page_index
            ) // 2
        entry.page_index = max(0, min(entry.page_index, n_pages - 1))
        entry.confidence = min(entry.confidence, 0.6)

    # Detail entries without a page are noise and are dropped; master
    # entries (chapter/appendix roots) are kept so the TOC still lists a
    # section whose pages could not be located.
    return [
        entry
        for entry in entries
        if entry.page_index is not None or entry.is_master
    ]


def _chapter_keys(entries: list[TocEntry]) -> set[str]:
    return {entry.chapter for entry in entries if entry.chapter}


def heading_sort_key(num: str | None) -> tuple:
    if not num:
        return (9999,)
    text = num.replace("第", "").replace("章", "").strip()
    if re.fullmatch(r"\d+(?:\.\d+)*", text):
        return tuple(int(part) for part in text.split("."))
    match = re.fullmatch(r"([A-Z])((?:\.\d+)*)", text)
    if match:
        parts = [1000, ord(match.group(1)) - 64]
        if match.group(2):
            parts += [int(part) for part in match.group(2).lstrip(".").split(".")]
        return tuple(parts)
    match = re.fullmatch(
        r"(?:附录|Annex|Appendix)\s*([A-Za-z])", text, re.IGNORECASE
    )
    if match:
        return (1000, ord(match.group(1).upper()) - 64)
    value = cn_number_to_int(text)
    if value is not None:
        return (value,)
    return (9999,)


def _canon_text(value: str | None) -> str:
    return re.sub(r"\s+", "", value or "").lower()


def _repair_heading_numbers(
    entries: list[TocEntry], body_headings: list[tuple[int, str]]
) -> None:
    """Use body headings to fix a misnumbered printed-TOC entry.

    Only when the body heading is on the entry's mapped page, has exactly the
    same title (after whitespace normalization), and its number is not used by
    another entry. This fixes a printed TOC that numbers one subsection
    differently from the body.
    """
    by_page: dict[int, list[tuple[str, str, int]]] = {}
    for page_index, raw in body_headings:
        parsed = parse_heading(raw)
        if parsed and parsed[0]:
            num, title, level = parsed
            by_page.setdefault(page_index, []).append((num, title, level))

    def used_numbers() -> set[str]:
        return {
            _canon_text(entry.heading_num)
            for entry in entries
            if entry.heading_num
        }

    for entry in entries:
        used = used_numbers()
        if not entry.heading_num or entry.page_index is None:
            continue
        if entry.level <= 1 or "." not in entry.heading_num:
            # Chapter roots (第4章/附录A) are the skeleton; never retype them.
            continue
        parsed_entry = parse_heading(entry.title)
        if not parsed_entry:
            continue
        entry_title = _canon_text(parsed_entry[1])
        candidates = [
            (num, level)
            for page_index in (
                entry.page_index - 1,
                entry.page_index,
                entry.page_index + 1,
            )
            for num, title, level in by_page.get(page_index, [])
            if _canon_text(num) != _canon_text(entry.heading_num)
            and _canon_text(title) == entry_title
        ]
        if len(candidates) != 1:
            continue
        new_num, new_level = candidates[0]
        if chapter_key(new_num) != chapter_key(entry.heading_num):
            # Never let an OCR-damaged number (167.1.2 for 16.7.1.2) move a
            # section into a different chapter.
            continue
        if _canon_text(new_num) in used:
            continue
        entry.heading_num = new_num
        entry.level = new_level
        entry.title = f"{new_num} {parsed_entry[1]}".strip()

    # Second pass: a printed TOC can repeat a number for two different
    # titles while the body uses different numbers. If the body has exactly
    # one heading with the same title under the same parent prefix, adopt
    # its number.
    groups: dict[str, list[TocEntry]] = {}
    for entry in entries:
        if entry.heading_num and "." in entry.heading_num:
            groups.setdefault(_canon_text(entry.heading_num), []).append(entry)
    for group in groups.values():
        if len({entry.title for entry in group}) <= 1:
            continue
        for entry in group:
            used = used_numbers()
            parsed_entry = parse_heading(entry.title)
            if not parsed_entry or not entry.heading_num:
                continue
            entry_title = _canon_text(parsed_entry[1])
            parent_num = entry.heading_num.rsplit(".", 1)[0]
            candidates: list[tuple[str, int]] = []
            for _, raw in body_headings:
                parsed = parse_heading(raw)
                if not parsed or not parsed[0]:
                    continue
                num, title, level = parsed
                if _canon_text(num) == _canon_text(entry.heading_num):
                    continue
                if num.rsplit(".", 1)[0] != parent_num:
                    continue
                if _canon_text(title) != entry_title:
                    continue
                candidates.append((num, level))
            unique = list(dict.fromkeys(candidates))
            if len(unique) != 1:
                continue
            new_num, new_level = unique[0]
            if _canon_text(new_num) in used:
                continue
            entry.heading_num = new_num
            entry.level = new_level
            entry.title = f"{new_num} {parsed_entry[1]}".strip()


def _entry_title_key(entry: TocEntry) -> str:
    parsed = parse_heading(entry.title)
    return _normalize_title(parsed[1] if parsed else entry.title)


def _title_similarity(left: str, right: str) -> float:
    """Character-level similarity that works for short Chinese titles.

    difflib alone scores 校准确认/校准验证 at 0.5, so combine it with the
    overlap coefficient over character sets.
    """
    if left in right or right in left:
        return 1.0
    if not left or not right:
        return 0.0
    overlap = len(set(left) & set(right)) / min(len(set(left)), len(set(right)))
    return max(_similarity(left, right), overlap)


def _same_section(left: TocEntry, right: TocEntry) -> bool:
    """Whether two entries are the same section under slightly different titles."""
    if not left.heading_num or not right.heading_num:
        return False
    if (
        re.sub(r"\s+", "", left.heading_num).lower()
        != re.sub(r"\s+", "", right.heading_num).lower()
    ):
        return False
    a, b = _entry_title_key(left), _entry_title_key(right)
    if not a or not b:
        return False
    return _title_similarity(a, b) >= 0.5


def _looks_like_body_line(title: str) -> bool:
    """A TOC title never ends a sentence or runs half a paragraph.

    Wrapped body text under a heading number ("2.2 所示。它是在低压条件
    下运行…") is a text-layer artifact, not a second section with the same
    number; it must lose to the real heading that shares its number.
    """
    return bool(title) and (
        title[-1] in "。？！；，、"
        or "。" in title
        or len(title) > 60
    )


def _dedupe_entries(entries: list[TocEntry]) -> list[TocEntry]:
    """Merge near-identical entries that share a heading number.

    Printed TOCs and PDF outlines often disagree by a word or two
    (确认/验证, 范围/适用范围). Keeping both creates duplicate sibling
    numbers and splits one section's content across two nodes. Entries with
    genuinely different titles (e.g. 计时器/测量尺 under the same number)
    are kept as-is — unless one of them reads like a wrapped body line, in
    which case the sentence-shaped one is the artifact and is dropped.
    """
    # Pass 1: drop body-line artifacts that collide with a real heading on
    # the same number, regardless of title similarity.
    by_number: dict[str, list[int]] = {}
    for index, entry in enumerate(entries):
        if entry.heading_num:
            key = re.sub(r"\s+", "", entry.heading_num).lower()
            by_number.setdefault(key, []).append(index)
    dropped: set[int] = set()
    for indexes in by_number.values():
        if len(indexes) < 2:
            continue
        clean = [i for i in indexes if not _looks_like_body_line(entries[i].title)]
        if clean and len(clean) < len(indexes):
            dropped.update(i for i in indexes if i not in clean)

    kept: list[TocEntry] = []
    used: set[int] = set()
    for index, entry in enumerate(entries):
        if index in used or index in dropped:
            continue
        group = [index]
        for other_index in range(index + 1, len(entries)):
            if other_index in used or other_index in dropped:
                continue
            if _same_section(entry, entries[other_index]):
                group.append(other_index)
        used.update(group)
        if len(group) == 1:
            kept.append(entry)
            continue
        best = max(
            group,
            key=lambda item: (
                entries[item].confidence,
                1 if entries[item].page_index is not None else 0,
                -entries[item].order,
            ),
        )
        kept.append(entries[best])
    return kept


def _continuation_entries(page: OcrPage) -> list[TocEntry]:
    """Entries for a TOC continuation page the strict parser cannot read.

    A master TOC often runs past the page that carries the "目录" marker.
    Continuation pages lose the dot leaders (or the text layer mangles
    them), so ``parse_toc_page`` finds nothing and the rest of the TOC is
    silently dropped. Accept a line as a continuation entry when it starts
    with a heading number and ends with a printed page label — the two
    properties every TOC line has and no body line combines.
    """
    entries: list[TocEntry] = []
    for block in _ordered_blocks(page):
        for line in block.content.splitlines():
            raw = line.strip()
            if not raw or len(raw) > 120:
                continue
            parsed = parse_heading(raw)
            if parsed is None or parsed[0] is None:
                continue
            num, title, _ = parsed
            if not title:
                continue
            match = _TRAILING_PAGE_LABEL.search(raw)
            if match is None:
                continue
            printed = match.group("page")
            if _printed_key(printed) is None:
                continue
            # Strip the leader and page label from the title.
            title = raw[: match.start()].rstrip(" #＃.。·…．-－—–、")
            stripped = parse_heading(title)
            if stripped is not None and stripped[1]:
                num, title = stripped[0] or num, stripped[1]
            if not title or not any(char.isalpha() for char in title):
                continue  # digit-only residue of a merged TOC line
            entries.append(
                TocEntry(
                    title=title,
                    heading_num=num,
                    level=max(1, min(6, parsed[2])),
                    printed_page=printed,
                    page_index=None,
                    confidence=0.75,
                    order=len(entries),
                )
            )
    return entries


def _collect_toc_pages(
    pages: list[OcrPage],
) -> list[tuple[int, list[TocEntry]]]:
    if not pages or all(page.width <= 0 for page in pages):
        return []
    early_limit = max(10, int(len(pages) * 0.2))
    toc_pages: list[tuple[int, list[TocEntry]]] = []
    seen: set[int] = set()
    for page in pages:
        page_entries = parse_toc_page(page)
        if _looks_like_toc_page(
            page, page_entries, require_marker=page.page_index >= early_limit
        ):
            toc_pages.append((page.page_index, page_entries))
            seen.add(page.page_index)
    # A master TOC that spills onto the next page usually loses its marker
    # and its leaders together. Give the page right after a detected TOC page
    # one relaxed chance: enough numbered-lines-with-page-labels means it is
    # part of the same TOC, not body text (body sentences end with 。, not a
    # page number).
    for page in pages:
        if page.page_index in seen or page.page_index - 1 not in seen:
            continue
        if not _looks_like_toc_page(page, parse_toc_page(page), require_marker=False):
            entries = _continuation_entries(page)
            if len(entries) >= 3:
                toc_pages.append((page.page_index, entries))
                seen.add(page.page_index)
    return sorted(toc_pages, key=lambda item: item[0])


def toc_page_indices(pages: list[OcrPage]) -> set[int]:
    """Every page that looks like a printed TOC, chapter TOCs included."""
    return {page_index for page_index, _ in _collect_toc_pages(pages)}


def detect_printed_toc(parsed: ParsedDocument) -> PrintedToc | None:
    """Find every printed TOC page and merge chapter TOCs under the master.

    The earliest TOC that spans multiple top-level chapters is the master
    skeleton. Later TOC pages are chapter/detail lists: their entries are
    attached under the matching chapter and never replace master entries.
    """
    pages = parsed.pages
    toc_pages = _collect_toc_pages(pages)
    if not toc_pages:
        return None

    single_chapter_pages: list[tuple[int, str]] = []
    for page_index, entries in toc_pages:
        keys = _chapter_keys(entries)
        if len(keys) == 1 and len(entries) >= 1:
            single_chapter_pages.append((page_index, next(iter(keys))))
    single_chapter_pages.sort()

    candidates = [
        (page_index, entries, _chapter_keys(entries))
        for page_index, entries in toc_pages
        if len(_chapter_keys(entries)) >= 2
    ]
    master_pages: set[int] = set()
    if candidates:
        by_page = {
            page_index: (entries, keys)
            for page_index, entries, keys in candidates
        }
        # A long master TOC can span consecutive pages. Take the whole run
        # around the strongest page, otherwise chapters printed on a
        # continuation page lose their level-1 roots.
        master_page = max(
            candidates, key=lambda item: (len(item[2]), -item[0])
        )[0]
        first = last = master_page
        while first - 1 in by_page:
            first -= 1
        while last + 1 in by_page:
            last += 1
        master_pages = set(range(first, last + 1))
        master_entries = [
            entry
            for page_index in sorted(master_pages)
            for entry in by_page[page_index][0]
        ]
        master_keys: set[str] = set()
        for page_index in master_pages:
            master_keys |= by_page[page_index][1]
    else:
        # A page full of unnumbered lines is usually a "总目录" listing
        # other documents in a series, not this document's TOC. Prefer a
        # page with at least one numbered entry; otherwise fall back to
        # body headings/bookmarks.
        numbered_pages = [
            item
            for item in toc_pages
            if any(entry.heading_num for entry in item[1])
        ]
        if not numbered_pages:
            return None
        master_page, master_entries = max(
            numbered_pages, key=lambda item: len(item[1])
        )
        master_keys = _chapter_keys(master_entries)
        master_pages = {master_page}

    for entry in master_entries:
        entry.is_master = True

    filtered_bookmarks = filter_bookmarks(parsed.bookmarks)
    merged: list[TocEntry] = list(master_entries)
    for page_index, entries in toc_pages:
        if page_index in master_pages:
            continue
        for entry in entries:
            # Only the master page owns level-1 chapter nodes; later chapter
            # TOCs contribute children and must not create duplicate roots.
            if entry.level <= 1:
                continue
            if master_keys and entry.chapter and entry.chapter not in master_keys:
                continue
            merged.append(entry)

    # The PDF outline often carries detail that later chapter TOCs lost
    # (or that the text layer could not read); attach it as extra children.
    # Chapter spans from the outline itself bound what a bookmark may point
    # at: a phantom outline entry that carries table cells as bookmarks
    # targets a page far outside its own chapter and is dropped.
    bookmark_chapter_pages: dict[str, int] = {}
    for bookmark in filtered_bookmarks:
        parsed_bookmark = parse_heading(bookmark.title)
        if parsed_bookmark is None:
            continue
        chapter = chapter_key(parsed_bookmark[0])
        if chapter:
            bookmark_chapter_pages[chapter] = min(
                bookmark_chapter_pages.get(chapter, bookmark.page_index),
                bookmark.page_index,
            )

    def _chapter_span(chapter: str) -> tuple[int, int] | None:
        own = bookmark_chapter_pages.get(chapter)
        if own is None:
            return None
        later = [
            start
            for key, start in bookmark_chapter_pages.items()
            if key != chapter and start > own
        ]
        return (own, min(later)) if later else None

    seen_bookmark_keys: set[tuple[str, int]] = set()
    for bookmark in filtered_bookmarks:
        parsed_bookmark = parse_heading(bookmark.title)
        if parsed_bookmark is None:
            continue
        num, _, level = parsed_bookmark
        chapter = chapter_key(num)
        if not chapter or (master_keys and chapter not in master_keys):
            continue
        span = _chapter_span(chapter)
        if span is not None and not (span[0] <= bookmark.page_index < span[1]):
            continue
        if level <= 1:
            continue
        key = (_normalize_title(bookmark.title), bookmark.page_index)
        if key in seen_bookmark_keys:
            continue
        seen_bookmark_keys.add(key)
        merged.append(
            TocEntry(
                title=bookmark.title,
                heading_num=num,
                level=level,
                printed_page=None,
                page_index=bookmark.page_index,
                confidence=0.85,
                order=100000 + len(merged),
            )
        )

    if len(merged) < 2:
        return None

    toc_page_indices = {page_index for page_index, _ in toc_pages}
    context = build_mapping_context(pages, toc_page_indices, single_chapter_pages)

    mapped = _map_entries(
        merged,
        pages,
        context.page_map,
        context.body_headings,
        bookmark_chapter_pages=bookmark_chapter_pages,
        chapter_toc_ranges=context.chapter_toc_ranges,
    )
    _repair_heading_numbers(mapped, context.body_headings)
    if len(mapped) < 2:
        return None

    seen: set[tuple[str, int]] = set()
    unique: list[TocEntry] = []
    # ``merged`` lists masters, then chapter TOCs, then outline children —
    # each group already in reading order. Renumber ``order`` sequentially so
    # it is the global reading order and sort by it: sorting by resolved page
    # scrambles the tree whenever one page mapping is off (a section jumps
    # ahead of an earlier sibling that was mapped to the same page, and an
    # unmapped entry lands in front of everything via ``page_index or 0``).
    for position, entry in enumerate(mapped):
        entry.order = position
    for entry in sorted(mapped, key=lambda e: e.order):
        key = (_normalize_title(entry.title), entry.page_index or 0)
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)

    unique = _dedupe_entries(unique)

    if candidates and context.page_map:
        confidence = 0.9
    elif candidates:
        confidence = 0.8
    elif context.page_map:
        confidence = 0.75
    else:
        confidence = 0.7
    return PrintedToc(
        entries=unique,
        source_page=master_page,
        confidence=confidence,
    )