"""LLM usage view fixes: doc_id tagging, token budget, export, by_endpoint."""
from __future__ import annotations

import json

import httpx
import pytest

from app.curation import llm_log
from app.curation.dispatch import Budget, Dispatcher
from app.config import Settings


# ── doc_id reaches the call log from the ingest TOC path ────────────────


def _ok_response(content: str):
    from types import SimpleNamespace

    return SimpleNamespace(
        status_code=200,
        json=lambda: {"choices": [{"message": {"content": content}}]},
    )


def test_ingest_llm_calls_carry_doc_id(tmp_path, monkeypatch):
    """The usage view's core promise is per-document cost; the ingest TOC
    calls must tag their doc_id or 98% of the log lands in 'unassigned'."""
    from app.ingest.llm_toc import detect_toc_pages_llm

    captured = {}

    def fake_complete(prompt, settings, **kwargs):
        captured.update(kwargs)
        return '{"toc_detected": "no"}'

    monkeypatch.setattr("app.ingest.llm_toc.llm_complete", fake_complete)
    settings = Settings(
        tocmemex_data_dir=tmp_path,
        tocmemex_llm_api_key="k",
        tocmemex_llm_model="m",
        tocmemex_llm_base_url="http://x/v1",
    )
    detect_toc_pages_llm(["page text"], settings, doc_id="doc-1")
    assert captured.get("doc_id") == "doc-1"

    # the bench path logs a doc_id without touching the disk cache
    from app.ingest.llm_toc import extract_toc_entries_llm

    captured.clear()
    monkeypatch.setattr(
        "app.ingest.llm_toc.llm_complete",
        lambda prompt, settings, **kw: captured.update(kw) or '{"table_of_contents": []}',
    )
    extract_toc_entries_llm(["1 前言"], settings, doc_id="doc-bench")
    assert captured.get("doc_id") == "doc-bench"


# ── token budget is real ────────────────────────────────────────────────


def test_budget_spend_tokens_depletes():
    budget = Budget(calls_left=10, tokens_left=100)
    assert budget.spend_tokens(60) is True
    assert budget.spend_tokens(60) is False
    assert budget.tokens_left == -20
    assert budget.state()["tokens_left"] == 0


@pytest.mark.asyncio
async def test_tool_loop_stops_when_token_budget_exhausted(tmp_path, monkeypatch):
    """A run that keeps burning tokens winds down instead of quadrupling the
    wall-clock estimate: the loop breaks after the turn that crossed the
    budget (its tool results still land, so the transcript stays valid)."""
    from app.db import init_db, make_engine, session_factory
    from app.llm.agent import run_tool_loop
    from app.models import CurationJob, Document, Namespace

    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(
            Document(
                id="d1", namespace_id="shared", title="Demo", filename="a.pdf",
                file_path="a.pdf", content_hash="h", page_count=3, status="ready",
            )
        )
        await db.flush()
        job = CurationJob(
            id="jb", issue_id="d1:short-chunks", doc_id="d1",
            kind="short-chunks", fingerprint="f",
        )
        db.add(job)
        await db.commit()
        doc = await db.get(Document, "d1")

        dispatcher = Dispatcher(db, tmp_path, "d1", job_id=job.id, budget=Budget(calls_left=10, tokens_left=50))

        turn = {"n": 0}

        def fake_chat(messages, settings, **kwargs):
            turn["n"] += 1
            from app.llm.client import ChatReply

            return ChatReply(
                content=None,
                tool_calls=[{
                    "id": f"c{turn['n']}",
                    "type": "function",
                    "function": {"name": "get_quality", "arguments": "{}"},
                }],
                tokens={"prompt": 40, "completion": 30, "total": 70,
                        "source": "api_usage"},
            )

        monkeypatch.setattr("app.llm.agent.llm_chat", fake_chat)
        result = await run_tool_loop(dispatcher, doc, max_calls=10)
        # 50-token budget: turn 1 spends 70 → over. The loop stops after
        # turn 1's tool result lands, so the transcript never dangles.
        assert turn["n"] == 1
        # the budget went negative; the state() view clamps to 0
        assert dispatcher.budget.tokens_left == -20
        assert dispatcher.budget.state()["tokens_left"] == 0
        assert result["tokens_left"] == -20
    await engine.dispose()


# ── export ──────────────────────────────────────────────────────────────


def _seed_log(tmp_path: "object", rows: list[dict]) -> None:
    path = llm_log.log_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_export_jsonl_is_verbatim_and_filtered(tmp_path):
    prompt = "全文提示词 " * 500
    content = "完整回答 " * 500
    # 老记录先落盘：没有明细文件，JSONL 导出应保留其预览原样
    _seed_log(tmp_path, [
        {"id": "2", "ts": "2026-11-18T02:00:00+00:00", "call_site": "curator.tool_loop",
         "endpoint": "e2", "model": "m2", "run_id": "run_b", "doc_id": "d2",
         "status": "error", "error": "boom",
         "tokens": {"prompt": 1, "completion": 0, "total": 1}},
    ])
    llm_log.append_call(
        tmp_path, call_site="curator.tool_loop", endpoint="e1", model="m1",
        prompt=prompt, content=content,
        tokens={"prompt": 10, "completion": 5, "total": 15, "source": "api_usage"},
        elapsed_ms=10, run_id="run_a", doc_id="d1",
    )
    body, media, filename = llm_log.export_calls(tmp_path, format="jsonl")
    assert media == "application/x-ndjson"
    assert filename.startswith("llm-calls-")
    rows = [json.loads(line) for line in body.splitlines()]
    assert len(rows) == 2  # uncapped export: everything
    full = next(r for r in rows if r.get("run_id") == "run_a")
    assert full["request"]["prompt"] == prompt        # 完整，不是 800 字预览
    assert full["response"]["content"] == content
    legacy = next(r for r in rows if r.get("run_id") == "run_b")
    assert legacy.get("request", {}).get("prompt") is None  # 无明细的老记录保持原样

    body_b, _, _ = llm_log.export_calls(tmp_path, run_id="run_b", format="jsonl")
    rows_b = [json.loads(line) for line in body_b.splitlines()]
    assert [row["id"] for row in rows_b] == ["2"]


def test_export_csv_flattens(tmp_path):
    _seed_log(tmp_path, [
        {"id": "1", "ts": "2026-11-18T01:00:00+00:00", "call_site": "llm_complete",
         "endpoint": "e1", "model": "m1", "run_id": "run_a", "doc_id": "d1",
         "status": "ok", "elapsed_ms": 900, "retries": 0,
         "tokens": {"prompt": 10, "completion": 5, "total": 15, "source": "api_usage"},
         "request": {"prompt_chars": 100}, "response": {"content_chars": 20},
         "error": None},
    ])
    body, media, filename = llm_log.export_calls(tmp_path, format="csv")
    assert media == "text/csv"
    assert filename.endswith(".csv")
    lines = body.strip().splitlines()
    assert lines[0].startswith("ts,call_site,endpoint")
    assert "15" in lines[1] and "api_usage" in lines[1]


def test_export_csv_neutralizes_formula_injection(tmp_path):
    """S5: a cell starting with = + - @ must not open as a spreadsheet formula.

    Model name / endpoint / error are attacker-influenced; a leading '=' would
    be evaluated (DDE, HYPERLINK) when the CSV is opened. Each such cell is
    prefixed with a single quote so the spreadsheet shows it literally.
    """
    _seed_log(tmp_path, [
        {"id": "1", "ts": "2026-11-18T01:00:00+00:00", "call_site": "llm_complete",
         "endpoint": "=cmd|' /C calc'!A0", "model": "+SUM(A1)", "run_id": "run_a",
         "doc_id": "d1", "status": "error", "error": "@evil",
         "tokens": {"prompt": 1, "completion": 0, "total": 1}},
    ])
    body, _, _ = llm_log.export_calls(tmp_path, format="csv")
    import csv as _csv
    import io as _io

    rows = list(_csv.reader(_io.StringIO(body)))
    header, data = rows[0], rows[1]
    cell = dict(zip(header, data))
    # Every dangerous field is neutralized with a leading apostrophe.
    assert cell["endpoint"].startswith("'") and "cmd" in cell["endpoint"]
    assert cell["model"].startswith("'")
    assert cell["error"].startswith("'")
    # The leading formula characters no longer start the raw cell.
    for field in ("endpoint", "model", "error"):
        assert not cell[field][0] in ("=", "+", "-", "@")



def test_count_calls_matches_read_calls(tmp_path):
    _seed_log(tmp_path, [
        {"id": str(i), "ts": f"2026-11-18T0{i}:00:00+00:00", "call_site": "llm_complete",
         "run_id": "run_a" if i % 2 else None, "status": "ok",
         "tokens": {"prompt": 1, "completion": 1, "total": 2}}
        for i in range(5)
    ])
    assert llm_log.count_calls(tmp_path) == 5
    assert llm_log.count_calls(tmp_path, run_id="run_a") == 2
    page = llm_log.read_calls(tmp_path, limit=2, offset=0)
    assert len(page) == 2


def test_usage_summary_groups_by_endpoint(tmp_path):
    _seed_log(tmp_path, [
        {"id": "1", "ts": "2026-11-18T01:00:00+00:00", "call_site": "a",
         "endpoint": "http://e1/v1", "model": "m1", "status": "ok",
         "tokens": {"prompt": 10, "completion": 10, "total": 20}},
        {"id": "2", "ts": "2026-11-18T01:01:00+00:00", "call_site": "a",
         "endpoint": "http://e2/v1", "model": "m2", "status": "error",
         "tokens": {"prompt": 5, "completion": 0, "total": 5}},
    ])
    summary = llm_log.usage_summary(tmp_path)
    endpoints = {row["endpoint"]: row for row in summary["by_endpoint"]}
    assert set(endpoints) == {"http://e1/v1", "http://e2/v1"}
    assert endpoints["http://e1/v1"]["tokens"] == 20
    assert endpoints["http://e2/v1"]["errors"] == 1


# ── full-detail per-call log and export ─────────────────────────────────


def test_append_call_full_mode_writes_detail_file(tmp_path):
    """full 模式下每次调用额外落一份完整输入输出；JSONL 仍只有预览。"""
    prompt = "全文提示词 " * 500  # 远超 800 字预览
    content = "完整回答 " * 500
    llm_log.append_call(
        tmp_path, call_site="curator.tool_loop", endpoint="http://e/v1",
        model="m", prompt=prompt, content=content,
        tokens={"prompt": 100, "completion": 50, "total": 150, "source": "api_usage"},
        elapsed_ms=120, run_id="run_1", doc_id="d1",
    )
    record = json.loads(llm_log.log_path(tmp_path).read_text(encoding="utf-8").splitlines()[0])
    assert len(record["request"]["prompt_preview"]) <= llm_log._PREVIEW_CHARS

    detail = llm_log.read_call_detail(tmp_path, record["id"])
    assert detail["request"]["prompt"] == prompt      # 完整输入
    assert detail["response"]["content"] == content   # 完整输出
    assert detail["run_id"] == "run_1"
    assert detail["response"]["tool_calls"] == []


def test_append_call_meta_mode_writes_no_detail(tmp_path):
    llm_log.append_call(
        tmp_path, call_site="llm_complete", endpoint="e", model="m",
        prompt="p", content="c",
        tokens={"prompt": 1, "completion": 1, "total": 2, "source": "estimated"},
        elapsed_ms=1, log_mode="meta",
    )
    record = json.loads(llm_log.log_path(tmp_path).read_text(encoding="utf-8").splitlines()[0])
    assert record["request"]["prompt_preview"] == "<1 chars>"
    # meta 模式没有全文可导出，回退到 JSONL 记录（其 request 里没有 prompt）
    detail = llm_log.read_call_detail(tmp_path, record["id"])
    assert detail["id"] == record["id"]
    assert "prompt" not in detail["request"]


def test_export_call_detail_round_trip(tmp_path):
    llm_log.append_call(
        tmp_path, call_site="curator.tool_loop", endpoint="e", model="m",
        prompt="完整提示", content="完整回答",
        tokens={"prompt": 2, "completion": 2, "total": 4, "source": "api_usage"},
        elapsed_ms=5, run_id="run_1",
    )
    record_id = json.loads(
        llm_log.log_path(tmp_path).read_text(encoding="utf-8").splitlines()[0]
    )["id"]
    record, media, filename = llm_log.export_call_detail(tmp_path, record_id)
    assert media == "application/json"
    assert filename == f"llm-call-{record_id}.json"
    assert record["request"]["prompt"] == "完整提示"
    assert record["response"]["content"] == "完整回答"

    assert llm_log.export_call_detail(tmp_path, "missing-id")[0] is None


def test_export_call_detail_falls_back_to_jsonl_record(tmp_path):
    """detail 文件出现之前的旧记录：回退到 JSONL 行，而不是 404。"""
    _seed_log(tmp_path, [
        {"id": "legacy", "ts": "2026-11-18T01:00:00+00:00", "call_site": "a",
         "status": "ok", "tokens": {"prompt": 1, "completion": 1, "total": 2},
         "request": {"prompt_chars": 10, "prompt_preview": "head"},
         "response": {"content_chars": 5, "content_preview": "tail"}},
    ])
    record, _, _ = llm_log.export_call_detail(tmp_path, "legacy")
    assert record["request"]["prompt_preview"] == "head"


def test_export_call_detail_rejects_malformed_ids(tmp_path):
    """路径穿越 / 非法 id 一律按「未找到」处理，不触碰文件系统。"""
    for bad in ("../../secrets", "a/b", "..", "", "x" * 65, "%2e%2e"):
        assert llm_log.read_call_detail(tmp_path, bad) is None
        assert llm_log.export_call_detail(tmp_path, bad)[0] is None


@pytest.mark.asyncio
async def test_call_detail_export_endpoint(tmp_path, monkeypatch):
    from app.web_api import create_app

    app = create_app(db_path=tmp_path / "tocmemex.db")
    app.state.data_dir = tmp_path
    llm_log.append_call(
        tmp_path, call_site="curator.tool_loop", endpoint="e", model="m",
        prompt="P" * 50, content="C" * 50,
        tokens={"prompt": 1, "completion": 1, "total": 2, "source": "api_usage"},
        elapsed_ms=1,
    )
    call_id = json.loads(
        llm_log.log_path(tmp_path).read_text(encoding="utf-8").splitlines()[0]
    )["id"]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.get(f"/api/llm/calls/{call_id}/export")
        assert response.status_code == 200
        body = response.json()
        assert body["request"]["prompt"] == "P" * 50
        missing = await client.get("/api/llm/calls/nope/export")
        assert missing.status_code == 404
