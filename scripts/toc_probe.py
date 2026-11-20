"""Offline TOC probe for any text-layer PDF.

It does NOT call PaddleOCR. It builds an OcrPage per PDF page from the
text layer, adds the PDF outline as paragraph_title blocks, and runs the
same printed-TOC/bookmark merge used by the ingest pipeline.

    PYTHONPATH=. python scripts/toc_probe.py <pdf>
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pypdfium2 as pdfium

from app.ingest.bookmarks import filter_bookmarks
from app.ingest.toc import build_toc
from app.ocr.models import Bookmark, OcrBlock, OcrPage, ParsedDocument
from vendor.pageindex.flash.embedded_toc import read_bookmarks

PAGE_NUM = re.compile(r"^(?:\d+(?:[-－–—−]\d+)?|[ivxlcdmIVXLCDM]+)$")


def main(pdf_path: str) -> None:
    doc = pdfium.PdfDocument(pdf_path)
    pages: list[OcrPage] = []
    for index in range(len(doc)):
        text = doc[index].get_textpage().get_text_range() or ""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        blocks = [
            OcrBlock("text", line, (100, 100 + pos * 24, 1100, 120 + pos * 24), pos)
            for pos, line in enumerate(lines)
        ]
        if lines and PAGE_NUM.fullmatch(lines[-1]):
            blocks.append(OcrBlock("number", lines[-1], (580, 1500, 620, 1520), None))
        pages.append(OcrPage(index, 1200, 1600, "", blocks, {}, {}))
    doc.close()

    bookmarks = filter_bookmarks(
        [
            Bookmark(
                level=entry["level"],
                title=entry["title"],
                page_index=entry["page"] - 1,
            )
            for entry in read_bookmarks(pdf_path)
        ]
    )
    for bookmark in bookmarks:
        if 0 <= bookmark.page_index < len(pages):
            pages[bookmark.page_index].blocks.append(
                OcrBlock(
                    "paragraph_title",
                    bookmark.title,
                    (100, 40, 1100, 80),
                    None,
                )
            )

    parsed = ParsedDocument(pages=pages, bookmarks=bookmarks, metadata={})
    nodes = build_toc(Path(pdf_path).stem, parsed)
    print("bookmarks:", len(bookmarks), "toc_nodes:", len(nodes))
    if nodes:
        print("source:", nodes[0].source, "confidence:", nodes[0].confidence)
    for node in nodes[:60]:
        indent = "  " * max(0, node.level - 1)
        print(
            f"{indent}{node.node_id} [{node.level}] {node.title} "
            f"pages {node.page_start}-{node.page_end} ({node.source})"
        )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "sample.pdf")