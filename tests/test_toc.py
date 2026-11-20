from __future__ import annotations

from app.config import Settings
from app.ingest.toc import TocNodeData, build_toc, validate_toc
from app.ocr.models import Bookmark, OcrBlock, OcrPage, ParsedDocument


def page(index: int, *blocks: OcrBlock) -> OcrPage:
    return OcrPage(index, 100, 200, "", list(blocks), {}, {})


def title(text: str, order: int) -> OcrBlock:
    return OcrBlock("paragraph_title", text, (0, order, 100, order + 10), order)


def test_build_toc_from_bookmarks():
    parsed = ParsedDocument(
        pages=[page(0), page(1), page(2)],
        bookmarks=[
            Bookmark(1, "1 范围", 0, "1"),
            Bookmark(2, "1.1 目的", 1, "1.1"),
        ],
        metadata={},
    )
    nodes = build_toc("d1", parsed)
    assert [(n.level, n.title, n.page_start, n.page_end) for n in nodes] == [
        (1, "1 范围", 0, 2),
        (2, "1.1 目的", 1, 2),
    ]
    assert nodes[1].parent_id == nodes[0].node_id
    assert nodes[0].source == "bookmark"


def test_build_toc_from_ocr_paragraph_titles():
    parsed = ParsedDocument(
        pages=[
            page(0, title("1 范围", 1), title("1.1 目的", 2)),
            page(1, title("2 试验条件", 1)),
        ],
        bookmarks=[],
        metadata={},
    )
    nodes = build_toc("d1", parsed)
    assert [n.title for n in nodes] == ["1 范围", "1.1 目的", "2 试验条件"]
    assert [n.level for n in nodes] == [1, 2, 1]
    assert nodes[0].source == "pageindex_md"


def test_build_toc_merges_split_number_and_title():
    parsed = ParsedDocument(
        pages=[
            page(
                0,
                OcrBlock("paragraph_title", "3 术语和定义", (0, 100, 100, 110), 1),
                OcrBlock("paragraph_title", "3.1", (0, 150, 20, 160), 2),
                OcrBlock("text", "术语说明", (0, 165, 100, 175), 3),
                OcrBlock(
                    "paragraph_title",
                    "最大负载循环 maximum duty cycle",
                    (30, 190, 100, 200),
                    4,
                ),
            )
        ],
        bookmarks=[],
        metadata={},
    )
    nodes = build_toc("d1", parsed)
    titles = [n.title for n in nodes]
    assert "3.1 最大负载循环 maximum duty cycle" in titles
    merged = next(n for n in nodes if n.title.startswith("3.1"))
    assert merged.level == 2


def test_docx_direct_prefers_bookmarks_over_printed_toc():
    toc_page = page(
        0,
        title("目 录", 1),
        title("1 范围 1", 2),
        title("2 引用文件 2", 3),
        title("3 术语和定义 3", 4),
        title("4 一般要求 4", 5),
    )
    body = [
        page(1, title("1 范围", 10)),
        page(2, title("2 引用文件", 10)),
        page(3, title("3 术语和定义", 10)),
        page(4, title("4 一般要求", 10)),
    ]
    bookmarks = [
        Bookmark(1, "1 范围", 1, "1"),
        Bookmark(1, "2 引用文件", 2, "2"),
        Bookmark(1, "3 术语和定义", 3, "3"),
        Bookmark(1, "4 一般要求", 4, "4"),
    ]

    ocr_nodes = build_toc(
        "d1", ParsedDocument(pages=[toc_page, *body], bookmarks=bookmarks)
    )
    assert ocr_nodes[0].source == "toc_page"

    docx_nodes = build_toc(
        "d1",
        ParsedDocument(
            pages=[toc_page, *body],
            bookmarks=bookmarks,
            metadata={"format": "docx-direct"},
        ),
    )
    assert [node.source for node in docx_nodes] == ["bookmark"] * 4
    assert [node.page_start for node in docx_nodes] == [1, 2, 3, 4]


def test_single_bookmark_falls_back_to_ocr_titles():
    parsed = ParsedDocument(
        pages=[page(0, title("1 范围", 1), title("1.1 目的", 2))],
        bookmarks=[Bookmark(1, "文档标题", 0, None)],
        metadata={},
    )
    nodes = build_toc("d1", parsed)
    assert nodes[0].source == "pageindex_md"


def test_same_page_bookmark_children_stay_under_their_parent():
    parsed = ParsedDocument(
        pages=[page(index) for index in range(12)],
        bookmarks=[
            Bookmark(2, "4.5 温度试验", 10),
            Bookmark(3, "4.5.1 试验设备", 11),
            Bookmark(2, "4.6 其他试验", 11),
        ],
        metadata={},
    )
    nodes = build_toc("d1", parsed)
    by_title = {node.title: node for node in nodes}
    assert by_title["4.5.1 试验设备"].parent_id == by_title["4.5 温度试验"].node_id
    assert by_title["4.6 其他试验"].parent_id is None


def node(
    node_id: str,
    parent_id: str | None,
    level: int,
    num: str | None,
    title: str,
    start: int,
    end: int,
) -> TocNodeData:
    return TocNodeData(
        node_id, parent_id, level, num, title, title, start, end, "bookmark", 1.0
    )


def test_fix_page_ranges_extends_parent_to_child_start():
    from app.ingest.toc import _fix_page_ranges

    toc = [
        node("d:1", None, 1, "9.4", "9.4 一般", 64, 64),
        node("d:1.1", "d:1", 2, "9.4.1", "9.4.1 甲", 63, 63),
    ]
    _fix_page_ranges(toc, 100)
    assert toc[0].page_start == 63
    assert toc[0].page_end >= 63
    assert toc[0].page_start <= toc[0].page_end


def test_printed_toc_keeps_sibling_ranges_apart_when_the_contents_page_folio_is_misread():
    """Regression for GJB150.16A: a misread contents folio shifted chapter 1.

    The contents page prints a roman ``Ⅰ`` that OCR reads as ``1``. Binding
    ``n:1`` there put ``1 范围``, ``3 目的与应用`` and its children on the
    contents page, so ``前言`` (P2–P5 by next-start arithmetic) overlapped
    ``3 目的与应用`` (P1–P6) and chunking attached 3.2 应用's text to 前言.
    """
    def number(content: str) -> OcrBlock:
        return OcrBlock("number", content, (580, 1500, 620, 1520), None)

    def heading(content: str) -> OcrBlock:
        return OcrBlock("paragraph_title", content, (100, 100, 600, 124), None)

    parsed = ParsedDocument(
        pages=[
            page(0),
            page(
                1,
                number("1"),
                OcrBlock(
                    "content",
                    "前言.....II\n1 范围.....1\n3 目的与应用.....1\n3.1 目的.....1\n"
                    "3.2 应用.....1\n3.3 限制.....3\n4 剪裁指南.....3",
                    (100, 200, 1100, 600),
                    None,
                ),
            ),
            page(2, number("Ⅱ"), heading("前言")),
            page(3, number("III"), OcrBlock("text", "前言续", (100, 100, 600, 120), None)),
            page(4, number("1"), heading("1 范围"), heading("3 目的与应用")),
            page(5, number("2"), heading("3.2 应用"), OcrBlock("text", "应用正文", (100, 200, 600, 220), None)),
            page(6, number("3"), heading("3.3 限制"), heading("4 剪裁指南")),
        ],
        bookmarks=[],
        metadata={},
    )
    nodes = build_toc("d1", parsed)
    by_title = {node.title: node for node in nodes}

    assert by_title["1 范围"].page_start == 4
    assert by_title["3 目的与应用"].page_start == 4
    assert by_title["前言"].page_end == 3
    assert not [
        issue
        for issue in validate_toc(nodes, parsed.page_count)
        if "overlap" in issue.message
    ]


def test_validate_toc_accepts_a_consistent_tree():
    toc = [
        node("d:1", None, 1, "1", "1 一", 0, 10),
        node("d:1.1", "d:1", 2, "1.1", "1.1 甲", 0, 5),
        node("d:1.2", "d:1", 2, "1.2", "1.2 乙", 6, 10),
    ]
    assert validate_toc(toc, page_count=11) == []


def test_validate_toc_does_not_warn_about_a_level_one_entry_without_pages():
    """A contents entry for a chapter the PDF does not contain is not an error.

    DO-160G's contents list 附录 A/B/C, but the excerpt carries no such
    pages and no bookmarks for them. The node is kept (the document does
    claim the section) with ``page_start=None``; that is a fact about the
    source, not a parse failure.
    """
    toc = [
        node("d:1", None, 1, "1", "1 一", 0, 10),
        node("d:a", None, 1, "附录A", "附录A 缩略语", None, None),
    ]
    assert validate_toc(toc, page_count=11) == []


def test_validate_toc_still_warns_about_a_deeper_entry_without_pages():
    """A located parent must account for its children's pages."""
    toc = [
        node("d:1", None, 1, "1", "1 一", 0, 10),
        node("d:1.1", "d:1", 2, "1.1", "1.1 甲", None, None),
    ]
    issues = validate_toc(toc, page_count=11)
    assert [issue.message for issue in issues] == ["missing page range"]


def test_duplicate_appendix_numbers_get_a_letter_suffix():
    """GB/T 2423 numbers two appendices 附录N and tells them apart by letter.

    ``parse_heading`` keeps the letter in the title, so the tree carries a
    duplicate sibling number. The number is disambiguated after nesting.
    """
    def text(content: str, y: int) -> OcrBlock:
        return OcrBlock("text", content, (100, y, 1100, y + 20), None)

    parsed = ParsedDocument(
        pages=[
            page(
                0,
                text("目 录", 100),
                text("附录N A（资料性附录） GB/T 2423 标准环境参数 .......... 1", 200),
                text("附录N B（资料性附录） 附录 A 中规定的对应关系 .......... 2", 240),
            ),
            page(1, title("附录N A（资料性附录） GB/T 2423 标准环境参数", 100)),
            page(2, title("附录N B（资料性附录） 附录 A 中规定的对应关系", 100)),
        ],
        bookmarks=[],
        metadata={},
    )
    nodes = build_toc("d1", parsed)
    numbers = [n.heading_num for n in nodes if n.heading_num]
    assert numbers == ["附录NA", "附录NB"]
    assert [n.title for n in nodes] == [
        "附录N A（资料性附录） GB/T 2423 标准环境参数",
        "附录N B（资料性附录） 附录 A 中规定的对应关系",
    ]
    assert not [
        issue
        for issue in validate_toc(nodes, parsed.page_count)
        if "duplicate" in issue.message
    ]


def test_appendix_suffix_is_not_applied_without_a_collision():
    """A lone 附录N keeps its number; only real duplicates are rewritten."""
    def text(content: str, y: int) -> OcrBlock:
        return OcrBlock("text", content, (100, y, 1100, y + 20), None)

    parsed = ParsedDocument(
        pages=[
            page(
                0,
                text("目 录", 100),
                text("附录A 缩略语 .......... 1", 200),
                text("附录N A（资料性附录） 标准环境参数 .......... 2", 240),
            ),
            page(1, title("附录A 缩略语", 100)),
            page(2, title("附录N A（资料性附录） 标准环境参数", 100)),
        ],
        bookmarks=[],
        metadata={},
    )
    nodes = build_toc("d1", parsed)
    numbers = [n.heading_num for n in nodes if n.heading_num]
    assert numbers == ["附录A", "附录N"]


def test_validate_toc_reports_structure_errors():
    toc = [
        node("d:1", None, 1, "1", "1 一", 5, 4),
        node("d:1.1", "d:1", 3, "1.1", "1.1 甲", 5, 5),
        node("d:x", "missing", 2, "1.2", "1.2 乙", 6, 6),
    ]
    messages = [issue.message for issue in validate_toc(toc)]
    assert any("reversed page range" in message for message in messages)
    assert any("level jump" in message for message in messages)
    assert any("missing parent" in message for message in messages)


def test_validate_toc_warns_on_duplicate_numbers_and_large_overlap():
    toc = [
        node("d:1", None, 1, "1", "1 一", 0, 20),
        node("d:1.1", "d:1", 2, "1.1", "1.1 甲", 0, 3),
        node("d:1.1b", "d:1", 2, "1.1", "1.1 乙", 2, 10),
        node("d:2", None, 1, "2", "2 二", 18, 20),
    ]
    issues = validate_toc(toc, page_count=21)
    messages = [issue.message for issue in issues]
    assert any("duplicate sibling number 1.1" in message for message in messages)
    assert any("overlap by" in message for message in messages)


def test_validate_toc_warns_on_low_page_coverage():
    toc = [node("d:1", None, 1, "1", "1 一", 0, 0)]
    issues = validate_toc(toc, page_count=100)
    assert any("covers 1/100 pages" in issue.message for issue in issues)


def test_unnumbered_appendix_subtitles_nest_under_appendix():
    parsed = ParsedDocument(
        pages=[
            page(
                0,
                title("附录A", 1),
                title("（资料性附录）", 2),
                title("典型污染流体种类和试验流体", 3),
                title("A.1 范围", 4),
            )
        ],
        bookmarks=[],
        metadata={},
    )
    nodes = build_toc("d1", parsed)
    roots = [node for node in nodes if node.parent_id is None]
    assert [node.title for node in roots] == ["附录A"]
    by_title = {node.title: node for node in nodes}
    assert by_title["（资料性附录）"].parent_id == roots[0].node_id
    assert (
        by_title["典型污染流体种类和试验流体"].parent_id == roots[0].node_id
    )


def test_build_toc_fallback_root():
    parsed = ParsedDocument(pages=[page(0)], bookmarks=[], metadata={})
    nodes = build_toc("d1", parsed)
    assert len(nodes) == 1
    assert nodes[0].node_id == "d1:root"
    assert nodes[0].source == "fallback"

# --- Task 6: build_toc LLM arbitration ---

def _keyed_settings():
    from app.config import Settings

    return Settings(
        tocmemex_llm_api_key="k",
        tocmemex_llm_model="m",
        tocmemex_llm_base_url="http://x/v1",
    )


def _weak_parsed_2_roots(n_pages: int = 26) -> ParsedDocument:
    """A parsed doc whose rule path yields only 2 chapter roots.

    Enough body pages that a 12-root LLM proposal gets full page coverage.
    """
    pages = [
        OcrPage(
            page_index=0,
            width=595.0,
            height=842.0,
            markdown="",
            blocks=[
                OcrBlock("text", "目 录", (0.0, 0.0, 595.0, 40.0), 0),
                OcrBlock("text", "第1章 总则……2", (0.0, 50.0, 595.0, 90.0), 1),
                OcrBlock("text", "第2章 试验方法……4", (0.0, 100.0, 595.0, 140.0), 2),
            ],
        ),
        OcrPage(
            page_index=1,
            width=595.0,
            height=842.0,
            markdown="",
            blocks=[OcrBlock("paragraph_title", "第1章 总则", (0.0, 0.0, 595.0, 100.0), 0)],
        ),
        OcrPage(
            page_index=2,
            width=595.0,
            height=842.0,
            markdown="",
            blocks=[OcrBlock("paragraph_title", "第2章 试验方法", (0.0, 0.0, 595.0, 100.0), 0)],
        ),
    ]
    pages.extend(
        OcrPage(page_index=i, width=595.0, height=842.0, markdown="", blocks=[])
        for i in range(3, n_pages)
    )
    return ParsedDocument(pages=pages)


def _fake_printed_toc_12_roots() -> "PrintedToc":
    from app.ingest.printed_toc import PrintedToc, TocEntry

    entries = []
    for chapter in range(1, 13):
        entries.append(
            TocEntry(
                title=f"{chapter}.0 章节{chapter}",
                heading_num=f"{chapter}.0",
                level=1,
                printed_page=str(chapter * 2 + 1),
                page_index=chapter * 2,
                confidence=0.85,
                is_master=True,
                order=chapter,
                evidence="llm",
            )
        )
    return PrintedToc(entries=entries, source_page=0, confidence=0.8)


def test_build_toc_no_key_matches_rule_only(monkeypatch):
    """无 key 时不得走 LLM 路径，输出与纯规则一致。

    The spy patches app.ingest.toc.detect_printed_toc_llm — the name
    build_toc actually calls (import-time binding). A keyed-settings
    counter-check proves the spy would fire if the no-key gate broke.
    """
    import app.ingest.toc as toc_mod
    from app.ingest.toc import build_toc

    monkeypatch.setattr(
        "app.ingest.toc.get_settings", lambda: Settings()
    )  # 无 key
    calls = {"n": 0}

    def spy(parsed, settings, doc_id=""):
        calls["n"] += 1
        return None

    monkeypatch.setattr(toc_mod, "detect_printed_toc_llm", spy)
    nodes = build_toc("docx", _weak_parsed_2_roots())
    assert calls["n"] == 0
    assert nodes  # 规则路径仍产出

    # Counter-check: the same spy fires under a keyed settings object, so
    # the assertion above actually gates the no-key path.
    monkeypatch.setattr(
        "app.ingest.toc.get_settings", lambda: _keyed_settings()
    )
    build_toc("docx", _weak_parsed_2_roots())
    assert calls["n"] == 1


def test_build_toc_prefers_llm_when_rules_weak(monkeypatch):
    """规则弱（2 根）+ LLM 可用 + LLM 结果更强（12 根）→ 采用 LLM。"""
    import app.ingest.toc as toc_mod
    from app.ingest.toc import build_toc

    monkeypatch.setattr(
        toc_mod,
        "detect_printed_toc_llm",
        lambda parsed, settings, doc_id="": _fake_printed_toc_12_roots(),
    )
    monkeypatch.setattr(
        "app.ingest.toc.get_settings", lambda: _keyed_settings()
    )
    nodes = build_toc("docx", _weak_parsed_2_roots())
    roots = [n for n in nodes if n.parent_id is None]
    assert len(roots) == 12
    assert nodes[0].source == "printed_toc_llm"


def test_build_toc_falls_back_to_rules_when_llm_fails(monkeypatch):
    from app.llm.client import LLMError

    import app.ingest.toc as toc_mod
    from app.ingest.toc import build_toc

    def boom(parsed, settings, doc_id=""):
        raise LLMError("down")

    monkeypatch.setattr(toc_mod, "detect_printed_toc_llm", boom)
    monkeypatch.setattr(
        "app.ingest.toc.get_settings", lambda: _keyed_settings()
    )
    nodes = build_toc("docx", _weak_parsed_2_roots())
    assert all(n.source != "printed_toc_llm" for n in nodes)
    assert nodes
