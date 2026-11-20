from __future__ import annotations

from app.ingest.tabletext import table_html_to_grid, table_html_to_markdown
from app.ocr.models import OcrBlock, OcrPage
from app.ocr.parser import page_text

# The first rows of 表1 in GB/T 38058-2019. "基本检查" spans seven rows and
# the 试验项目 header spans two columns.
TABLE_1 = (
    '<table><tr><td>序号</td><td colspan="2">试验项目</td><td>章条号</td></tr>'
    '<tr><td>1</td><td rowspan="7">基本检查</td><td>齐套性</td><td>6.2.1</td></tr>'
    '<tr><td>2</td><td>外观</td><td>6.2.2</td></tr>'
    '<tr><td>3</td><td>尺寸</td><td>6.2.3</td></tr>'
    '<tr><td>4</td><td>质量和质心</td><td>6.2.4</td></tr>'
    '<tr><td>5</td><td>机构动作</td><td>6.2.5</td></tr>'
    '<tr><td>6</td><td>接插件</td><td>6.2.6</td></tr>'
    '<tr><td>7</td><td>登记备案</td><td>6.2.7</td></tr>'
    '<tr><td>20</td><td rowspan="12">飞行性能试验</td>'
    '<td>最大起飞质量</td><td>6.4.1</td></tr>'
    '</table>'
)


def test_rowspan_value_repeats_on_every_covered_row():
    grid = table_html_to_grid(TABLE_1)
    assert len(grid[0]) == 4
    assert grid[1] == ["1", "基本检查", "齐套性", "6.2.1"]
    assert grid[2] == ["2", "基本检查", "外观", "6.2.2"]
    assert grid[7] == ["7", "基本检查", "登记备案", "6.2.7"]
    assert grid[8] == ["20", "飞行性能试验", "最大起飞质量", "6.4.1"]


def test_colspan_header_fills_every_column():
    grid = table_html_to_grid(TABLE_1)
    assert grid[0] == ["序号", "试验项目", "试验项目", "章条号"]


def test_markdown_output_keeps_columns_aligned():
    markdown = table_html_to_markdown(TABLE_1)
    lines = markdown.splitlines()
    assert lines[0] == "| 序号 | 试验项目 | 试验项目 | 章条号 |"
    assert "| 2 | 基本检查 | 外观 | 6.2.2 |" in lines
    assert "| 20 | 飞行性能试验 | 最大起飞质量 | 6.4.1 |" in lines
    assert len({line.count("|") for line in lines}) == 1


def test_pipe_and_newline_are_escaped_for_markdown():
    html = "<table><tr><td>a|b</td><td>c\n d</td></tr></table>"
    markdown = table_html_to_markdown(html)
    assert "a\\|b" in markdown
    assert "c d" in markdown


def test_page_text_uses_expanded_table_not_flattened_tabs():
    page = OcrPage(
        page_index=7,
        width=100,
        height=100,
        markdown="",
        blocks=[OcrBlock("table", TABLE_1, (0, 0, 10, 10), 0)],
    )
    text = page_text(page)
    assert "| 2 | 基本检查 | 外观 | 6.2.2 |" in text
    assert "\t" not in text