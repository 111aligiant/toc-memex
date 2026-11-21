"""Probe a PDF corpus with the text-layer reader (no OCR, no database).

Reports which PDFs have a usable embedded text layer and what the TOC and
chunk pipeline produce for them. Scanned PDFs / broken fonts are reported as
``text_layer=unusable``; they still need PaddleOCR.

Usage:
    python scripts/probe_pdf_corpus.py --root D:\\path\\to\\corpus
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("data/tmp/pdf_probe.csv"))
    args = parser.parse_args()

    from app.ingest.chunker import chunk_document
    from app.ingest.pdf_text import parse_pdf_text
    from app.ingest.toc import build_toc

    files = sorted(
        path for path in args.root.rglob("*.pdf") if path.is_file()
    )
    rows: list[dict] = []
    for index, path in enumerate(files, 1):
        started = time.time()
        row: dict = {"path": str(path.relative_to(args.root))}
        try:
            parsed = parse_pdf_text(path)
            if parsed is None:
                row.update(text_layer="unusable", seconds=round(time.time() - started, 1))
            else:
                toc = build_toc("probe", parsed)
                chunks = chunk_document(parsed, toc)
                row.update(
                    text_layer="used",
                    pages=parsed.page_count,
                    bookmarks=len(parsed.bookmarks),
                    nodes=len(toc),
                    roots=sum(1 for node in toc if node.parent_id is None),
                    chunks=len(chunks),
                    seconds=round(time.time() - started, 1),
                )
        except Exception as exc:
            row.update(
                text_layer="error",
                error=f"{type(exc).__name__}: {exc}",
                seconds=round(time.time() - started, 1),
            )
        rows.append(row)
        status = row.get("text_layer")
        if status == "used":
            status += (
                f" {row['pages']}p {row['roots']}roots {row['chunks']}chunks"
            )
        print(f"[{index}/{len(files)}] {path.name}: {status}", flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "path",
        "text_layer",
        "pages",
        "bookmarks",
        "nodes",
        "roots",
        "chunks",
        "seconds",
        "error",
    ]
    with args.out.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    used = [row for row in rows if row.get("text_layer") == "used"]
    print()
    print(f"pdfs: {len(rows)}")
    print(f"text layer usable: {len(used)}")
    print(f"needs OCR: {len(rows) - len(used)}")
    if used:
        print(f"pages: {sum(row['pages'] for row in used)}")
        print(f"chunks: {sum(row['chunks'] for row in used)}")
    print(f"report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
