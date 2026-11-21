"""Probe a DOCX/DOC corpus with the direct Word parser.

Each file runs in its own subprocess so a Word dialog or a pathological
document cannot block the whole run. The script does not touch the archive
database and does not call OCR.

Usage:
    python scripts/probe_docx_corpus.py --root D:\\path\\to\\corpus
    python scripts/probe_docx_corpus.py --root . --glob "**/*.docx"
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _probe_one(path: Path) -> dict:
    from app.ingest.chunker import chunk_document
    from app.ingest.docx_direct import parse_docx_direct
    from app.ingest.toc import build_toc

    started = time.time()
    parsed = parse_docx_direct(path)
    toc = build_toc("probe", parsed)
    chunks = chunk_document(parsed, toc)
    return {
        "path": str(path),
        "pages": parsed.page_count,
        "blocks": sum(len(page.blocks) for page in parsed.pages),
        "chars": sum(
            len(block.content)
            for page in parsed.pages
            for block in page.blocks
        ),
        "bookmarks": len(parsed.bookmarks),
        "nodes": len(toc),
        "roots": sum(1 for node in toc if node.parent_id is None),
        "chunks": len(chunks),
        "seconds": round(time.time() - started, 1),
    }


def _run_child(path: Path, timeout: float) -> dict:
    started = time.time()
    try:
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--one", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "path": str(path),
            "error": f"timeout after {timeout:.0f}s",
            "seconds": round(time.time() - started, 1),
        }
    if completed.returncode != 0:
        return {
            "path": str(path),
            "error": (completed.stderr or "").strip()[-300:],
            "seconds": round(time.time() - started, 1),
        }
    try:
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return {
            "path": str(path),
            "error": "unparseable child output",
            "seconds": round(time.time() - started, 1),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path)
    parser.add_argument("--ext", default=".docx,.doc")
    parser.add_argument("--out", type=Path, default=Path("data/tmp/docx_probe.jsonl"))
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--one", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.one is not None:
        print(json.dumps(_probe_one(args.one), ensure_ascii=False))
        return 0

    if args.root is None:
        parser.error("--root is required")

    extensions = [item.strip().lower() for item in args.ext.split(",") if item.strip()]
    files = sorted(
        path
        for extension in extensions
        for path in args.root.rglob(f"*{extension}")
        if path.is_file() and not path.name.startswith("~$")
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.out.exists():
        args.out.unlink()

    results: list[dict] = []
    for index, path in enumerate(files, 1):
        result = _run_child(path, args.timeout)
        results.append(result)
        with args.out.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
        status = result.get("error") or (
            f"{result.get('pages')}p {result.get('roots')}roots "
            f"{result.get('chunks')}chunks"
        )
        print(f"[{index}/{len(files)}] {path.name}: {status}", flush=True)

    csv_path = args.out.with_suffix(".csv")
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "path",
                "pages",
                "blocks",
                "chars",
                "bookmarks",
                "nodes",
                "roots",
                "chunks",
                "seconds",
                "error",
            ],
        )
        writer.writeheader()
        writer.writerows(results)

    ok = [row for row in results if not row.get("error")]
    print()
    print(f"files: {len(results)}")
    print(f"parsed: {len(ok)}")
    print(f"failed: {len(results) - len(ok)}")
    if ok:
        print(f"pages: {sum(row['pages'] for row in ok)}")
        print(f"chars: {sum(row['chars'] for row in ok)}")
        print(f"median seconds: {sorted(row['seconds'] for row in ok)[len(ok) // 2]}")
    print(f"report: {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
