"""Unified, deterministic eval harness for Toc Memex.

Round A (this file) implements the ``retrieval`` mode only:

- section-level retrieval metrics (hit@k / primary@1 / page@1 / mrr),
  reusing the logic of the old ``scripts/eval_retrieval.py``;
- ``read_section`` page-citation metrics (``citation_page_rate`` /
  ``citation_leak_rate`` / ``low_confidence_rate``);
- optional ``ask`` citation-page metric (``ask_page_rate``).
- no LLM, no LLM grading: the same input must produce the same metrics.

Usage::

    python scripts/run_eval.py retrieval \
        --db data/eval/papers.db \
        --golden benchmarks/papers/golden.jsonl \
        --top-k 5 \
        --out data/eval/report-papers.json \
        [--json] [--fail-under hit_at_k=0.8]

Exit codes: 0 ok, 1 below ``--fail-under``, 2 golden/DB read failure.

The ``agent`` subcommand (round B: end-to-end 40-tool vs 8-tool A/B/C with
an external model) is intentionally NOT implemented. The CLI surface is
reserved below so the command name does not have to change later.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import ensure_schema, make_engine, session_factory  # noqa: E402
from app.index.ask import ask_question  # noqa: E402
from app.index.fts import search_chunks  # noqa: E402
from app.index.sections import normalize_section_number, read_section  # noqa: E402
from app.models import TocNode  # noqa: E402
from sqlalchemy import select  # noqa: E402

# Metrics that are better when lower; used by the markdown printer and in
# error messages only. ``--fail-under`` always means "below is bad".
LOWER_IS_BETTER = {"citation_leak_rate", "low_confidence_rate"}


# ---------------------------------------------------------------------------
# Golden loading (v1 flat rows + v2 nested rows)
# ---------------------------------------------------------------------------


def _as_str_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _as_page_ranges(value) -> list[list[int]]:
    """Normalize pages to ``[[start, end], ...]``.

    Accepts ``[[12, 14]]`` (v2), ``[12, 14]`` (single range), or a flat
    ``[12, 14]`` legacy value. A single page ``12`` becomes ``[12, 12]``.
    """
    if value is None:
        return []
    if isinstance(value, (int, float)):
        return [[int(value), int(value)]]
    ranges: list[list[int]] = []
    items = list(value)
    if items and all(isinstance(v, (int, float)) for v in items):
        if len(items) == 2:
            ranges.append([int(items[0]), int(items[1])])
        elif len(items) == 1:
            ranges.append([int(items[0]), int(items[0])])
        return ranges
    for entry in items:
        if isinstance(entry, (int, float)):
            ranges.append([int(entry), int(entry)])
        elif isinstance(entry, (list, tuple)) and entry:
            start = int(entry[0])
            end = int(entry[1]) if len(entry) > 1 else start
            ranges.append([start, end])
    return ranges


def normalize_golden_item(raw: dict) -> dict:
    """Map a v1 flat row or a v2 nested row to one internal shape.

    Flat (v1) fields `doc_id / primary_heading_num / expected_heading_nums /
    expected_pages` move under ``expected``. ``expected.doc_id`` wins over
    the legacy ``doc_id``. ``kind`` defaults to ``""`` for v1 rows.
    Unknown ``kind`` values are passed through untouched.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"golden line must be a JSON object, got {type(raw).__name__}")
    query = raw.get("query")
    if not query or not str(query).strip():
        raise ValueError(f"golden item {raw.get('id')!r} has no query")
    item_id = raw.get("id")
    if not item_id:
        raise ValueError(f"golden item without query has no id: {raw!r}")

    expected = dict(raw.get("expected") or {})
    forbidden = dict(raw.get("forbidden") or {})

    # Legacy flat -> nested. v2 nested values already present are kept.
    if expected.get("doc_id") is None and raw.get("doc_id") is not None:
        expected["doc_id"] = raw["doc_id"]
    if expected.get("node_id") is None and raw.get("node_id") is not None:
        expected["node_id"] = raw["node_id"]
    if not expected.get("heading_nums") and raw.get("expected_heading_nums") is not None:
        expected["heading_nums"] = raw["expected_heading_nums"]
    if expected.get("primary_heading_num") is None and raw.get(
        "primary_heading_num"
    ) is not None:
        expected["primary_heading_num"] = raw["primary_heading_num"]
    if not expected.get("pages") and raw.get("expected_pages") is not None:
        expected["pages"] = raw["expected_pages"]

    heading_nums = [
        h for h in (normalize_section_number(v) for v in _as_str_list(
            expected.get("heading_nums")
        )) if h
    ]
    primary = normalize_section_number(expected.get("primary_heading_num"))
    if primary is None and heading_nums:
        # v2 has no explicit primary field; the first expected heading is the
        # primary section unless the author names one.
        primary = heading_nums[0]

    return {
        "id": str(item_id),
        "query": str(query),
        "kind": str(raw.get("kind", "")),
        "expected": {
            "doc_id": expected.get("doc_id"),
            "node_id": expected.get("node_id"),
            "heading_nums": heading_nums,
            "primary_heading_num": primary,
            "pages": _as_page_ranges(expected.get("pages")),
            "answer_contains": _as_str_list(expected.get("answer_contains")),
        },
        "forbidden": {
            "node_ids": _as_str_list(forbidden.get("node_ids")),
            "heading_nums": [
                h for h in (normalize_section_number(v) for v in _as_str_list(
                    forbidden.get("heading_nums")
                )) if h
            ],
        },
        "notes": raw.get("notes", ""),
    }


def load_golden(path: Path) -> list[dict]:
    """Read a JSONL golden file (v1/v2, mixed) and normalize every line."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise OSError(f"cannot read golden file {path}: {exc}") from exc
    items: list[dict] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
        try:
            items.append(normalize_golden_item(raw))
        except ValueError as exc:
            raise ValueError(f"{path}:{line_no}: {exc}") from exc
    return items


# ---------------------------------------------------------------------------
# Retrieval metrics
# ---------------------------------------------------------------------------


def heading_matches(hit: dict, expected: set[str]) -> bool:
    heading = normalize_section_number(hit.get("heading_num"))
    return heading is not None and heading in expected


def page_matches(pages_a: list[list[int]], pages_b: list[list[int]]) -> bool:
    return any(
        a_start <= b_end and a_end >= b_start
        for a_start, a_end in pages_a
        for b_start, b_end in pages_b
    )


def _hit_pages(hit: dict) -> list[list[int]]:
    start = hit.get("page_start")
    end = hit.get("page_end")
    if start is None or end is None:
        return []
    return [[start, end]]


def _doc_matches(hit: dict, expected: dict) -> bool:
    expected_doc = expected.get("doc_id")
    return not expected_doc or hit.get("doc_id") == expected_doc


def is_correct(hit: dict, item: dict) -> bool:
    """A hit is correct when its heading matches, or (only for heading-less
    hits) when its pages overlap. Cross-document corpora require the doc to
    match too, so the same section number in another paper never counts."""
    expected = item["expected"]
    if not _doc_matches(hit, expected):
        return False
    if heading_matches(hit, set(expected["heading_nums"])):
        return True
    # Page overlap is a fallback only for hits without a heading; otherwise a
    # wrong section on the same page would mask a ranking miss.
    if normalize_section_number(hit.get("heading_num")) is None:
        return page_matches(_hit_pages(hit), expected["pages"])
    return False


async def eval_retrieval(db, items: list[dict], top_k: int) -> list[dict]:
    """Score section retrieval for every golden item. Returns one row each."""
    rows: list[dict] = []
    for item in items:
        expected = item["expected"]
        hits = await search_chunks(db, item["query"], top_k=top_k)
        top = hits[0] if hits else None
        correct_rank = next(
            (rank for rank, hit in enumerate(hits, 1) if is_correct(hit, item)),
            None,
        )
        primary = expected["primary_heading_num"]
        top_heading = normalize_section_number(top.get("heading_num")) if top else None
        top_doc = top.get("doc_id") if top else None
        top_pages = _hit_pages(top) if top else []
        rows.append(
            {
                "id": item["id"],
                "query": item["query"],
                "kind": item["kind"],
                "top_doc": top_doc[:8] if top_doc else None,
                "top_heading": top_heading,
                "top_pages": top_pages or None,
                "top_match_type": top.get("match_type") if top else None,
                "hit_at_k": correct_rank is not None,
                "correct_rank": correct_rank,
                "mrr": (1.0 / correct_rank) if correct_rank else 0.0,
                "primary_at_1": bool(
                    top_heading
                    and primary
                    and top_heading == primary
                    and _doc_matches(top, expected)
                ),
                "page_at_1": bool(
                    top
                    and _doc_matches(top, expected)
                    and page_matches(_hit_pages(top), expected["pages"])
                ),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# read_section citation metrics
# ---------------------------------------------------------------------------


def _doc_id_for_read(item: dict) -> str | None:
    """read_section needs a doc_id. Golden may omit it; node ids look like
    ``<doc_id>:h:<number>`` so the prefix is a safe fallback."""
    expected = item["expected"]
    if expected.get("doc_id"):
        return expected["doc_id"]
    node_id = expected.get("node_id") or ""
    if ":h:" in node_id:
        return node_id.split(":h:", 1)[0]
    return None


async def _forbidden_titles(db, doc_id: str, heading_nums: set[str]) -> list[str]:
    """Titles of forbidden heading numbers, only where long enough to be
    meaningful (>= 4 chars) so a generic short title never false-positives."""
    if not heading_nums:
        return []
    nodes = (
        await db.execute(select(TocNode).where(TocNode.doc_id == doc_id))
    ).scalars().all()
    titles: list[str] = []
    for node in nodes:
        if normalize_section_number(node.heading_num) in heading_nums:
            title = (node.title or "").strip()
            if len(title) >= 4:
                titles.append(title)
    return titles


async def eval_read_section(db, items: list[dict]) -> list[dict]:
    """Score ``read_section`` page citation + neighbour leakage.

    Only items with ``expected.node_id`` participate. ``citation_page_ok``
    is None when the item states no expected pages; ``leak`` is None when
    the item declares no forbidden node/heading (nothing to judge).
    """
    rows: list[dict] = []
    for item in items:
        expected = item["expected"]
        node_id = expected.get("node_id")
        if not node_id:
            continue
        doc_id = _doc_id_for_read(item)
        row: dict = {
            "id": item["id"],
            "kind": item["kind"],
            "node_id": node_id,
            "doc_id": doc_id,
            "citation_page_ok": None,
            "leak": None,
            "confidence": None,
            "fallback": None,
            "content_empty": None,
            "error": None,
        }
        if not doc_id:
            row["error"] = "no doc_id for read_section (set expected.doc_id)"
            rows.append(row)
            continue
        try:
            section = await read_section(db, doc_id, node_id)
        except Exception as exc:  # a missing node must not abort the run
            row["error"] = f"{type(exc).__name__}: {exc}"
            rows.append(row)
            continue

        section_pages = _as_page_ranges(
            [section.get("page_start"), section.get("page_end")]
            if section.get("page_start") is not None
            and section.get("page_end") is not None
            else None
        )
        if expected["pages"]:
            row["citation_page_ok"] = page_matches(section_pages, expected["pages"])

        # Confidence / fallback are optional: round-03 adds them to
        # read_section; a base without them reports None, never a crash.
        row["confidence"] = section.get("citation_confidence")
        row["fallback"] = section.get("fallback", section.get("resolved_from"))
        content = section.get("content") or ""
        row["content_empty"] = not content.strip()

        forbidden = item["forbidden"]
        has_forbidden = bool(
            forbidden["node_ids"] or forbidden["heading_nums"]
        )
        if has_forbidden:
            returned_node_ids = {
                seg.get("node_id")
                for seg in section.get("chunks", [])
                if seg.get("node_id")
            }
            leak = bool(returned_node_ids & set(forbidden["node_ids"]))
            if not leak:
                titles = await _forbidden_titles(
                    db, doc_id, set(forbidden["heading_nums"])
                )
                leak = any(title in content for title in titles)
            row["leak"] = leak
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# ask citation metrics (optional)
# ---------------------------------------------------------------------------


async def eval_ask(db, items: list[dict]) -> list[dict]:
    """Page-citation accuracy of ``ask``; answer text is not judged.

    Runs only for items that carry both a query and expected pages. The
    answer's citations count as correct when any citation page range
    overlaps the expected pages.
    """
    rows: list[dict] = []
    for item in items:
        expected = item["expected"]
        if not expected["pages"]:
            continue
        answer = await ask_question(db, item["query"])
        ok = any(
            page_matches(_hit_pages(citation), expected["pages"])
            for citation in answer.get("citations", [])
        )
        rows.append(
            {
                "id": item["id"],
                "kind": item["kind"],
                "ask_page_ok": ok,
                "citations": len(answer.get("citations", [])),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _mean(values: list) -> float | None:
    values = [value for value in values if value is not None]
    if not values:
        return None
    return round(sum(1.0 if value is True else 0.0 if value is False else float(value)
                     for value in values) / len(values), 4)


def _aggregate(rows: list[dict]) -> dict:
    retrieval = [row["retrieval"] for row in rows if row.get("retrieval")]
    reads = [row["read_section"] for row in rows if row.get("read_section")]
    asks = [row["ask"] for row in rows if row.get("ask")]
    return {
        "queries": len(rows),
        "hit_at_k": _mean([r["hit_at_k"] for r in retrieval]),
        "primary_at_1": _mean([r["primary_at_1"] for r in retrieval]),
        "page_at_1": _mean([r["page_at_1"] for r in retrieval]),
        "mrr": _mean([r["mrr"] for r in retrieval]),
        "citation_page_rate": _mean(
            [r["citation_page_ok"] for r in reads if r["citation_page_ok"] is not None]
        ),
        "citation_leak_rate": _mean(
            [r["leak"] for r in reads if r["leak"] is not None]
        ),
        "low_confidence_rate": _mean(
            [
                (r["confidence"] == "low")
                for r in reads
                if r["confidence"] is not None
            ]
        ),
        "ask_page_rate": _mean(
            [r["ask_page_ok"] for r in asks if r["ask_page_ok"] is not None]
        ),
    }


def summarize(rows: list[dict]) -> dict:
    """Totals plus the same metrics grouped by ``kind``."""
    kinds: list[str] = []
    for row in rows:
        kind = row.get("kind") or ""
        if kind not in kinds:
            kinds.append(kind)
    by_kind = {
        (kind or "unspecified"): _aggregate(
            [row for row in rows if (row.get("kind") or "") == kind]
        )
        for kind in kinds
    }
    return {"totals": _aggregate(rows), "by_kind": by_kind}


def merge_rows(
    retrieval_rows: list[dict],
    read_rows: list[dict],
    ask_rows: list[dict],
) -> list[dict]:
    read_by_id = {row["id"]: row for row in read_rows}
    ask_by_id = {row["id"]: row for row in ask_rows}
    merged: list[dict] = []
    for retrieval in retrieval_rows:
        item_id = retrieval["id"]
        merged.append(
            {
                "id": item_id,
                "kind": retrieval["kind"],
                "query": retrieval["query"],
                "retrieval": retrieval,
                "read_section": read_by_id.get(item_id),
                "ask": ask_by_id.get(item_id),
            }
        )
    return merged


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

_RATE_METRICS = {
    "hit_at_k",
    "primary_at_1",
    "page_at_1",
    "citation_page_rate",
    "citation_leak_rate",
    "low_confidence_rate",
    "ask_page_rate",
}


def _fmt(metric: str, value) -> str:
    if value is None:
        return "n/a"
    if metric in _RATE_METRICS:
        return f"{value:.2%}"
    if metric == "mrr":
        return f"{value:.4f}"
    return str(value)


def print_markdown(report: dict) -> None:
    print(f"# Eval report - {report['golden']}")
    print()
    print(f"- db: `{report['db']}`")
    print(f"- top_k: {report['top_k']}")
    print(f"- generated_at: {report['generated_at']}")
    print()
    print("| metric | value |")
    print("| --- | --- |")
    for metric in (
        "queries",
        "hit_at_k",
        "primary_at_1",
        "page_at_1",
        "mrr",
        "citation_page_rate",
        "citation_leak_rate",
        "low_confidence_rate",
        "ask_page_rate",
    ):
        if metric in report["totals"]:
            print(f"| {metric} | {_fmt(metric, report['totals'][metric])} |")
    print()
    print("| kind | n | hit@k | primary@1 | page@1 | mrr | cit.page | leak | low.conf | ask.page |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for kind, agg in report["by_kind"].items():
        print(
            "| "
            + " | ".join(
                [
                    kind,
                    str(agg["queries"]),
                    _fmt("hit_at_k", agg["hit_at_k"]),
                    _fmt("primary_at_1", agg["primary_at_1"]),
                    _fmt("page_at_1", agg["page_at_1"]),
                    _fmt("mrr", agg["mrr"]),
                    _fmt("citation_page_rate", agg["citation_page_rate"]),
                    _fmt("citation_leak_rate", agg["citation_leak_rate"]),
                    _fmt("low_confidence_rate", agg["low_confidence_rate"]),
                    _fmt("ask_page_rate", agg["ask_page_rate"]),
                ]
            )
            + " |"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_fail_under(specs: list[str] | None) -> dict[str, float]:
    thresholds: dict[str, float] = {}
    for spec in specs or []:
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            key, sep, value = part.partition("=")
            if not sep:
                raise ValueError(f"--fail-under expects metric=value, got {part!r}")
            thresholds[key.strip()] = float(value)
    return thresholds


async def _run_retrieval(
    db_path: Path, golden_path: Path, items: list[dict], top_k: int
) -> dict:
    engine = make_engine(db_path)
    try:
        await ensure_schema(engine)
        Session = session_factory(engine)
        async with Session() as db:
            retrieval_rows = await eval_retrieval(db, items, top_k)
            read_rows = await eval_read_section(db, items)
            ask_rows = await eval_ask(db, items)
    finally:
        await engine.dispose()

    merged = merge_rows(retrieval_rows, read_rows, ask_rows)
    summary = summarize(merged)
    return {
        "db": str(db_path),
        "golden": str(golden_path),
        "top_k": top_k,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "totals": summary["totals"],
        "by_kind": summary["by_kind"],
        "rows": merged,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)

    retrieval = sub.add_parser("retrieval", help="retrieval metrics (round A)")
    retrieval.add_argument("--db", default="data/tocmemex.db")
    retrieval.add_argument("--golden", required=True)
    retrieval.add_argument("--top-k", type=int, default=5)
    retrieval.add_argument("--out", default=None)
    retrieval.add_argument("--json", action="store_true")
    retrieval.add_argument(
        "--fail-under",
        action="append",
        metavar="METRIC=VALUE",
        help="exit 1 when a total metric is below VALUE (repeatable, comma-separated)",
    )

    # Round B is designed, not implemented (see plan 05 §9). The subcommand
    # exists so the command name is stable, but it reports "not implemented".
    agent = sub.add_parser("agent", help="end-to-end A/B/C (round B, not implemented)")
    agent.add_argument("--golden", required=True)
    agent.add_argument("--server-cmd", default="toc-memex-mcp")
    agent.add_argument("--profile", choices=["default", "full"], default="default")
    agent.add_argument("--model-endpoint", default=None)
    agent.add_argument("--max-calls", type=int, default=12)
    agent.add_argument("--out", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.mode == "agent":
        print(
            "agent mode is not implemented this round; see plan 05 section 9.",
            file=sys.stderr,
        )
        return 2

    db_path = Path(args.db)
    golden_path = Path(args.golden)
    try:
        thresholds = _parse_fail_under(args.fail_under)
    except ValueError as exc:
        parser.error(str(exc))
        return 2  # unreachable; parser.error exits
    try:
        items = load_golden(golden_path)
    except (OSError, ValueError) as exc:
        print(f"golden error: {exc}", file=sys.stderr)
        return 2
    if not db_path.exists():
        print(f"db not found: {db_path}", file=sys.stderr)
        return 2
    if not items:
        print(f"golden has no items: {golden_path}", file=sys.stderr)
        return 2

    try:
        report = asyncio.run(
            _run_retrieval(db_path, golden_path, items, max(1, args.top_k))
        )
    except Exception as exc:  # DB open / query failure
        print(f"db error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_markdown(report)

    failures: list[str] = []
    for metric, threshold in thresholds.items():
        if metric not in report["totals"]:
            print(f"unknown metric for --fail-under: {metric}", file=sys.stderr)
            return 2
        actual = report["totals"][metric]
        if actual is None or actual < threshold:
            shown = "n/a" if actual is None else f"{actual:.4f}"
            failures.append(f"{metric}={shown} < {threshold}")
    if failures:
        print("fail-under not met: " + ", ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
