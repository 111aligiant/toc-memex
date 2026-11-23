"""Tests for scripts/run_eval.py on synthetic docs (no real corpus, no LLM)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from app.db import init_db, make_engine, session_factory
from app.index.fts import index_document
from app.models import Chunk, Document, Namespace, TocNode

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_eval.py"
_spec = importlib.util.spec_from_file_location("run_eval_under_test", _SCRIPT)
run_eval = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = run_eval
_spec.loader.exec_module(run_eval)


# ---------------------------------------------------------------------------
# Synthetic fixture: four documents with known sections / pages / tokens
# ---------------------------------------------------------------------------


async def _seed(tmp_path) -> Path:
    db_path = tmp_path / "eval.db"
    engine = make_engine(db_path)
    await init_db(engine)
    Session = session_factory(engine)
    async with Session() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()

        docs = {
            doc_id: Document(
                id=doc_id,
                namespace_id="shared",
                title=title,
                filename=f"{doc_id}.pdf",
                file_path=f"{doc_id}.pdf",
                content_hash=f"hash-{doc_id}",
                status="ready",
            )
            for doc_id, title in {
                "d1": "Hit Paper",
                "d3": "Cross Alpha",
                "d4": "Cross Beta",
                "d5": "Read Paper",
            }.items()
        }
        db.add_all(docs.values())
        await db.flush()

        def node(doc_id, node_id, num, title, page_start, page_end, start, end):
            return TocNode(
                doc_id=doc_id,
                node_id=node_id,
                level=2,
                heading_num=num,
                title=title,
                heading_path=f"{num} {title}",
                page_start=page_start,
                page_end=page_end,
                chunk_start=start,
                chunk_end=end,
            )

        db.add_all(
            [
                node("d1", "d1:h:2.1", "2.1", "Preparation", 12, 14, 0, 0),
                node("d3", "d3:h:2.1", "2.1", "Preparation", 12, 14, 0, 0),
                node("d4", "d4:h:2.1", "2.1", "Preparation", 12, 14, 0, 0),
                node("d5", "d5:h:2.1", "2.1", "Preparation Methods", 12, 14, 0, 0),
                node("d5", "d5:h:2.4", "2.4", "Leakage Baseline Title", 20, 21, 1, 1),
                node("d5", "d5:h:3", "3", "Combined Section", 12, 21, 0, 1),
            ]
        )

        chunks = {
            "d1": [
                Chunk(
                    doc_id="d1",
                    node_id="d1:h:2.1",
                    chunk_index=0,
                    content="hitneedle preparation procedure for thin films",
                    heading_path="2.1 Preparation",
                    page_start=12,
                    page_end=14,
                )
            ],
            "d3": [
                Chunk(
                    doc_id="d3",
                    node_id="d3:h:2.1",
                    chunk_index=0,
                    content="crossneedle " + "filler " * 40,
                    heading_path="2.1 Preparation",
                    page_start=12,
                    page_end=14,
                )
            ],
            "d4": [
                Chunk(
                    doc_id="d4",
                    node_id="d4:h:2.1",
                    chunk_index=0,
                    content="crossneedle crossneedle crossneedle",
                    heading_path="2.1 Preparation",
                    page_start=12,
                    page_end=14,
                )
            ],
            "d5": [
                Chunk(
                    doc_id="d5",
                    node_id="d5:h:2.1",
                    chunk_index=0,
                    content="clean section body",
                    heading_path="2.1 Preparation Methods",
                    page_start=12,
                    page_end=14,
                ),
                Chunk(
                    doc_id="d5",
                    node_id="d5:h:2.4",
                    chunk_index=1,
                    content="neighbour text mentioning Leakage Baseline Title",
                    heading_path="2.4 Leakage Baseline Title",
                    page_start=20,
                    page_end=21,
                ),
            ],
        }
        for doc_chunks in chunks.values():
            db.add_all(doc_chunks)
        await db.flush()
        for doc_id, doc_chunks in chunks.items():
            await index_document(db, docs[doc_id], doc_chunks)
        await db.commit()
    await engine.dispose()
    return db_path


def _write_golden(tmp_path: Path, rows: list[dict], name: str = "golden.jsonl") -> Path:
    path = tmp_path / name
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    return path


def _open(db_path: Path):
    # A fresh engine over the same file; the schema is already created.
    return make_engine(db_path)


# ---------------------------------------------------------------------------
# load_golden
# ---------------------------------------------------------------------------


def test_load_golden_maps_v1_flat_rows_and_keeps_unknown_kind(tmp_path):
    path = _write_golden(
        tmp_path,
        [
            {
                "id": "a",
                "query": "q one",
                "kind": "fact",
                "doc_id": "legacy-doc",
                "primary_heading_num": "§2.1",
                "expected_heading_nums": ["2.1", "2.2"],
                "expected_pages": [[12, 14]],
            },
            {
                "id": "b",
                "query": "q two",
                "kind": "a-kind-we-do-not-know",
                "expected": {"doc_id": "new-doc", "pages": [7, 8]},
                "doc_id": "legacy-doc",
            },
        ],
    )
    items = run_eval.load_golden(path)

    first = items[0]
    assert first["kind"] == "fact"
    assert first["expected"]["doc_id"] == "legacy-doc"
    assert first["expected"]["heading_nums"] == ["2.1", "2.2"]
    assert first["expected"]["primary_heading_num"] == "2.1"
    assert first["expected"]["pages"] == [[12, 14]]

    # expected.doc_id wins over a legacy top-level doc_id; unknown kind passes.
    second = items[1]
    assert second["kind"] == "a-kind-we-do-not-know"
    assert second["expected"]["doc_id"] == "new-doc"
    assert second["expected"]["pages"] == [[7, 8]]


def test_load_golden_rejects_missing_query(tmp_path):
    path = _write_golden(tmp_path, [{"id": "no-query", "kind": "fact"}])
    with pytest.raises(ValueError, match="no query"):
        run_eval.load_golden(path)


# ---------------------------------------------------------------------------
# eval_retrieval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eval_retrieval_hit_miss_and_cross_document(tmp_path):
    db_path = await _seed(tmp_path)
    golden = _write_golden(
        tmp_path,
        [
            {
                "id": "hit",
                "kind": "section",
                "query": "hitneedle",
                "expected": {
                    "doc_id": "d1",
                    "heading_nums": ["2.1"],
                    "primary_heading_num": "2.1",
                    "pages": [[12, 14]],
                },
            },
            {
                "id": "miss",
                "kind": "fact",
                "query": "missingneedle",
                "expected": {"heading_nums": ["9.9"], "pages": [[30, 31]]},
            },
            {
                # d4 repeats the token and ranks first; d3 is the expected doc.
                "id": "cross",
                "kind": "across",
                "query": "crossneedle",
                "expected": {
                    "doc_id": "d3",
                    "heading_nums": ["2.1"],
                    "primary_heading_num": "2.1",
                    "pages": [[12, 14]],
                },
            },
        ],
    )
    items = run_eval.load_golden(golden)

    engine = _open(db_path)
    Session = session_factory(engine)
    try:
        async with Session() as db:
            rows = await run_eval.eval_retrieval(db, items, top_k=5)
    finally:
        await engine.dispose()

    by_id = {row["id"]: row for row in rows}

    hit = by_id["hit"]
    assert hit["hit_at_k"] is True
    assert hit["correct_rank"] == 1
    assert hit["mrr"] == 1.0
    assert hit["primary_at_1"] is True
    assert hit["page_at_1"] is True

    miss = by_id["miss"]
    assert miss["hit_at_k"] is False
    assert miss["correct_rank"] is None
    assert miss["mrr"] == 0.0
    assert miss["primary_at_1"] is False
    assert miss["page_at_1"] is False

    cross = by_id["cross"]
    assert cross["hit_at_k"] is True
    assert cross["correct_rank"] == 2
    assert cross["mrr"] == 0.5
    # The top hit is the right heading in the wrong document: no credit.
    assert cross["primary_at_1"] is False
    assert cross["page_at_1"] is False


# ---------------------------------------------------------------------------
# eval_read_section
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eval_read_section_clean_and_leaks(tmp_path):
    db_path = await _seed(tmp_path)
    items = [
        run_eval.normalize_golden_item(
            {
                "id": "clean",
                "kind": "section",
                "query": "ignored",
                "expected": {"doc_id": "d5", "node_id": "d5:h:2.1", "pages": [[12, 14]]},
                "forbidden": {"node_ids": ["d5:h:2.4"]},
            }
        ),
        run_eval.normalize_golden_item(
            {
                # Spans both chunks, so the forbidden node's chunk is returned.
                "id": "leak-node",
                "query": "ignored",
                "expected": {"doc_id": "d5", "node_id": "d5:h:3", "pages": [[12, 21]]},
                "forbidden": {"node_ids": ["d5:h:2.4"]},
            }
        ),
        run_eval.normalize_golden_item(
            {
                # Same read, judged by the forbidden heading's title in the text.
                "id": "leak-text",
                "query": "ignored",
                "expected": {"doc_id": "d5", "node_id": "d5:h:3"},
                "forbidden": {"heading_nums": ["2.4"]},
            }
        ),
    ]

    engine = _open(db_path)
    Session = session_factory(engine)
    try:
        async with Session() as db:
            rows = await run_eval.eval_read_section(db, items)
    finally:
        await engine.dispose()

    by_id = {row["id"]: row for row in rows}

    clean = by_id["clean"]
    assert clean["citation_page_ok"] is True
    assert clean["leak"] is False
    assert clean["content_empty"] is False
    # citation_confidence arrives with round 03; absent is recorded as None.
    assert clean["confidence"] in (None, "high")

    assert by_id["leak-node"]["leak"] is True
    assert by_id["leak-node"]["citation_page_ok"] is True
    assert by_id["leak-text"]["leak"] is True
    # No expected pages on this item: the page metric is not judged.
    assert by_id["leak-text"]["citation_page_ok"] is None


# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------


def test_summarize_totals_and_by_kind():
    rows = [
        {
            "id": "a",
            "kind": "fact",
            "retrieval": {"hit_at_k": True, "primary_at_1": True, "page_at_1": True, "mrr": 1.0},
            "read_section": {"citation_page_ok": True, "leak": False, "confidence": "high"},
            "ask": {"ask_page_ok": True},
        },
        {
            "id": "b",
            "kind": "fact",
            "retrieval": {"hit_at_k": False, "primary_at_1": False, "page_at_1": False, "mrr": 0.0},
            "read_section": {"citation_page_ok": False, "leak": True, "confidence": "low"},
            "ask": {"ask_page_ok": False},
        },
        {
            "id": "c",
            "kind": "section",
            "retrieval": {"hit_at_k": True, "primary_at_1": False, "page_at_1": True, "mrr": 0.5},
            "read_section": None,
            "ask": None,
        },
    ]
    summary = run_eval.summarize(rows)

    totals = summary["totals"]
    assert totals["queries"] == 3
    assert totals["hit_at_k"] == 0.6667
    assert totals["primary_at_1"] == 0.3333
    assert totals["page_at_1"] == 0.6667
    assert totals["mrr"] == 0.5
    assert totals["citation_page_rate"] == 0.5
    assert totals["citation_leak_rate"] == 0.5
    assert totals["low_confidence_rate"] == 0.5
    assert totals["ask_page_rate"] == 0.5

    fact = summary["by_kind"]["fact"]
    assert fact["queries"] == 2
    assert fact["hit_at_k"] == 0.5
    assert fact["citation_leak_rate"] == 0.5

    section = summary["by_kind"]["section"]
    assert section["queries"] == 1
    assert section["hit_at_k"] == 1.0
    assert section["citation_page_rate"] is None
    assert section["ask_page_rate"] is None


# ---------------------------------------------------------------------------
# CLI / --fail-under exit codes
# ---------------------------------------------------------------------------


def test_main_fail_under_exit_codes(tmp_path):
    db_path = asyncio.run(_seed(tmp_path))
    golden = _write_golden(
        tmp_path,
        [
            {
                "id": "hit",
                "query": "hitneedle",
                "expected": {"doc_id": "d1", "heading_nums": ["2.1"], "pages": [[12, 14]]},
            },
            {
                "id": "cross",
                "query": "crossneedle",
                "expected": {"doc_id": "d3", "heading_nums": ["2.1"], "pages": [[12, 14]]},
            },
            {
                "id": "miss",
                "query": "missingneedle",
                "expected": {"heading_nums": ["9.9"], "pages": [[30, 31]]},
            },
        ],
    )
    base = ["retrieval", "--db", str(db_path), "--golden", str(golden), "--top-k", "5"]

    # 2 of 3 hit → 0.6667: below 0.9 fails, above 0.5 passes.
    assert run_eval.main(base + ["--fail-under", "hit_at_k=0.9"]) == 1
    assert run_eval.main(base + ["--fail-under", "hit_at_k=0.5"]) == 0


def test_main_missing_golden_returns_2(tmp_path):
    assert run_eval.main(
        ["retrieval", "--db", str(tmp_path / "none.db"), "--golden", str(tmp_path / "no.jsonl")]
    ) == 2
