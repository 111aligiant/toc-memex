from __future__ import annotations

from app.ingest.bookmarks import filter_bookmarks
from app.ocr.models import Bookmark


def test_filter_drops_document_number_headers():
    bookmarks = [
        Bookmark(1, "ACME／100-2020", 0),
        Bookmark(1, "DOC 9001", 0),
        Bookmark(1, "1 范围", 1),
    ]
    result = filter_bookmarks(bookmarks)
    assert [bookmark.title for bookmark in result] == ["1 范围"]


def test_filter_synthesizes_missing_appendix_roots():
    bookmarks = [
        Bookmark(2, "A.1 冲击", 56),
        Bookmark(2, "A.2 冲击有效持续时间", 57),
        Bookmark(2, "B.1 范围", 65),
    ]
    result = filter_bookmarks(bookmarks)
    titles = [bookmark.title for bookmark in result]
    assert titles == ["附录A", "A.1 冲击", "A.2 冲击有效持续时间", "附录B", "B.1 范围"]
    assert result[0].level == 1
    assert result[0].page_index == 56
    assert result[3].level == 1
    assert result[3].page_index == 65


def test_filter_keeps_unnumbered_titles_with_spaces():
    bookmarks = [Bookmark(1, "前 言", 2), Bookmark(1, "引 言", 3)]
    result = filter_bookmarks(bookmarks)
    assert [bookmark.title for bookmark in result] == ["前 言", "引 言"]


def test_filter_keeps_numbered_title_without_space():
    bookmarks = [Bookmark(1, "4.0温度－高度", 14)]
    result = filter_bookmarks(bookmarks)
    assert len(result) == 1
    assert result[0].heading_num == "4.0"
    assert result[0].title == "4.0温度－高度"
    assert result[0].level == 2


def test_filter_keeps_titles_containing_junk_substrings():
    bookmarks = [
        Bookmark(1, "4.5.1 Inclined Test", 10),
        Bookmark(1, "4.2 Usage Limits", 11),
        Bookmark(1, "5.1 Principle of operation", 12),
        Bookmark(1, "6.1 Telephone Equipment", 13),
    ]
    result = filter_bookmarks(bookmarks)
    assert [bookmark.title for bookmark in result] == [
        "4.5.1 Inclined Test",
        "4.2 Usage Limits",
        "5.1 Principle of operation",
        "6.1 Telephone Equipment",
    ]


def test_filter_drops_running_headers_and_relevels_by_numbering():
    bookmarks = [
        Bookmark(1, "https://example.com/doc", 1),
        Bookmark(1, "ACME-2024", 1),
        Bookmark(1, "前言", 3),
        Bookmark(1, "4.0 温度－高度", 14),
        Bookmark(1, "8.3 方法要求", 48),
    ]
    result = filter_bookmarks(bookmarks)
    assert [bookmark.title for bookmark in result] == [
        "前言",
        "4.0 温度－高度",
        "8.3 方法要求",
    ]
    assert [bookmark.level for bookmark in result] == [1, 2, 2]
    assert [bookmark.heading_num for bookmark in result] == [None, "4.0", "8.3"]