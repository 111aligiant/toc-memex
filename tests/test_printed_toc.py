from __future__ import annotations

from app.ingest.heading import chapter_key
from app.ingest.printed_toc import (
    TocEntry,
    _dedupe_entries,
    _make_entry,
    _printed_key,
    _repair_heading_numbers,
    heading_sort_key,
)
from app.ingest.toc import build_toc
from app.ocr.models import Bookmark, OcrBlock, OcrPage, ParsedDocument


def page(index: int, *blocks: OcrBlock, width: int = 1200, height: int = 1600) -> OcrPage:
    return OcrPage(index, width, height, "", list(blocks), {}, {})


def text(content: str, y: int, x: int = 100) -> OcrBlock:
    return OcrBlock("text", content, (x, y, x + 500, y + 20), None)


def number(content: str, y: int = 1500) -> OcrBlock:
    return OcrBlock("number", content, (580, y, 620, y + 20), None)


def title(content: str, y: int) -> OcrBlock:
    return OcrBlock("paragraph_title", content, (100, y, 600, y + 24), None)


def test_chapter_local_page_beyond_chapter_range_falls_back_to_heading():
    parsed = ParsedDocument(
        pages=[
            page(0, text("封面", 100)),
            page(
                1,
                text("目录", 100),
                text("第14章 甲 ..... 1", 200),
                text("第15章 乙 ..... 2", 240),
            ),
            page(2, text("目录", 100), text("14.1 子 ..... 14-99", 200)),
            page(3, number("1"), title("第14章 甲", 100), title("14.1 子", 300)),
            page(4, number("2"), title("第15章 乙", 100)),
        ],
        bookmarks=[],
        metadata={},
    )
    nodes = build_toc("d1", parsed)
    by_title = {node.title: node for node in nodes}
    assert by_title["14.1 子"].page_start == 3


def test_multi_page_chapter_toc_uses_last_page_as_start():
    parsed = ParsedDocument(
        pages=[
            page(0, text("封面", 100)),
            page(
                1,
                text("目录", 100),
                text("第1章 甲 ..... 1", 200),
                text("第2章 乙 ..... 2", 240),
            ),
            page(2, text("目录", 100), text("1.0 子零 ..... 1-1", 200)),
            page(3, text("目录", 100), text("1.5 子五 ..... 1-5", 200)),
            page(4, number("1"), title("第1章 甲", 100)),
            page(5, number("1-1"), title("1.0 子零", 100)),
            page(6, number("1-5"), title("1.5 子五", 100)),
            page(7, number("2"), title("第2章 乙", 100)),
        ],
        bookmarks=[],
        metadata={},
    )
    nodes = build_toc("d1", parsed)
    by_title = {node.title: node for node in nodes}
    assert by_title["1.0 子零"].page_start == 5
    assert by_title["1.5 子五"].page_start == 6


def test_document_running_header_is_not_a_toc_entry():
    assert _make_entry("ACME／100-2020 ..... 5", 100, 0.9) is None
    assert _make_entry("DOC 61000-4-2 ..... 1", 100, 0.9) is None


def test_appendix_page_label_is_parsed_and_keyed():
    entry = _make_entry("A.1 App Detail ..... A-1", 100, 0.9)
    assert entry is not None
    assert entry.heading_num == "A.1"
    assert entry.printed_page == "A-1"
    assert _printed_key("A-1") == "h:A-1"


def test_appendix_detail_pages_using_letter_hyphen_labels():
    parsed = ParsedDocument(
        pages=[
            page(0, text("封面", 100)),
            page(
                1,
                text("目录", 100),
                text("第1章 甲 ..... 1", 200),
                text("附录 A 甲录 ..... A-1", 240),
            ),
            page(2, text("目录", 100), text("A.1 明细 ..... A-1", 200)),
            page(3, number("1"), title("第1章 甲", 100)),
            page(
                4,
                number("A-1"),
                title("附录 A 甲录", 100),
                title("A.1 明细", 300),
            ),
        ],
        bookmarks=[],
        metadata={},
    )
    nodes = build_toc("d1", parsed)
    by_title = {node.title: node for node in nodes}
    assert by_title["A.1 明细"].page_start == 4
    assert by_title["A.1 明细"].parent_id == by_title["附录A 甲录"].node_id


def test_repair_heading_numbers_uses_same_page_body_heading():
    entries = [TocEntry("9.4.1 A 类设备", "9.4.1", 3, "9-1", 1, 0.9)]
    _repair_heading_numbers(entries, [(1, "9.3.1 A 类设备")])
    assert entries[0].heading_num == "9.3.1"
    assert entries[0].level == 3


def test_repair_heading_numbers_does_not_cross_chapters():
    entries = [
        TocEntry("16.7.1.2 电流畸变", "16.7.1.2", 4, "16-3", 123, 0.85)
    ]
    _repair_heading_numbers(entries, [(123, "167.1.2 电流畸变")])
    assert entries[0].heading_num == "16.7.1.2"


def test_repair_resolves_duplicate_number_from_body_sibling():
    entries = [
        TocEntry("9.7.2.2 试验实施", "9.7.2.2", 4, "9-6", 68, 0.9),
        TocEntry("9.7.2.2 失效判据", "9.7.2.2", 4, "9-6", 68, 0.9),
    ]
    _repair_heading_numbers(
        entries,
        [(65, "9.7.2.2 试验实施"), (66, "9.7.2.3 失效判据")],
    )
    assert entries[0].heading_num == "9.7.2.2"
    assert entries[1].heading_num == "9.7.2.3"


def test_repair_heading_numbers_skips_a_number_already_in_use():
    entries = [
        TocEntry("9.4.1 A 类设备", "9.4.1", 3, "9-1", 1, 0.9),
        TocEntry("9.3.1 其他", "9.3.1", 3, "9-1", 1, 0.9),
    ]
    _repair_heading_numbers(entries, [(1, "9.3.1 A 类设备")])
    assert entries[0].heading_num == "9.4.1"


def test_dedupe_merges_similar_titles_with_the_same_number():
    entries = [
        TocEntry("1.1 校准确认", "1.1", 2, "1", 2, 0.9),
        TocEntry("1.1 校准验证", "1.1", 2, "1", 3, 0.85),
    ]
    result = _dedupe_entries(entries)
    assert len(result) == 1
    assert result[0].title == "1.1 校准确认"


def test_dedupe_keeps_genuinely_different_titles_with_the_same_number():
    entries = [
        TocEntry("26.6.4.2.4 计时器", "26.6.4.2.4", 5, "26-13", 300, 0.85),
        TocEntry("26.6.4.2.4 测量尺", "26.6.4.2.4", 5, "26-13", 301, 0.85),
    ]
    result = _dedupe_entries(entries)
    assert [entry.title for entry in result] == [
        "26.6.4.2.4 计时器",
        "26.6.4.2.4 测量尺",
    ]


def test_chapter_key_recognizes_appendix_and_chinese_numerals():
    assert chapter_key("附录A") == "A"
    assert chapter_key("第一章") == "1"
    assert chapter_key("第二十六章") == "26"
    assert chapter_key("第14章") == "14"
    assert chapter_key("14.1") == "14"
    assert chapter_key("A.1") == "A"


def test_heading_sort_key_orders_chinese_chapters():
    assert heading_sort_key("第一章") == (1,)
    assert heading_sort_key("第二十六章") == (26,)
    assert heading_sort_key("附录A") == (1000, 1)


def test_chinese_master_chapter_keeps_detail_entries():
    """Chinese-numbered master keys must match Arabic detail chapters."""
    parsed = ParsedDocument(
        pages=[
            page(0, text("封面", 100)),
            page(
                1,
                text("目录", 100),
                text("第一章 甲 ..... 1", 200),
                text("第二章 乙 ..... 2", 240),
            ),
            page(2, text("目录", 100), text("1.1 子 ..... 1-1", 200)),
            page(3, number("1"), title("第一章 甲", 100)),
            page(4, number("1-1"), title("1.1 子", 100)),
            page(5, number("2"), title("第二章 乙", 100)),
        ],
        bookmarks=[],
        metadata={},
    )
    nodes = build_toc("d1", parsed)
    by_title = {node.title: node for node in nodes}
    assert by_title["1.1 子"].parent_id == by_title["第一章 甲"].node_id


def test_spaced_toc_line_without_dots_is_parsed():
    parsed = ParsedDocument(
        pages=[
            page(
                0,
                text("目录", 100),
                text("前言     2", 200),
                text("1 范围     1", 240),
            ),
            page(1, number("2"), title("前言", 100)),
            page(2, number("1"), title("1 范围", 100)),
        ],
        bookmarks=[],
        metadata={},
    )
    nodes = build_toc("d1", parsed)
    by_title = {node.title: node for node in nodes}
    assert nodes[0].source == "toc_page"
    assert by_title["前言"].page_start == 1
    assert by_title["1 范围"].page_start == 2


def test_two_character_ellipsis_leaders_are_parsed():
    parsed = ParsedDocument(
        pages=[
            page(
                0,
                text("目录", 100),
                text("1 范围 …… 1", 200),
                text("2 术语 .. 2", 240),
            ),
            page(1, number("1"), title("1 范围", 100)),
            page(2, number("2"), title("2 术语", 100)),
        ],
        bookmarks=[],
        metadata={},
    )
    nodes = build_toc("d1", parsed)
    assert [node.title for node in nodes] == ["1 范围", "2 术语"]


def test_out_of_order_printed_toc_yields_to_bookmarks():
    parsed = ParsedDocument(
        pages=[
            page(
                0,
                text("目录", 100),
                text("4 四 ..... 1", 200),
                text("5 五 ..... 2", 240),
                text("1 一 ..... 3", 280),
            ),
            page(1, number("1"), title("4 四", 100)),
            page(2, number("2"), title("5 五", 100)),
            page(3, number("3"), title("1 一", 100)),
            page(4, number("4"), title("2 二", 100)),
            page(5, number("5"), title("3 三", 100)),
        ],
        bookmarks=[
            Bookmark(1, "1 一", 3),
            Bookmark(1, "2 二", 4),
            Bookmark(1, "3 三", 5),
        ],
        metadata={},
    )
    nodes = build_toc("d1", parsed)
    assert nodes[0].source == "bookmark"


def test_unnumbered_series_toc_is_not_used_as_document_toc():
    parsed = ParsedDocument(
        pages=[
            page(0, text("目录", 100), text("1 引言", 160), text("2 定义", 200)),
            page(
                1,
                text("总目录", 100),
                text("1. GJB4.1—83 总则 ..... 1", 160),
                text("2. GJB4.2—83 高温试验 ..... 6", 200),
            ),
            page(
                2,
                title("1 引言", 100),
                title("1.1 范围", 140),
                text("范围正文", 180),
            ),
        ],
        bookmarks=[],
        metadata={},
    )
    nodes = build_toc("d1", parsed)
    assert nodes[0].source == "pageindex_md"
    assert [node.title for node in nodes] == ["1 引言", "1.1 范围"]
    assert all("GJB4" not in node.title for node in nodes)


def test_weak_printed_toc_yields_to_bookmarks():
    parsed = ParsedDocument(
        pages=[
            page(
                0,
                text("目录", 100),
                text("1 甲 ..... 1", 200),
                text("2 乙 ..... 2", 240),
            ),
            page(1, number("1"), title("1 甲", 100)),
            page(2, number("2"), title("2 乙", 100)),
        ],
        bookmarks=[
            Bookmark(1, "1 甲", 1),
            Bookmark(2, "1.1 甲一", 1),
            Bookmark(1, "2 乙", 2),
        ],
        metadata={},
    )
    nodes = build_toc("d1", parsed)
    assert nodes[0].source == "bookmark"


def test_printed_toc_with_footer_page_numbers():
    parsed = ParsedDocument(
        pages=[
            page(0, text("封面", 100)),
            page(
                1,
                text("目录", 100),
                text("1 范围 ............ 1", 200),
                text("2 术语和定义 ........ 2", 240),
                text("2.1 最大负载循环 .... 2", 280),
                text("3 试验条件 .......... 3", 320),
            ),
            page(2, number("1"), title("1 范围", 100), text("正文", 200)),
            page(
                3,
                number("2"),
                title("2 术语和定义", 100),
                title("2.1 最大负载循环", 200),
            ),
            page(4, number("3"), title("3 试验条件", 100)),
        ],
        bookmarks=[],
        metadata={},
    )
    nodes = build_toc("d1", parsed)
    assert nodes[0].source == "toc_page"
    assert [n.title for n in nodes] == [
        "1 范围",
        "2 术语和定义",
        "2.1 最大负载循环",
        "3 试验条件",
    ]
    assert [n.page_start for n in nodes] == [2, 3, 3, 4]
    assert [n.level for n in nodes] == [1, 1, 2, 1]
    assert nodes[2].parent_id == nodes[1].node_id


def test_roman_front_matter_does_not_collide_with_arabic_page_one():
    parsed = ParsedDocument(
        pages=[
            page(0, text("封面", 100)),
            page(
                1,
                text("目次", 100),
                text("前言.....II", 200),
                text("1 范围.....1", 240),
            ),
            page(2, number("I")),
            page(3, number("Ⅱ"), title("前言", 100)),
            page(4, number("III")),
            page(5, number("1"), title("1 范围", 100)),
        ],
        bookmarks=[],
        metadata={},
    )
    nodes = build_toc("d1", parsed)
    assert nodes[0].source == "toc_page"
    assert [n.title for n in nodes] == ["前言", "1 范围"]
    assert [n.page_start for n in nodes] == [3, 5]
    assert nodes[0].page_end == 4


def test_master_toc_is_extended_by_later_chapter_tocs():
    parsed = ParsedDocument(
        pages=[
            page(0, text("封面", 100)),
            page(
                1,
                text("目 录", 100),
                text("第 1 章 目的和适用范围", 160),
                text("第 2 章 通用术语定义", 200),
                text("第 4 章 温度－高度", 240),
            ),
            page(
                2,
                text("目 录", 100),
                text("1.0 目的和适用范围 ..... 1－1", 160),
                text("1.1 历史记录 ..... 1－1", 200),
                text("2.0 通用术语定义 ..... 2－1", 240),
            ),
            page(
                3,
                text("目 录", 100),
                text("4.0 温度－高度 ..... 4－1", 160),
                text("4.1 试验目的 ..... 4－1", 200),
            ),
            page(4, number("1-1"), title("1.0 目的和适用范围", 100), title("1.1 历史记录", 200)),
            page(5, number("2-1"), title("2.0 通用术语定义", 100)),
            page(6, number("4-1"), title("4.0 温度－高度", 100), title("4.1 试验目的", 200)),
        ],
        bookmarks=[],
        metadata={},
    )
    nodes = build_toc("d1", parsed)
    assert nodes[0].source == "toc_page"
    assert [n.title for n in nodes] == [
        "第1章 目的和适用范围",
        "1.0 目的和适用范围",
        "1.1 历史记录",
        "第2章 通用术语定义",
        "2.0 通用术语定义",
        "第4章 温度－高度",
        "4.0 温度－高度",
        "4.1 试验目的",
    ]
    assert [n.level for n in nodes] == [1, 2, 2, 1, 2, 1, 2, 2]
    assert [n.page_start for n in nodes] == [4, 4, 4, 5, 5, 6, 6, 6]
    assert nodes[1].parent_id == nodes[0].node_id
    assert nodes[6].parent_id == nodes[5].node_id


def test_chapter_local_toc_page_overrides_a_bad_global_page_number():
    parsed = ParsedDocument(
        pages=[
            page(0, text("封面", 100)),
            page(
                1,
                text("目 录", 100),
                text("第 1 章 一", 160),
                text("第 4 章 四", 200),
            ),
            # Chapter 1 TOC page; the stray 4-1 is a bad global page number.
            page(2, number("4-1"), text("目 录", 100), text("1.0 一 ..... 1-1", 160)),
            page(3, number("1-1"), title("1.0 一", 100)),
            # Chapter 4 TOC page, immediately before its content.
            page(4, text("目 录", 100), text("4.0 四 ..... 4-1", 160)),
            page(5, number("4-1"), title("4.0 四", 100)),
        ],
        bookmarks=[],
        metadata={},
    )
    nodes = build_toc("d1", parsed)
    chapter4 = next(node for node in nodes if node.title.startswith("第4章"))
    detail4 = next(node for node in nodes if node.title.startswith("4.0"))
    assert chapter4.page_start == 5
    assert detail4.page_start == 5


def test_bookmarks_fill_missing_chapter_details():
    parsed = ParsedDocument(
        pages=[
            page(0, text("封面", 100)),
            page(1, text("目 录", 100), text("第 4 章 温度－高度", 160)),
            page(2, number("4-1"), title("4.0 温度－高度", 100)),
            page(3, number("4-2"), title("4.5 温度试验", 100)),
        ],
        bookmarks=[Bookmark(2, "4.5 温度试验", 3)],
        metadata={},
    )
    nodes = build_toc("d1", parsed)
    assert nodes[0].source == "toc_page"
    titles = [node.title for node in nodes]
    assert "第4章 温度－高度" in titles
    assert "4.5 温度试验" in titles
    parent = next(node for node in nodes if node.title == "第4章 温度－高度")
    child = next(node for node in nodes if node.title == "4.5 温度试验")
    assert child.parent_id == parent.node_id


def test_hyphenated_page_labels_are_mapped():
    parsed = ParsedDocument(
        pages=[
            page(0, text("封面", 100)),
            page(
                1,
                text("目录", 100),
                text("1 范围 ..... 1-1", 200),
                text("2 术语和定义 ..... 1-2", 240),
            ),
            page(2, number("1-1"), title("1 范围", 100)),
            page(3, number("1-2"), title("2 术语和定义", 100)),
        ],
        bookmarks=[],
        metadata={},
    )
    nodes = build_toc("d1", parsed)
    assert nodes[0].source == "toc_page"
    assert [n.page_start for n in nodes] == [2, 3]


def test_printed_toc_two_columns_are_read_left_then_right():
    parsed = ParsedDocument(
        pages=[
            page(0, text("封面", 100)),
            page(
                1,
                text("目录", 100),
                text("1 范围 ............ 1", 200, x=100),
                text("2 术语和定义 ........ 2", 240, x=100),
                text("3 试验条件 .......... 3", 200, x=650),
                text("4 结果 ............ 4", 240, x=650),
            ),
            page(2, number("1"), title("1 范围", 100)),
            page(3, number("2"), title("2 术语和定义", 100)),
            page(4, number("3"), title("3 试验条件", 100)),
            page(5, number("4"), title("4 结果", 100)),
        ],
        bookmarks=[],
        metadata={},
    )
    nodes = build_toc("d1", parsed)
    assert nodes[0].source == "toc_page"
    assert [n.title for n in nodes] == [
        "1 范围",
        "2 术语和定义",
        "3 试验条件",
        "4 结果",
    ]


def test_same_page_chapter_and_appendix_roots_keep_their_children():
    """Chapter detail must stay under its chapter, not the last same-page root."""
    parsed = ParsedDocument(
        pages=[
            page(
                0,
                text("目 录", 100),
                text("第 1 章 一", 160),
                text("第 26 章 二十六", 200),
                text("附录 A 甲", 240),
                text("附录 B 乙", 280),
                text("附录 C 丙", 320),
            ),
            page(
                1,
                text("目 录", 100),
                text("26.0 零 ..... 26-1", 160),
                text("26.1 一 ..... 26-1", 200),
            ),
            page(2, number("26-1"), title("26.0 零", 100), title("26.1 一", 200)),
        ],
        bookmarks=[],
        metadata={},
    )
    nodes = build_toc("d1", parsed)
    by_title = {node.title: node for node in nodes}
    chapter26 = by_title["第26章 二十六"]
    appendix_c = by_title["附录C 丙"]
    assert by_title["26.0 零"].parent_id == chapter26.node_id
    assert by_title["26.1 一"].parent_id == chapter26.node_id
    assert appendix_c.parent_id is None
    assert all(node.parent_id != appendix_c.node_id for node in nodes)


def test_master_toc_spanning_two_pages_keeps_all_chapters():
    parsed = ParsedDocument(
        pages=[
            page(0, text("封面", 100)),
            page(
                1,
                text("目 录", 100),
                text("1 一 ..... 1", 160),
                text("2 二 ..... 2", 200),
                text("3 三 ..... 3", 240),
            ),
            page(
                2,
                text("目 录", 100),
                text("4 四 ..... 4", 160),
                text("5 五 ..... 5", 200),
                text("6 六 ..... 6", 240),
            ),
            page(3, number("1"), title("1 一", 100)),
            page(4, number("2"), title("2 二", 100)),
            page(5, number("3"), title("3 三", 100)),
            page(6, number("4"), title("4 四", 100)),
            page(7, number("5"), title("5 五", 100)),
            page(8, number("6"), title("6 六", 100)),
        ],
        bookmarks=[],
        metadata={},
    )
    nodes = build_toc("d1", parsed)
    assert [node.title for node in nodes] == [
        "1 一",
        "2 二",
        "3 三",
        "4 四",
        "5 五",
        "6 六",
    ]
    assert [node.page_start for node in nodes] == [3, 4, 5, 6, 7, 8]
    assert nodes[2].page_end == 5


def test_printed_toc_aligns_titles_when_footer_numbers_are_missing():
    parsed = ParsedDocument(
        pages=[
            page(0, text("封面", 100)),
            page(
                1,
                text("目录", 100),
                text("1 范围 ............ 1", 200),
                text("2 术语和定义 ........ 2", 240),
                text("3 试验条件 .......... 3", 280),
            ),
            page(2, title("1 范围", 100)),
            page(3, title("2 术语和定义", 100)),
            page(4, title("3 试验条件", 100)),
        ],
        bookmarks=[],
        metadata={},
    )
    nodes = build_toc("d1", parsed)
    assert nodes[0].source == "toc_page"
    assert [n.page_start for n in nodes] == [2, 3, 4]

def test_page_number_map_rejects_backwards_chapter_footer():
    """A footer whose chapter number regresses is a source-document error.

    DO-160G prints "14-1..14-3" on pages inside chapter 23; trusting it
    maps the whole of chapter 14 onto those pages.
    """
    from app.ingest.printed_toc import build_page_number_map

    pages = [
        page(0, number("23-18")),
        page(1, number("14－1")),
        page(2, number("14－2")),
        page(3, number("23-19")),
    ]
    mapping = build_page_number_map(pages)
    assert mapping.get("h:23-18") == 0
    assert "h:14-1" not in mapping
    assert "h:14-2" not in mapping
    assert mapping.get("h:23-19") == 3


def test_page_number_map_ignores_a_contents_page_folio():
    """A contents page prints its own folio, and OCR can misread it.

    PaddleOCR turns the roman ``Ⅰ`` of a front-matter contents page into an
    arabic ``1``. Binding ``n:1`` to that page puts every chapter printed as
    page 1 one front-matter page too early, which is what makes sibling TOC
    ranges overlap.
    """
    from app.ingest.printed_toc import build_page_number_map

    pages = [
        page(0, text("封面", 100)),
        page(1, number("1"), text("目 录", 100)),
        page(2, number("Ⅱ"), text("前言", 100)),
        page(3, number("1"), title("1 范围", 100)),
        page(4, number("2"), title("2 引用文件", 100)),
        page(5, number("3"), title("3 目的与应用", 100)),
    ]
    mapping = build_page_number_map(pages)
    assert mapping["n:1"] == 3
    assert mapping["n:2"] == 4
    assert mapping["r:ii"] == 2


def test_page_number_map_picks_the_reading_matching_the_dominant_offset():
    """A stray number block on the folio page must not win.

    PaddleOCR sometimes labels a figure or table fragment ``number`` on the
    same page as the real folio, and reads the folio itself with an extra
    digit (``17`` -> ``716``). Both readings are kept; the one that agrees
    with the document's offset is used.
    """
    from app.ingest.printed_toc import build_page_number_map

    pages = [
        page(0, text("封面", 100)),
        page(1, number("1"), text("目 录", 100)),
        page(2, number("Ⅱ"), text("前言", 100)),
        page(3, number("III"), text("前言", 100)),
    ]
    for folio in range(1, 20):
        extra = [number("716")] if folio == 17 else []
        pages.append(page(folio + 3, *extra, number(str(folio)), title(f"{folio} 节", 100)))

    mapping = build_page_number_map(pages)
    assert mapping["n:17"] == 20
    assert "n:716" not in mapping



def test_page_number_map_keeps_front_matter_numerals_read_only_on_the_toc_page():
    """Front-matter numerals often exist only on the contents page.

    Dropping every reading taken from a contents page would lose ``r:i``
    entirely, so such a label is kept when nothing else claims it.
    """
    from app.ingest.printed_toc import build_page_number_map

    pages = [
        page(0, text("封面", 100)),
        page(1, number("I"), text("目 录", 100)),
        page(2, number("1"), title("1 范围", 100)),
        page(3, number("2"), title("2 引用文件", 100)),
        page(4, number("3"), title("3 术语", 100)),
    ]
    mapping = build_page_number_map(pages)
    assert mapping["r:i"] == 1
    assert mapping["n:1"] == 2


def test_page_number_map_reads_every_block_on_a_page():
    """A page with two number blocks must offer both readings.

    ``67e6a794`` prints the folio ``1`` next to a misread ``24`` on the same
    page; stopping at the first block binds ``n:24`` to that page instead of
    to printed page 24.
    """
    from app.ingest.printed_toc import build_page_number_map

    pages = [
        page(0, text("封面", 100)),
        page(1, number("1"), text("目 录", 100)),
        page(2, number("Ⅱ"), text("前言", 100)),
        page(3, number("III"), text("前言", 100)),
    ]
    for folio in range(1, 25):
        extra = [number("24")] if folio == 1 else []
        pages.append(
            page(folio + 3, *extra, number(str(folio)), title(f"{folio} 节", 100))
        )

    mapping = build_page_number_map(pages)
    assert mapping["n:1"] == 4
    assert mapping["n:24"] == 27



def test_phantom_bookmark_outside_its_chapter_is_dropped():
    """The outline can carry phantom entries (table rows that became
    bookmarks). A bookmark whose target page lies outside its chapter's
    own span must not become a TOC node.
    """
    parsed = ParsedDocument(
        pages=[
            page(0, text("封面", 100)),
            page(
                1,
                text("目录", 100),
                text("第1章 甲 ..... 1", 200),
                text("第2章 乙 ..... 2", 240),
            ),
            page(2, number("1"), title("第1章 甲", 100), title("1.1 子", 300)),
            page(3, number("2"), title("第2章 乙", 100)),
        ],
        bookmarks=[
            Bookmark(1, "1 甲", 2),
            Bookmark(1, "1.1 子", 2),
            Bookmark(1, "2 乙", 3),
            Bookmark(1, "1.6 A•m（15kHz）", 165),
        ],
        metadata={},
    )
    nodes = build_toc("d1", parsed)
    assert all("1.6" != node.heading_num for node in nodes)
    assert all(
        node.page_start is None or node.page_start <= 3 for node in nodes
    )


# ── TOC continuation pages (marker-less spill-over) ────────────────────


def test_toc_continuation_page_without_marker_is_collected():
    from app.ingest.printed_toc import _collect_toc_pages

    pages = [
        page(
            0,
            text("目录", 100),
            text("1 甲 ..... 1", 200),
            text("2 乙 ..... 2", 240),
            text("3 丙 ..... 3", 280),
        ),
        page(
            1,
            text("４．１ 实验过程＃３９", 100),
            text("４．２ 燃烧气氛对前驱体的影响＃４０", 140),
            text("４．３ 碳热还原反应过程＃４４", 180),
        ),
    ] + [page(i, text(f"第{i}页 正文内容，句子以句号结束。", 100)) for i in range(2, 12)]

    collected = _collect_toc_pages(pages)
    indices = {page_index for page_index, _ in collected}
    assert 0 in indices and 1 in indices
    continuation = dict(collected)[1]
    assert [entry.heading_num for entry in continuation] == ["4.1", "4.2", "4.3"]
    # The broken layer keeps fullwidth digits; _printed_key normalizes them.
    assert [_printed_key(entry.printed_page) for entry in continuation] == [
        "n:39",
        "n:40",
        "n:44",
    ]


def test_body_page_after_toc_is_not_a_continuation():
    from app.ingest.printed_toc import _collect_toc_pages

    pages = [
        page(
            0,
            text("目录", 100),
            text("1 甲 ..... 1", 200),
            text("2 乙 ..... 2", 240),
            text("3 丙 ..... 3", 280),
        ),
        page(
            1,
            text("1 实验表明样品在1300℃下保温2h后完全氮化，见文献。", 100),
            text("2 结果与讨论部分的其余内容照常排版，不以页码结尾。", 140),
        ),
    ] + [page(i, text(f"第{i}页 正文。", 100)) for i in range(2, 12)]

    indices = {page_index for page_index, _ in _collect_toc_pages(pages)}
    assert 1 not in indices


# ── body-line artifacts sharing a heading number ───────────────────────


def test_dedupe_drops_body_line_that_shares_a_heading_number():
    entries = [
        TocEntry(
            title="2.2 低压化学气相沉积设备简介",
            heading_num="2.2",
            level=2,
            printed_page="17",
            page_index=None,
            confidence=0.9,
            order=0,
        ),
        TocEntry(
            title="2.2 所示。它是在低压条件下运行由芯片控制的全自动设备主要由腔室构成",
            heading_num="2.2",
            level=2,
            printed_page=None,
            page_index=None,
            confidence=0.8,
            order=1,
        ),
    ]
    kept = _dedupe_entries(entries)
    assert [entry.title for entry in kept] == ["2.2 低压化学气相沉积设备简介"]


def test_dedupe_keeps_two_real_sections_with_the_same_number_shape():
    """Different real titles under one number are both kept (计时器/测量尺)."""
    entries = [
        TocEntry(
            title="6.3 计时器",
            heading_num="6.3",
            level=2,
            printed_page="12",
            page_index=None,
            confidence=0.9,
            order=0,
        ),
        TocEntry(
            title="6.3 测量尺",
            heading_num="6.3",
            level=2,
            printed_page="13",
            page_index=None,
            confidence=0.9,
            order=1,
        ),
    ]
    assert len(_dedupe_entries(entries)) == 2


# ── reading order beats resolved pages for siblings ────────────────────


def test_printed_nodes_follow_reading_order_not_resolved_pages():
    from app.ingest.toc import _nodes_from_printed_entries

    entries = [
        TocEntry("1 绪论", "1", 1, "1", 11, 0.9, order=0),
        TocEntry("1.1 引言", "1.1", 2, "1", 11, 0.9, order=1),
        TocEntry("1.2 压电效应", "1.2", 2, "2", 12, 0.9, order=2),
        # 1.3's page mapping is wrong (same page as 1.1); reading order wins.
        TocEntry("1.3 研究现状", "1.3", 2, "2", 11, 0.9, order=3),
    ]
    nodes = _nodes_from_printed_entries("d1", entries, 30, 0.9)
    children = [node.title for node in nodes if node.level == 2]
    assert children == ["1.1 引言", "1.2 压电效应", "1.3 研究现状"]
