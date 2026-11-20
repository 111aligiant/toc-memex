from __future__ import annotations

import re

from ..identifiers import is_bare_designation
from ..ocr.models import Bookmark
from .heading import parse_heading

# Header/footer/address noise: URLs and bare document-number lines.
_PAGE_FURNITURE = re.compile(r"https?://|www\.", re.IGNORECASE)

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
    """
    result: list[Bookmark] = []
    seen: set[tuple[str, int]] = set()

    for bookmark in bookmarks:
        title = " ".join((bookmark.title or "").split())
        if (
            not title
            or len(title) > 200
            or _PAGE_FURNITURE.search(title)
            or is_bare_designation(title)
        ):
            continue

        parsed = parse_heading(title)
        if parsed is None:
            continue
        num, _, level = parsed
        if num is None and "".join(title.split()) not in _KEEP_UNNUMBERED_COMPACT:
            continue

        key = (title, bookmark.page_index)
        if key in seen:
            continue
        seen.add(key)
        result.append(
            Bookmark(
                level=level if num else 1,
                title=title,
                page_index=max(0, bookmark.page_index),
                heading_num=num,
            )
        )

    # Stable page-only sort: PDF outlines already list same-page headings in
    # reading order. Sorting by level would hoist the next chapter above the
    # previous chapter's subsections (4.6 before 4.5.1 on the same page).
    result.sort(key=lambda item: item.page_index)
    return _synthesize_appendix_roots(result)
