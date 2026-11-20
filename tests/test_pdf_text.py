from __future__ import annotations

from app.ingest.pdf_text import _text_heading, _toc_pages, text_layer_is_usable
from app.ocr.models import OcrBlock, OcrPage


class FakeTextPage:
    def __init__(self, text: str) -> None:
        self._text = text

    def get_text_range(self) -> str:
        return self._text


class FakePage:
    def __init__(self, text: str) -> None:
        self._text = text

    def get_textpage(self) -> FakeTextPage:
        return FakeTextPage(self._text)


class FakeDoc:
    def __init__(self, pages: list[str]) -> None:
        self._pages = [FakePage(text) for text in pages]

    def __len__(self) -> int:
        return len(self._pages)

    def __getitem__(self, index: int) -> FakePage:
        return self._pages[index]

    def close(self) -> None:
        pass


def test_chinese_text_layer_is_usable():
    text = "本部分规定了民用飞机机载设备的环境试验条件。\r\n" * 50
    assert text_layer_is_usable(FakeDoc([text]))


def test_english_text_layer_is_usable():
    text = "This International Standard applies to electromagnetic compatibility.\r\n" * 30
    assert text_layer_is_usable(FakeDoc([text]))


def test_broken_font_encoding_is_not_usable():
    # A CID/font mapping failure yields C1 controls and mojibake.
    text = "B(\x8f7¹º¤¥¦§¨©*\r\n&$'$!\r\n" * 60
    assert not text_layer_is_usable(FakeDoc([text]))


def test_scanned_pdf_with_tiny_text_layer_is_not_usable():
    assert not text_layer_is_usable(FakeDoc(["一二三"]))


def test_numbered_text_headings():
    assert _text_heading("1 范围") == ("1", "范围", 1)
    assert _text_heading("4.2 试验条件允差") == ("4.2", "试验条件允差", 2)
    assert _text_heading("1 Scope") == ("1", "Scope", 1)


def test_body_and_table_lines_are_not_headings():
    assert _text_heading("本部分规定了机载设备的环境试验条件。") is None
    assert _text_heading("5cm") is None
    assert _text_heading("表1 试验温度等级") is None
    assert _text_heading("N 个天线位置") is None


def test_toc_page_detection():
    def block(text: str, order: int) -> OcrBlock:
        return OcrBlock("text", text, (0, order, 100, order + 5), order)

    toc_page = OcrPage(
        0,
        100,
        200,
        "",
        [
            block("目 录", 0),
            block("1 范围 ........ 1", 1),
            block("2 引用文件 ...... 2", 2),
            block("3 术语和定义 .... 3", 3),
            block("4 一般要求 ...... 4", 4),
            block("5 详细要求 ...... 5", 5),
        ],
        {},
        {},
    )
    body_page = OcrPage(
        1,
        100,
        200,
        "",
        [block("1 范围", 0), block("本部分规定了机载设备的环境试验条件。", 1)],
        {},
        {},
    )
    assert _toc_pages([toc_page, body_page]) == {0}
