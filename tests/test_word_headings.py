from __future__ import annotations

from app.ingest.word import extract_headings


class FakeListFormat:
    def __init__(self, list_string: str = "") -> None:
        self.ListString = list_string


class FakeRange:
    def __init__(self, text: str, page: int, list_string: str = "") -> None:
        self.Text = text
        self._page = page
        self.ListFormat = FakeListFormat(list_string)

    def Information(self, what: int) -> int:
        return self._page


class FakePara:
    def __init__(self, level: int, text: str, page: int, list_string: str = "") -> None:
        self.OutlineLevel = level
        self.Range = FakeRange(text, page, list_string)


class FakeDoc:
    def __init__(self, paras: list[FakePara]) -> None:
        self.Paragraphs = paras


def test_extract_headings_maps_outline_levels_and_pages():
    doc = FakeDoc(
        [
            FakePara(1, "1 范围\r", 1),
            FakePara(2, "1.1 目的\r", 2),
            FakePara(10, "正文", 2),
            FakePara(1, "2 试验条件\r", 3),
        ]
    )
    marks = extract_headings(doc)
    assert [(m.level, m.title, m.page_index) for m in marks] == [
        (1, "1 范围", 0),
        (2, "1.1 目的", 1),
        (1, "2 试验条件", 2),
    ]
    assert [m.heading_num for m in marks] == ["1", "1.1", "2"]