"""Evaluate section-level retrieval against a golden query set.

Usage:
    python scripts/eval_retrieval.py \
        --db data/tocmemex.db \
        --queries path/to/queries.jsonl \
        --top-k 5

The golden file is JSONL; each line has:
    id, query, kind, doc_id,
    primary_heading_num,
    expected_heading_nums,
    expected_pages

Metrics:
    hit@k       any top-k hit matches an expected heading or page
    primary@1   top hit's heading equals primary_heading_num
    page@1      top hit's page range overlaps expected_pages
    mrr         mean reciprocal rank of the first correct hit
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import ensure_schema, make_engine, session_factory  # noqa: E402
from app.index.fts import search_chunks  # noqa: E402
from app.index.sections import normalize_section_number  # noqa: E402


def load_queries(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]


def heading_matches(hit: dict, expected: set[str]) -> bool:
    heading = normalize_section_number(hit.get("heading_num"))
    return heading is not None and heading in expected


def page_matches(hit: dict, pages: list[list[int]]) -> bool:
    start = hit.get("page_start")
    end = hit.get("page_end")
    if start is None or end is None:
        return False
    return any(start <= p_end and end >= p_start for p_start, p_end in pages)


def is_correct(hit: dict, item: dict) -> bool:
    # Cross-document corpora: same heading numbers and pages appear in many
    # documents, so an expected_doc_id (or legacy doc_id) must also match.
    expected_doc = item.get("expected_doc_id") or item.get("doc_id")
    if expected_doc and hit.get("doc_id") != expected_doc:
        return False
    expected = {
        normalize_section_number(value)
        for value in item.get("expected_heading_nums", [])
    }
    expected.discard(None)
    if heading_matches(hit, expected):
        return True
    # Page overlap is a fallback only for hits without a heading number;
    # otherwise a wrong section on the same page would mask a ranking miss.
    heading = normalize_section_number(hit.get("heading_num"))
    if heading is None:
        return page_matches(hit, item.get("expected_pages", []))
    return False


async def evaluate(db_path: Path, query_path: Path, top_k: int) -> dict:
    queries = load_queries(query_path)
    engine = make_engine(db_path)
    # Retrieval now joins collections, so a database created before that
    # table existed must be migrated before the first query.
    await ensure_schema(engine)
    Session = session_factory(engine)
    rows: list[dict] = []

    async with Session() as db:
        for item in queries:
            hits = await search_chunks(db, item["query"], top_k=top_k)
            top = hits[0] if hits else None
            correct_rank = next(
                (rank for rank, hit in enumerate(hits, 1) if is_correct(hit, item)),
                None,
            )
            primary = normalize_section_number(item.get("primary_heading_num"))
            top_heading = (
                normalize_section_number(top.get("heading_num")) if top else None
            )
            expected_doc = item.get("expected_doc_id") or item.get("doc_id")
            top_doc = top.get("doc_id") if top else None
            rows.append(
                {
                    "id": item["id"],
                    "query": item["query"],
                    "kind": item.get("kind", ""),
                    "top_doc": top_doc[:8] if top_doc else None,
                    "top_heading": top_heading,
                    "top_pages": (
                        [top.get("page_start"), top.get("page_end")] if top else None
                    ),
                    "top_match_type": top.get("match_type") if top else None,
                    "primary_at_1": bool(
                        top_heading
                        and primary
                        and top_heading == primary
                        and (not expected_doc or top_doc == expected_doc)
                    ),
                    "hit_at_k": correct_rank is not None,
                    "correct_rank": correct_rank,
                    "mrr": (1.0 / correct_rank) if correct_rank else 0.0,
                    "page_at_1": bool(
                        top
                        and (not expected_doc or top_doc == expected_doc)
                        and page_matches(top, item.get("expected_pages", []))
                    ),
                }
            )

    await engine.dispose()

    total = len(rows)
    denominator = total or 1
    return {
        "db": str(db_path),
        "queries": str(query_path),
        "top_k": top_k,
        "total": total,
        "hit_at_k": round(sum(row["hit_at_k"] for row in rows) / denominator, 4),
        "primary_at_1": round(
            sum(row["primary_at_1"] for row in rows) / denominator, 4
        ),
        "page_at_1": round(sum(row["page_at_1"] for row in rows) / denominator, 4),
        "mrr": round(sum(row["mrr"] for row in rows) / denominator, 4),
        "rows": rows,
    }


def print_report(report: dict) -> None:
    print(f"db: {report['db']}")
    print(f"queries: {report['total']}")
    print(f"hit@{report['top_k']}: {report['hit_at_k']:.2%}")
    print(f"primary@1: {report['primary_at_1']:.2%}")
    print(f"page@1: {report['page_at_1']:.2%}")
    print(f"mrr: {report['mrr']:.4f}")
    print()
    print("id\tkind\tquery\ttop_doc\ttop\ttop_pages\tmatch\tcorrect\trank")
    for row in report["rows"]:
        print(
            f"{row['id']}\t{row['kind']}\t{row['query']}\t{row['top_doc']}\t"
            f"{row['top_heading']}\t{row['top_pages']}\t"
            f"{row['top_match_type']}\t{row['hit_at_k']}\t{row['correct_rank']}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/tocmemex.db")
    parser.add_argument("--queries", required=True)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = asyncio.run(
        evaluate(Path(args.db), Path(args.queries), max(1, args.top_k))
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)

    return 0 if report["hit_at_k"] >= 0.9 else 1


if __name__ == "__main__":
    raise SystemExit(main())