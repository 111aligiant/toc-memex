from __future__ import annotations

from app.ingest.heading import chapter_key, parse_heading


def test_parse_heading_normalizes_spaces_around_dots():
    assert parse_heading("5 . 1 试验目的") == ("5.1", "试验目的", 2)
    assert parse_heading("5 . 2. 1 技术文件") == ("5.2.1", "技术文件", 3)


def test_parse_heading_normalizes_fullwidth_digits():
    assert parse_heading("４ ．１ 系统组成") == ("4.1", "系统组成", 2)
    assert parse_heading("５．２ 功能、性能") == ("5.2", "功能、性能", 2)


def test_parse_heading_keeps_number_butted_against_title():
    assert parse_heading("4.0温度－高度") == ("4.0", "温度－高度", 2)


def test_parse_heading_recognizes_english_annexes():
    assert parse_heading("Annex A General") == ("Annex A", "General", 1)
    assert parse_heading("Appendix B Details") == ("Appendix B", "Details", 1)
    assert chapter_key("Annex A") == "A"
    assert chapter_key("Appendix B") == "B"


def test_parse_heading_does_not_treat_dates_or_list_markers_as_headings():
    assert parse_heading("2026-11-11")[0] is None
    assert parse_heading("1) 本文件适用于……")[0] is None