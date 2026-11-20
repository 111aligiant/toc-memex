"""Turn OCR table HTML into a grid that keeps merged-cell meaning.

The OCR providers return tables as HTML with ``rowspan``/``colspan``. The
chunk text used to be produced by stripping every tag and inserting a tab at
each ``</td>`` — which drops the rows a spanning cell covers, so downstream
readers (BM25, the LLM, the curator) saw rows with different column counts and
no way to tell that "外观" belongs to the "基本检查" group.

Here the HTML is parsed into a full rectangular grid: every cell of a merged
span repeats its value in each covered position. The grid is then serialised
as a Markdown table, which keeps the column structure and is easy for an LLM
to read. Display in the Web UI still uses the original HTML, so merged cells
render as merged there; this module is only about the text representation.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

_MAX_SPAN = 1000
_WS = re.compile(r"[ \t\r\f\v　]+")


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict]] = []
        self._row: list[dict] | None = None
        self._cell: dict | None = None
        self._table_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag == "table":
            self._table_depth += 1
            return
        if self._cell is not None:
            # A nested table inside a cell is kept as text, not as a new grid.
            if tag == "br":
                self._cell["text"].append("\n")
            return
        if tag == "tr":
            self._row = []
            return
        if tag in {"td", "th"}:
            values = dict(attrs)
            self._cell = {
                "text": [],
                "rowspan": _span(values.get("rowspan")),
                "colspan": _span(values.get("colspan")),
            }

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "table":
            self._table_depth = max(0, self._table_depth - 1)
            return
        if self._cell is not None:
            if tag in {"td", "th"}:
                if self._row is not None:
                    self._row.append(self._cell)
                self._cell = None
            return
        if tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell["text"].append(data)

    def close(self) -> None:
        super().close()
        if self._cell is not None and self._row is not None:
            self._row.append(self._cell)
            self._cell = None
        if self._row:
            self.rows.append(self._row)
            self._row = None


def _span(value: str | None) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return 1
    return max(1, min(number, _MAX_SPAN))


def _cell_text(parts: list[str]) -> str:
    text = "".join(parts)
    text = _WS.sub(" ", text).replace("\n", " ")
    return " ".join(text.split())


def table_html_to_grid(html_text: str) -> list[list[str]]:
    """Return the table as a rectangular grid with spans expanded."""
    parser = _TableParser()
    parser.feed(html_text)
    parser.close()
    if not parser.rows:
        return []

    cells: list[dict[int, str]] = []

    def ensure(row: int) -> None:
        while len(cells) <= row:
            cells.append({})

    for row_index, row in enumerate(parser.rows):
        ensure(row_index)
        column = 0
        for cell in row:
            while column in cells[row_index]:
                column += 1
            value = _cell_text(cell["text"])
            for row_offset in range(cell["rowspan"]):
                ensure(row_index + row_offset)
                for column_offset in range(cell["colspan"]):
                    cells[row_index + row_offset][column + column_offset] = value
            column += cell["colspan"]

    width = max((max(row) + 1 for row in cells if row), default=0)
    if width == 0:
        return []
    grid = [[row.get(column, "") for column in range(width)] for row in cells]
    return [row for row in grid if any(value for value in row)]


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").strip()


def grid_to_markdown(grid: list[list[str]]) -> str:
    """Serialise a grid as a Markdown table, padding rows to equal width."""
    if not grid:
        return ""
    width = max(len(row) for row in grid)
    rows = [row + [""] * (width - len(row)) for row in grid]
    lines = [
        "| " + " | ".join(_escape(value) for value in rows[0]) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    for row in rows[1:]:
        lines.append("| " + " | ".join(_escape(value) for value in row) + " |")
    return "\n".join(lines)


def table_html_to_markdown(html_text: str) -> str:
    """Expand merged cells and serialise the table as Markdown.

    Returns an empty string when the input has no parseable rows, so callers
    can fall back to their old text conversion.
    """
    return grid_to_markdown(table_html_to_grid(html_text))