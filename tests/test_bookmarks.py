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

# ── authored PDF outlines (Bookmark.from_outline) ──────────────────────


def test_outline_keeps_unnumbered_titles_with_authored_level():
    """An English paper's outline has no numbers; authored levels still tree."""
    bookmarks = [
        Bookmark(1, "Nitridation reaction of aluminum powder", 0, from_outline=True),
        Bookmark(2, "Introduction", 0, from_outline=True),
        Bookmark(2, "Thermodynamic considerations", 1, from_outline=True),
        Bookmark(3, "Reactor design", 2, from_outline=True),
    ]
    result = filter_bookmarks(bookmarks)
    assert [bookmark.title for bookmark in result] == [
        "Nitridation reaction of aluminum powder",
        "Introduction",
        "Thermodynamic considerations",
        "Reactor design",
    ]
    assert [bookmark.level for bookmark in result] == [1, 2, 2, 3]


def test_outline_drops_equation_and_sentence_bookmarks():
    """Auto-added equation/body bookmarks never make a heading."""
    bookmarks = [
        Bookmark(1, "1 引言", 10, from_outline=True),
        Bookmark(1, "2Al + N2 -> 2AlN (2-1)", 15, from_outline=True),
        Bookmark(1, "J 混介状态。随着溶剂的蒸发以及燃烧反应", 58, from_outline=True),
        Bookmark(1, "9 又 1 2 0 Q T：", 58, from_outline=True),
    ]
    result = filter_bookmarks(bookmarks)
    assert [bookmark.title for bookmark in result] == ["1 引言"]


def test_outline_drops_page_label_bookmarks():
    bookmarks = [
        Bookmark(1, "Page 3", 2, from_outline=True),
        Bookmark(1, "第 12 页", 11, from_outline=True),
        Bookmark(1, "1 范围", 12, from_outline=True),
    ]
    result = filter_bookmarks(bookmarks)
    assert [bookmark.title for bookmark in result] == ["1 范围"]


def test_running_headers_repeating_on_many_pages_are_dropped():
    """Thesis templates bookmark the running header line on every page."""
    bookmarks = [
        Bookmark(1, "第 一 章 绪论", page)
        for page in range(11, 26, 2)
    ] + [Bookmark(1, "1.1 引言", 11), Bookmark(1, "1.2 压电效应", 13)]
    result = filter_bookmarks(bookmarks)
    assert [bookmark.title for bookmark in result] == ["1.1 引言", "1.2 压电效应"]


def test_outline_title_repeated_with_spacing_variant_is_kept_once():
    """Two spaced variants of one outline title are the same section."""
    bookmarks = [
        Bookmark(1, "摘要", 4, from_outline=True),
        Bookmark(1, "摘 要", 5, from_outline=True),
        Bookmark(1, "1 范围", 30, from_outline=True),
    ]
    result = filter_bookmarks(bookmarks)
    assert [bookmark.title for bookmark in result] == ["摘要", "1 范围"]
    assert result[0].page_index == 4


def test_unnumbered_title_repeated_on_two_pages_is_kept():
    """Two occurrences can be legitimate re-entries ("引言" per chapter)."""
    bookmarks = [
        Bookmark(1, "引言", 8),
        Bookmark(1, "引 言", 25),
        Bookmark(1, "1 范围", 30),
    ]
    result = filter_bookmarks(bookmarks)
    assert [bookmark.title for bookmark in result] == ["引言", "引 言", "1 范围"]


def test_outline_keeps_prose_title_with_plus():
    """`+` alone is prose, not a formula: ReAct's §2 title must survive."""
    bookmarks = [
        Bookmark(
            1,
            "2 ReAct: Synergizing Reasoning + Acting",
            2,
            from_outline=True,
        ),
        Bookmark(1, "3 Tasks", 3, from_outline=True),
    ]
    result = filter_bookmarks(bookmarks)
    assert [bookmark.title for bookmark in result] == [
        "2 ReAct: Synergizing Reasoning + Acting",
        "3 Tasks",
    ]


def test_outline_keeps_english_terminal_sections():
    bookmarks = [
        Bookmark(1, "Conclusion", 9, from_outline=True),
        Bookmark(1, "References", 10, from_outline=False),
        Bookmark(1, "ACKNOWLEDGMENTS", 12, from_outline=False),
    ]
    result = filter_bookmarks(bookmarks)
    assert [bookmark.title for bookmark in result] == [
        "Conclusion",
        "References",
        "ACKNOWLEDGMENTS",
    ]
