from __future__ import annotations

import re

from ..identifiers import is_bare_designation
from ..ocr.models import Bookmark
from .heading import parse_heading

# Header/footer/address noise: URLs and bare document-number lines.
_PAGE_FURNITURE = re.compile(r"https?://|www\.", re.IGNORECASE)

# Auto-generated outlines that bookmark every page: "Page 3", "第 12 页",
# "Seite 4". A title that is only a page label carries no structure.
_PAGE_LABEL = re.compile(
    r"^(?:page|p\.?|seite|página|página|页|第)\s*[0-9０-９]+\s*页?$", re.IGNORECASE
)

# Sentence punctuation and math notation never appear in a real heading.
# Outlines auto-built by authoring tools bookmark displayed equations and
# body lines too ("2Al + N2 -> 2AlN (2-1)", "…，无定形"); a title carrying
# any of these marks is one of them, not a section.
_NON_HEADING_MARKS = tuple("。，：；＝→←＿＾+*\"“”‘’?!？!")

# Legitimate unnumbered sections worth keeping from a PDF outline.
_KEEP_UNNUMBERED = {
    "前言",
    "引言",
    "参考文献",
    "索引",
    "术语和定义",
    "范围",
    "缩略语",
    "附录",
}
_KEEP_UNNUMBERED_COMPACT = {"".join(title.split()) for title in _KEEP_UNNUMBERED}


_APPENDIX_SUBSECTION = re.compile(r"^([A-Z])\.\d+")


def _synthesize_appendix_roots(bookmarks: list[Bookmark]) -> list[Bookmark]:
    """Add an ``附录A`` root when the outline only has ``A.1``, ``A.2``...

    Word-exported outlines sometimes bookmark the appendix subsections but
    not the ``附录 A`` heading (its text is split from the letter). Without a
    root, PageIndex attaches A.x to the previous chapter, so chapter 7's
    page range swallows the appendices.
    """
    existing = {
        bookmark.heading_num
        for bookmark in bookmarks
        if bookmark.heading_num and bookmark.level == 1
    }
    first_pages: dict[str, int] = {}
    for bookmark in bookmarks:
        match = _APPENDIX_SUBSECTION.match(bookmark.heading_num or "")
        if not match:
            continue
        letter = match.group(1)
        if letter in existing or f"附录{letter}" in existing:
            continue
        first_pages[letter] = min(
            first_pages.get(letter, bookmark.page_index), bookmark.page_index
        )
    if not first_pages:
        return bookmarks

    out: list[Bookmark] = []
    added: set[str] = set()
    for bookmark in bookmarks:
        match = _APPENDIX_SUBSECTION.match(bookmark.heading_num or "")
        if match:
            letter = match.group(1)
            if letter in first_pages and letter not in added:
                out.append(
                    Bookmark(
                        level=1,
                        title=f"附录{letter}",
                        page_index=first_pages[letter],
                        heading_num=f"附录{letter}",
                    )
                )
                added.add(letter)
        out.append(bookmark)
    for letter, page_index in sorted(first_pages.items(), key=lambda item: item[1]):
        if letter not in added:
            out.append(
                Bookmark(
                    level=1,
                    title=f"附录{letter}",
                    page_index=page_index,
                    heading_num=f"附录{letter}",
                )
            )
    return out


def filter_bookmarks(bookmarks: list[Bookmark]) -> list[Bookmark]:
    """Drop outline junk and infer heading levels from the title numbering.

    A PDF outline often lists running headers and bare document-number
    lines, and every entry may be level 1. Keeping only numbered headings
    and re-leveling them (8.3 → level 2) turns the outline into a usable
    tree.

    Exception: entries the authoring tool wrote itself
    (``Bookmark.from_outline``) keep unnumbered titles ("Introduction",
    "Abstract") with their authored level — dropping them would leave an
    English paper with no tree at all. Their junk is handled structurally:
    sentence/math marks and page-label titles ("Page 12") are dropped.

    Running headers are dropped for both sources: a title (spacing
    normalized) that appears on three or more distinct pages is page
    furniture, not a heading — thesis templates bookmark the running
    "第一章 绪论" line on every page.
    """
    pages_by_title: dict[str, set[int]] = {}
    for bookmark in bookmarks:
        key = "".join((bookmark.title or "").split())
        if key:
            pages_by_title.setdefault(key, set()).add(bookmark.page_index)
    running_headers = {
        key for key, pages in pages_by_title.items() if len(pages) >= 3
    }

    result: list[Bookmark] = []
    seen: set[tuple[str, int]] = set()
    outline_titles: set[str] = set()

    for bookmark in bookmarks:
        title = " ".join((bookmark.title or "").split())
        if (
            not title
            or len(title) > 200
            or "".join(title.split()) in running_headers
            or _PAGE_FURNITURE.search(title)
            or is_bare_designation(title)
            or (bookmark.from_outline and _PAGE_LABEL.fullmatch(title))
            or (bookmark.from_outline and any(mark in title for mark in _NON_HEADING_MARKS))
        ):
            continue

        parsed = parse_heading(title)
        if parsed is None:
            if not bookmark.from_outline:
                continue
            num, _, level = None, title, max(1, min(6, bookmark.level))
        else:
            num, _, level = parsed
            if num is None:
                if bookmark.from_outline:
                    level = max(1, min(6, bookmark.level))
                elif "".join(title.split()) not in _KEEP_UNNUMBERED_COMPACT:
                    continue
                else:
                    level = 1

        if bookmark.from_outline:
            # An authored outline lists each section once; a spaced variant
            # of a title already seen is the same section twice.
            norm = "".join(title.split())
            if norm in outline_titles:
                continue
            outline_titles.add(norm)
        else:
            key = (title, bookmark.page_index)
            if key in seen:
                continue
            seen.add(key)
        result.append(
            Bookmark(
                level=level,
                title=title,
                page_index=max(0, bookmark.page_index),
                heading_num=num,
                from_outline=bookmark.from_outline,
            )
        )

    # Stable page-only sort: PDF outlines already list same-page headings in
    # reading order. Sorting by level would hoist the next chapter above the
    # previous chapter's subsections (4.6 before 4.5.1 on the same page).
    result.sort(key=lambda item: item.page_index)
    return _synthesize_appendix_roots(result)
