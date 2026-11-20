"""Rebuild TOC/chunks from a cached PaddleOCR run for offline inspection.

Usage:
    uv run python scripts/toc_stats.py <doc_id> [--data-dir data/ocr]

Reads ``data/ocr/<doc_id>/raw.jsonl`` and ``bookmarks.json``; never calls OCR
and never touches the database.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingest.chunker import chunk_document  # noqa: E402
from app.ingest.toc import build_toc, validate_toc  # noqa: E402
from app.ocr.models import Bookmark, ParsedDocument  # noqa: E402
from app.ocr.parser import parse_jsonl, page_text  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("doc_id")
    parser.add_argument("--data-dir", default="data/ocr")
    args = parser.parse_args()

    root = Path(args.data_dir) / args.doc_id
    raw_path = root / "raw.jsonl"
    if not raw_path.exists():
        print(f"OCR cache missing: {raw_path}", file=sys.stderr)
        return 1

    pages = parse_jsonl(raw_path.read_text(encoding="utf-8"))
    bookmark_path = root / "bookmarks.json"
    bookmarks = (
        [
            Bookmark(**item)
            for item in json.loads(bookmark_path.read_text(encoding="utf-8"))
        ]
        if bookmark_path.exists()
        else []
    )
    parsed = ParsedDocument(pages=pages, bookmarks=bookmarks)
    toc = build_toc(args.doc_id, parsed)
    chunks = chunk_document(parsed, toc)

    by_id = {node.node_id: node for node in toc}
    roots = [node for node in toc if node.parent_id is None]
    bad_ranges = [
        node
        for node in toc
        if node.page_start is not None
        and node.page_end is not None
        and node.page_end < node.page_start
    ]
    unplaced = [node for node in toc if node.page_start is None]

    print(
        f"pages={len(pages)} bookmarks={len(bookmarks)} nodes={len(toc)} "
        f"roots={len(roots)} chunks={len(chunks)} bad_ranges={len(bad_ranges)} "
        f"unplaced={len(unplaced)}"
    )
    sources: dict[str, int] = {}
    for node in toc:
        sources[node.source] = sources.get(node.source, 0) + 1
    print(f"sources={sources}")

    print("roots:")
    for node in roots:
        children = sum(1 for item in toc if item.parent_id == node.node_id)
        page_range = (
            f"{node.page_start}-{node.page_end}"
            if node.page_start is not None
            else "?"
        )
        print(
            f"  {node.heading_num or '-':<10} {node.title[:40]:<42} "
            f"pages {page_range} children={children}"
        )

    text_pages = {page.page_index for page in pages if page_text(page).strip()}
    covered = set()
    for chunk in chunks:
        covered.update(range(chunk.page_start, chunk.page_end + 1))
    missing = sorted(text_pages - covered)
    print(
        f"chunk coverage: text_pages={len(text_pages)} covered={len(covered)} "
        f"missing={len(missing)}"
    )
    if missing:
        print(f"  missing pages: {missing[:20]}")

    bad_parents = [
        node
        for node in toc
        if node.parent_id is not None and node.parent_id not in by_id
    ]
    print(f"dangling parents={len(bad_parents)}")

    issues = validate_toc(toc, len(pages))
    errors = [issue for issue in issues if issue.severity == "error"]
    warnings = [issue for issue in issues if issue.severity == "warning"]
    print(f"validation: errors={len(errors)} warnings={len(warnings)}")
    for issue in issues[:8]:
        print(f"  {issue.severity}: {issue.message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())