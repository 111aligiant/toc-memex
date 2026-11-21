"""Offline TOC/chunk probe for a text-layer PDF (no PaddleOCR).

Builds an ``OcrPage`` per PDF page from the text layer, promotes heading-like
lines to ``paragraph_title`` blocks, adds the PDF outline, then runs the same
``build_toc`` / ``chunk_document`` / ``validate_toc`` code as ingest.

Usage:
    python scripts/textlayer_probe.py <file.pdf> [--limit N]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pypdfium2 as pdfium

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingest.chunker import chunk_document  # noqa: E402
from app.ingest.heading import parse_heading  # noqa: E402
from app.ingest.toc import build_toc, validate_toc  # noqa: E402
from app.ocr.models import Bookmark, OcrBlock, OcrPage, ParsedDocument  # noqa: E402
from vendor.pageindex.flash.embedded_toc import read_bookmarks  # noqa: E402

PAGE_NUM = re.compile(
    r"^(?:\d+(?:[-－–—−]\d+)?|[ivxlcdmIVXLCDMⅠ-ⅿ]+)$"
)
_PURE_NUMBER = re.compile(r"^\d+(?:\.\d+)+$")
_DOT_LEADER = re.compile(r"[.…·．]{3,}\s*[\divxlcdmIVXLCDM\-－–—−]+\s*$")
_KEEP_UNNUMBERED = {
    "前言",
    "引言",
    "参考文献",
    "索引",
    "缩略语",
    "目 录",
    "目录",
}


def _blocks_for_page(lines: list[str]) -> list[OcrBlock]:
    blocks: list[OcrBlock] = []
    # Page numbers sit near the top/bottom, sometimes with a running header
    # after them. Treat a pure number anywhere in those bands as furniture.
    number_lines = {
        pos
        for pos, line in enumerate(lines)
        if PAGE_NUM.fullmatch(line)
        and (pos < 2 or pos >= len(lines) - 4)
    }
    for pos, line in enumerate(lines):
        if pos in number_lines:
            blocks.append(OcrBlock("number", line, (580, 1500, 620, 1520), None))
            continue
        parsed = parse_heading(line)
        is_heading = bool(parsed and parsed[0]) and not _DOT_LEADER.search(line)
        if not is_heading and _PURE_NUMBER.fullmatch(line):
            # Heading numbers are often split from their title into the next
            # line/block; _merge_adjacent_titles rejoins them later.
            is_heading = True
        if not is_heading and "".join(line.split()) in _KEEP_UNNUMBERED:
            is_heading = True
        label = "paragraph_title" if is_heading else "text"
        blocks.append(
            OcrBlock(label, line, (100, 100 + pos * 24, 1100, 120 + pos * 24), pos)
        )
    return blocks


def probe(pdf_path: Path, limit: int = 40) -> int:
    doc = pdfium.PdfDocument(str(pdf_path))
    pages: list[OcrPage] = []
    for index in range(len(doc)):
        text = doc[index].get_textpage().get_text_range() or ""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        pages.append(
            OcrPage(index, 1200, 1600, "", _blocks_for_page(lines), {}, {})
        )
    doc.close()

    bookmarks = [
        Bookmark(
            level=entry["level"],
            title=entry["title"],
            page_index=entry["page"] - 1,
        )
        for entry in read_bookmarks(pdf_path)
        if 0 <= entry["page"] - 1 < len(pages)
    ]
    for bookmark in bookmarks:
        page = pages[bookmark.page_index]
        key = "".join(bookmark.title.split())
        if all("".join(block.content.split()) != key for block in page.blocks):
            page.blocks.append(
                OcrBlock(
                    "paragraph_title",
                    bookmark.title,
                    (100, 20, 1100, 60),
                    None,
                )
            )

    parsed = ParsedDocument(pages=pages, bookmarks=bookmarks, metadata={})
    toc = build_toc(pdf_path.stem, parsed)
    chunks = chunk_document(parsed, toc)
    issues = validate_toc(toc, len(pages))
    used = {chunk.node_id for chunk in chunks}
    roots = [node for node in toc if node.parent_id is None]
    text_pages = {
        page.page_index
        for page in pages
        if any(
            block.label not in ("number", "header", "footer", "page_number", "aside_text")
            and block.content.strip()
            for block in page.blocks
        )
    }
    covered: set[int] = set()
    for chunk in chunks:
        covered.update(range(chunk.page_start, chunk.page_end + 1))

    print(f"file={pdf_path.name}")
    print(
        f"pages={len(pages)} bookmarks={len(bookmarks)} nodes={len(toc)} "
        f"roots={len(roots)} chunks={len(chunks)} "
        f"nodes_with_chunks={len(used)} "
        f"unplaced={sum(1 for node in toc if node.page_start is None)}"
    )
    print(
        f"coverage missing={len(text_pages - covered)} "
        f"errors={sum(1 for issue in issues if issue.severity == 'error')} "
        f"warnings={sum(1 for issue in issues if issue.severity == 'warning')}"
    )
    for issue in issues[:5]:
        print(f"  {issue.severity}: {issue.message}")
    print("roots:")
    for node in roots[:limit]:
        pages_text = (
            f"{node.page_start}-{node.page_end}"
            if node.page_start is not None
            else "?"
        )
        print(f"  {node.heading_num or '-':<10} {node.title[:50]:<52} pages {pages_text}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf")
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args()
    return probe(Path(args.pdf), args.limit)


if __name__ == "__main__":
    raise SystemExit(main())