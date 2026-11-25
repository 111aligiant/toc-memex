from __future__ import annotations

from app.ingest.docx_direct import parse_word_document
from app.ingest.pipeline import _docx_direct_has_text


class FakeRange:
    def __init__(self, text: str, start: int) -> None:
        self.Text = text
        self.Start = start


class FakePara:
    def __init__(self, text: str, start: int, outline: int = 10) -> None:
        self.Range = FakeRange(text, start)
        self.OutlineLevel = outline


class FakeDoc:
    def __init__(self, paras: list[FakePara], page_starts: list[int]) -> None:
        self.Paragraphs = paras
        self._page_starts = page_starts

    def ComputeStatistics(self, statistic: int) -> int:
        return len(self._page_starts)

    def GoTo(self, what: int, which: int, count: int) -> FakeRange:
        return FakeRange("", self._page_starts[count - 1])


def _positions(texts: list[str]) -> list[FakePara]:
    """Build paragraphs whose offsets are sequential over ``texts``."""
    paras: list[FakePara] = []
    offset = 0
    for text in texts:
        paras.append(FakePara(text, offset))
        offset += len(text) + 1
    return paras


def test_outline_and_text_headings_are_detected():
    paras = _positions(
        [
            "中华人民共和国国家军用标准\r",
            "1  试验目的\r",
            "评定舰船电子设备在高温环境条件下工作的适应性。\r",
            "2  试验等级\r",
        ]
    )
    paras[0].OutlineLevel = 1
    doc = FakeDoc(paras, [0])

    parsed = parse_word_document(doc)

    assert [(b.heading_num, b.title) for b in parsed.bookmarks] == [
        (None, "中华人民共和国国家军用标准"),
        ("1", "1  试验目的"),
        ("2", "2  试验等级"),
    ]
    assert [block.label for block in parsed.pages[0].blocks] == [
        "paragraph_title",
        "paragraph_title",
        "text",
        "paragraph_title",
    ]


def test_printed_toc_lines_do_not_become_headings():
    paras = _positions(
        [
            "目    录\r",
            "1  范围 \t1\r",
            "2  引用文件 \t 1\r",
        ]
    )
    doc = FakeDoc(paras, [0])

    parsed = parse_word_document(doc)

    assert parsed.bookmarks == []
    assert all(block.label == "text" for block in parsed.pages[0].blocks)


def test_table_values_and_figure_labels_are_not_headings():
    paras = _positions(
        [
            "5cm\r",
            "25Hz～1kHz\r",
            "10 100 1k\r",
            "3.2  试 验 条 件\r",
        ]
    )
    doc = FakeDoc(paras, [0])

    parsed = parse_word_document(doc)

    assert [b.heading_num for b in parsed.bookmarks] == ["3.2"]


def test_paragraphs_are_assigned_to_pages_by_offset():
    paras = _positions(["前言\r", "正文一\r", "正文二\r", "1  范围\r"])
    doc = FakeDoc(paras, [0, len("前言\r") + 1 + len("正文一\r") + 1])

    parsed = parse_word_document(doc)

    assert len(parsed.pages) == 2
    assert [block.content for block in parsed.pages[0].blocks] == ["前言", "正文一"]
    assert [block.content for block in parsed.pages[1].blocks] == ["正文二", "1  范围"]


def test_spaced_appendix_outline_heading_gets_a_number():
    paras = _positions(["附 录 B ( 资 料 性 附 录 ) 补 充 导 则\r"])
    paras[0].OutlineLevel = 1
    doc = FakeDoc(paras, [0])

    parsed = parse_word_document(doc)

    assert parsed.bookmarks[0].heading_num == "附录B"


def test_image_only_docx_is_not_treated_as_text():
    paras = _positions(["\r", "\r"])
    doc = FakeDoc(paras, list(range(0, 106)))
    parsed = parse_word_document(doc)
    assert not _docx_direct_has_text(parsed)

    text_doc = FakeDoc(_positions(["正文" * 100 + "\r"]), [0])
    assert _docx_direct_has_text(parse_word_document(text_doc))


def test_hand_typeset_resume_sections_become_headings():
    """Resumes keep every paragraph Normal/Body Text and draw section titles
    by hand; the known section names carry the structure."""
    paras = _positions(
        [
            "/ 核心优势\n",
            "AI产品从0到1落地能力：主导智能助手产品闭环。\n",
            "教育经历\n",
            "某大学 计算机科学与技术 本科\n",
            "/  项目经历\n",
            "检测AI助手平台：负责产品定义与MVP落地。\n",
            "/  技能特长\n",
            "产品管理：需求调研与PRD撰写。\n",
        ]
    )
    parsed = parse_word_document(FakeDoc(paras, [0]))
    assert [bookmark.title for bookmark in parsed.bookmarks] == [
        "/ 核心优势",
        "教育经历",
        "/  项目经历",
        "/  技能特长",
    ]
    assert [bookmark.level for bookmark in parsed.bookmarks] == [1, 1, 1, 1]
    headings = [
        block.content
        for block in parsed.pages[0].blocks
        if block.label == "paragraph_title"
    ]
    assert headings == ["/ 核心优势", "教育经历", "/  项目经历", "/  技能特长"]
