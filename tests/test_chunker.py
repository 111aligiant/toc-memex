from __future__ import annotations

from app.ingest.chunker import ChunkData, chunk_document, chunk_ranges_by_node
from app.ingest.toc import TocNodeData
from app.ocr.models import OcrBlock, OcrPage, ParsedDocument


def page(index: int, text: str) -> OcrPage:
    return OcrPage(index, 100, 200, "", [OcrBlock("text", text, (0, 0, 100, 10), 1)], {}, {})


def heading(text: str, y: int, order: int) -> OcrBlock:
    return OcrBlock("paragraph_title", text, (0, y, 100, y + 10), order)


def body(text: str, y: int, order: int) -> OcrBlock:
    return OcrBlock("text", text, (0, y, 100, y + 10), order)


def node(
    node_id: str,
    parent_id: str | None,
    level: int,
    num: str | None,
    title: str,
    page_start: int,
    page_end: int,
) -> TocNodeData:
    return TocNodeData(
        node_id,
        parent_id,
        level,
        num,
        title,
        title,
        page_start,
        page_end,
        "bookmark",
        1.0,
    )


def test_chunks_split_a_page_at_heading_blocks():
    parsed = ParsedDocument(
        pages=[
            OcrPage(
                0,
                100,
                200,
                "",
                [
                    heading("1.1 甲", 10, 1),
                    body("甲内容", 20, 2),
                    heading("1.2 乙", 30, 3),
                    body("乙内容", 40, 4),
                ],
                {},
                {},
            )
        ],
        bookmarks=[],
        metadata={},
    )
    toc = [
        node("d:1", None, 1, "1", "1 一", 0, 0),
        node("d:1.1", "d:1", 2, "1.1", "1.1 甲", 0, 0),
        node("d:1.2", "d:1", 2, "1.2", "1.2 乙", 0, 0),
    ]
    chunks = chunk_document(parsed, toc, max_chars=50, overlap=0)
    assert [chunk.node_id for chunk in chunks] == ["d:1.1", "d:1.2"]
    assert "甲内容" in chunks[0].content
    assert "乙内容" not in chunks[0].content
    assert "乙内容" in chunks[1].content


def test_heading_spanning_pages_stays_in_one_chunk():
    parsed = ParsedDocument(
        pages=[
            OcrPage(
                0,
                100,
                200,
                "",
                [heading("1.1 甲", 10, 1), body("甲一", 20, 2)],
                {},
                {},
            ),
            OcrPage(1, 100, 200, "", [body("甲二", 10, 1)], {}, {}),
        ],
        bookmarks=[],
        metadata={},
    )
    toc = [
        node("d:1", None, 1, "1", "1 一", 0, 1),
        node("d:1.1", "d:1", 2, "1.1", "1.1 甲", 0, 1),
    ]
    chunks = chunk_document(parsed, toc, max_chars=100, overlap=0)
    assert [chunk.node_id for chunk in chunks] == ["d:1.1"]
    assert "甲一" in chunks[0].content and "甲二" in chunks[0].content
    assert (chunks[0].page_start, chunks[0].page_end) == (0, 1)


def test_text_before_mid_page_heading_stays_with_previous_section():
    parsed = ParsedDocument(
        pages=[
            OcrPage(
                0,
                100,
                200,
                "",
                [heading("1.1 甲", 10, 1), body("甲内容", 20, 2)],
                {},
                {},
            ),
            OcrPage(
                1,
                100,
                200,
                "",
                [
                    body("甲续", 10, 1),
                    heading("1.2 乙", 20, 2),
                    body("乙内容", 30, 3),
                ],
                {},
                {},
            ),
        ],
        bookmarks=[],
        metadata={},
    )
    toc = [
        node("d:1", None, 1, "1", "1 一", 0, 1),
        node("d:1.1", "d:1", 2, "1.1", "1.1 甲", 0, 1),
        node("d:1.2", "d:1", 2, "1.2", "1.2 乙", 1, 1),
    ]
    chunks = chunk_document(parsed, toc, max_chars=100, overlap=0)
    by_node = {chunk.node_id: chunk for chunk in chunks}
    assert "甲续" in by_node["d:1.1"].content
    assert "乙内容" in by_node["d:1.2"].content
    assert "乙内容" not in by_node["d:1.1"].content


def test_heading_number_without_separator_matches_toc_node():
    parsed = ParsedDocument(
        pages=[
            OcrPage(
                0,
                100,
                200,
                "",
                [
                    heading("26.6.2.5.1燃烧器调节", 10, 1),
                    body("调节内容", 20, 2),
                ],
                {},
                {},
            )
        ],
        bookmarks=[],
        metadata={},
    )
    toc = [
        node("d:26", None, 1, "26", "26 防火", 0, 0),
        node("d:26.6", "d:26", 2, "26.6", "26.6 可燃性", 0, 0),
        node("d:26.6.2", "d:26.6", 3, "26.6.2", "26.6.2 垂直试验", 0, 0),
        node("d:26.6.2.5", "d:26.6.2", 4, "26.6.2.5", "26.6.2.5 准备", 0, 0),
        node(
            "d:26.6.2.5.1",
            "d:26.6.2.5",
            5,
            "26.6.2.5.1",
            "26.6.2.5.1燃烧器调节",
            0,
            0,
        ),
    ]
    chunks = chunk_document(parsed, toc, max_chars=100, overlap=0)
    assert [chunk.node_id for chunk in chunks] == ["d:26.6.2.5.1"]
    assert "调节内容" in chunks[0].content


def test_split_heading_number_and_title_match_toc_node():
    parsed = ParsedDocument(
        pages=[
            OcrPage(
                0,
                100,
                200,
                "",
                [
                    heading("3.1", 10, 1),
                    heading("术语", 15, 2),
                    body("术语内容", 40, 3),
                ],
                {},
                {},
            )
        ],
        bookmarks=[],
        metadata={},
    )
    toc = [
        node("d:3", None, 1, "3", "3 术语", 0, 0),
        node("d:3.1", "d:3", 2, "3.1", "3.1 术语", 0, 0),
    ]
    chunks = chunk_document(parsed, toc, max_chars=100, overlap=0)
    assert [chunk.node_id for chunk in chunks] == ["d:3.1"]
    assert "术语内容" in chunks[0].content


def test_split_heading_number_and_text_title_match_toc_node():
    parsed = ParsedDocument(
        pages=[
            OcrPage(
                0,
                100,
                200,
                "",
                [
                    heading("7", 10, 1),
                    body("Test levels", 15, 2),
                    body("Level content", 40, 3),
                ],
                {},
                {},
            )
        ],
        bookmarks=[],
        metadata={},
    )
    toc = [
        node("d:7", None, 1, "7", "7 Test levels", 0, 0),
    ]
    chunks = chunk_document(parsed, toc, max_chars=100, overlap=0)
    assert [chunk.node_id for chunk in chunks] == ["d:7"]
    assert "Level content" in chunks[0].content


def test_page_number_blocks_are_not_chunk_content():
    parsed = ParsedDocument(
        pages=[
            OcrPage(
                0,
                100,
                200,
                "",
                [
                    heading("1 范围", 10, 1),
                    body("正文", 20, 2),
                    OcrBlock("number", "26-1", (50, 190, 60, 200), None),
                ],
                {},
                {},
            )
        ],
        bookmarks=[],
        metadata={},
    )
    toc = [node("d:1", None, 1, "1", "1 范围", 0, 0)]
    chunks = chunk_document(parsed, toc, max_chars=100, overlap=0)
    assert "26-1" not in chunks[0].content
    assert "正文" in chunks[0].content


def test_front_matter_and_toc_pages_are_not_chunked():
    parsed = ParsedDocument(
        pages=[
            OcrPage(
                0,
                100,
                200,
                "",
                [OcrBlock("doc_title", "封面标题", (0, 0, 100, 10), 1)],
                {},
                {},
            ),
            OcrPage(
                1,
                100,
                200,
                "",
                [
                    OcrBlock("paragraph_title", "目录", (0, 0, 100, 10), 1),
                    OcrBlock("content", "1 一.....1\n2 二.....2", (0, 20, 100, 30), 2),
                ],
                {},
                {},
            ),
            OcrPage(
                2,
                100,
                200,
                "",
                [heading("1 一", 10, 1), body("正文一", 20, 2)],
                {},
                {},
            ),
        ],
        bookmarks=[],
        metadata={},
    )
    toc = [node("d:1", None, 1, "1", "1 一", 2, 2)]
    chunks = chunk_document(parsed, toc, max_chars=100, overlap=0)
    assert len(chunks) == 1
    assert chunks[0].page_start == 2
    assert "正文一" in chunks[0].content
    assert "封面" not in chunks[0].content
    assert "目录" not in chunks[0].content


def test_chapter_toc_pages_inside_body_are_not_chunked():
    parsed = ParsedDocument(
        pages=[
            OcrPage(
                0,
                100,
                200,
                "",
                [heading("1 一", 10, 1), body("正文一", 20, 2)],
                {},
                {},
            ),
            OcrPage(
                1,
                100,
                200,
                "",
                [
                    OcrBlock("paragraph_title", "目录", (0, 0, 100, 10), 1),
                    OcrBlock("content", "1.1 甲.....1\n1.2 乙.....2", (0, 20, 100, 30), 2),
                ],
                {},
                {},
            ),
            OcrPage(2, 100, 200, "", [body("正文二", 10, 1)], {}, {}),
        ],
        bookmarks=[],
        metadata={},
    )
    toc = [node("d:1", None, 1, "1", "1 一", 0, 2)]
    chunks = chunk_document(parsed, toc, max_chars=100, overlap=0)
    assert "正文一" in chunks[0].content and "正文二" in chunks[0].content
    assert "目录" not in chunks[0].content
    assert "1.1 甲" not in chunks[0].content


def test_chunk_ranges_cover_the_whole_subtree():
    toc = [
        node("d:1", None, 1, "1", "1 一", 0, 0),
        node("d:1.1", "d:1", 2, "1.1", "1.1 甲", 0, 0),
        node("d:1.2", "d:1", 2, "1.2", "1.2 乙", 0, 0),
    ]
    chunks = [
        ChunkData(0, "d:1.1", "甲", "1 一 > 1.1 甲", 0, 0),
        ChunkData(1, "d:1.2", "乙", "1 一 > 1.2 乙", 0, 0),
    ]
    ranges = chunk_ranges_by_node(toc, chunks)
    assert ranges["d:1"] == (0, 1)
    assert ranges["d:1.1"] == (0, 0)
    assert ranges["d:1.2"] == (1, 1)


def test_chunks_follow_deepest_toc_node_and_pages():
    parsed = ParsedDocument(
        pages=[page(0, "甲" * 10), page(1, "乙" * 10)], bookmarks=[], metadata={}
    )
    toc = [
        TocNodeData("d:1", None, 1, "1", "一", "1 一", 0, 1, "bookmark", 1.0),
        TocNodeData("d:1.1", "d:1", 2, "1.1", "甲", "1 一 > 1.1 甲", 1, 1, "bookmark", 1.0),
    ]
    chunks = chunk_document(parsed, toc, max_chars=50, overlap=0)
    assert [c.node_id for c in chunks] == ["d:1", "d:1.1"]
    assert [c.page_start for c in chunks] == [0, 1]
    assert chunks[1].heading_path == "1 一 > 1.1 甲"


def test_chunks_split_long_text_with_overlap():
    parsed = ParsedDocument(pages=[page(0, "字" * 300)], bookmarks=[], metadata={})
    toc = [
        TocNodeData("d:root", None, 1, None, "root", "root", 0, 0, "fallback", 0.3)
    ]
    chunks = chunk_document(parsed, toc, max_chars=100, overlap=20)
    assert len(chunks) >= 3
    assert all(len(c.content) <= 100 for c in chunks)


def test_same_page_bookmarks_without_numbers_split_at_body_headings():
    """An authored outline titles sections "Introduction" while the page
    prints "1 Introduction"; several such sections share one page, so page
    boundaries alone cannot split them."""
    parsed = ParsedDocument(
        pages=[
            OcrPage(
                0,
                100,
                200,
                "",
                [
                    heading("1 Introduction", 10, 1),
                    body("intro text", 20, 2),
                    heading("2 Background", 30, 3),
                    body("background text", 40, 4),
                    heading("3 Model Architecture", 50, 5),
                    body("model text", 60, 6),
                ],
                {},
                {},
            )
        ],
        bookmarks=[],
        metadata={},
    )
    toc = [
        node("d:intro", None, 1, None, "Introduction", 0, 0),
        node("d:bg", None, 1, None, "Background", 0, 0),
        node("d:model", None, 1, None, "Model Architecture", 0, 0),
    ]
    chunks = chunk_document(parsed, toc, max_chars=500, overlap=0)
    assert [chunk.node_id for chunk in chunks] == [
        "d:intro",
        "d:bg",
        "d:model",
    ]
    assert "background text" in chunks[1].content
    assert "model text" in chunks[2].content


def test_section_tail_on_next_page_stays_with_previous_node():
    """Text above the heading that opens a new page belongs to the section
    carried over from the previous page, not to the new section."""
    parsed = ParsedDocument(
        pages=[
            OcrPage(
                0,
                100,
                200,
                "",
                [
                    heading("1 甲", 10, 1),
                    body("甲正文", 20, 2),
                ],
                {},
                {},
            ),
            OcrPage(
                1,
                100,
                200,
                "",
                [
                    body("甲在第二页的续文", 10, 1),
                    heading("2 乙", 20, 2),
                    body("乙正文", 30, 3),
                ],
                {},
                {},
            ),
        ],
        bookmarks=[],
        metadata={},
    )
    toc = [
        node("d:1", None, 1, "1", "1 甲", 0, 0),
        node("d:2", None, 1, "2", "2 乙", 1, 1),
    ]
    chunks = chunk_document(parsed, toc, max_chars=500, overlap=0)
    assert [chunk.node_id for chunk in chunks] == ["d:1", "d:2"]
    assert "续文" in chunks[0].content
    assert "续文" not in chunks[1].content
