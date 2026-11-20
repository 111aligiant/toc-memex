from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.curation import llm_log
from app.curation.baselines import create_baseline
from app.db import init_db, make_engine, session_factory
from app.models import Chunk, Document, Namespace, TocNode
from app.web_api import create_app


async def _seed(tmp_path):
    db_path = tmp_path / "tocmemex.db"
    engine = make_engine(db_path)
    await init_db(engine)
    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(
            Document(
                id="d1",
                namespace_id="shared",
                title="Demo",
                filename="demo.pdf",
                file_path="demo.pdf",
                content_hash="h1",
                page_count=2,
                status="ready",
                summary="baseline summary",
            )
        )
        await db.flush()
        db.add(
            TocNode(
                doc_id="d1",
                node_id="d1:h:1",
                level=1,
                heading_num="1",
                title="1 范围",
                heading_path="1 范围",
                page_start=0,
                page_end=1,
            )
        )
        await db.flush()
        db.add(
            Chunk(
                doc_id="d1",
                node_id="d1:h:1",
                chunk_index=0,
                content="正文内容",
                heading_path="1 范围",
                page_start=0,
                page_end=1,
            )
        )
        await db.commit()
    return db_path, engine


def _write_ocr_cache(data_dir, doc_id: str) -> None:
    """Give the document a parse cache so evidence checks can pass."""
    from app.ocr.cache import save_pages
    from app.ocr.models import OcrBlock, OcrPage, ParsedDocument

    cache = data_dir / "ocr" / doc_id
    cache.mkdir(parents=True, exist_ok=True)
    save_pages(
        cache,
        ParsedDocument(
            pages=[
                OcrPage(
                    page_index=0,
                    width=100,
                    height=200,
                    markdown="",
                    blocks=[
                        OcrBlock("paragraph_title", "1 范围", (0, 0, 10, 10), 0),
                        OcrBlock("text", "正文内容", (0, 20, 10, 30), 1),
                    ],
                ),
                OcrPage(
                    page_index=1,
                    width=100,
                    height=200,
                    markdown="",
                    blocks=[OcrBlock("text", "第二页正文", (0, 20, 10, 30), 0)],
                ),
            ]
        ),
    )


@pytest.mark.asyncio
async def test_baseline_create_list_restore_api(tmp_path):
    db_path, engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        row = await create_baseline(db, "d1", "pre_curator_write")
        await db.commit()
        baseline_id = row.id

        doc = await db.get(Document, "d1")
        doc.summary = "curator changed this"
        await db.commit()
    await engine.dispose()

    app = create_app(db_path, tmp_path)
    with TestClient(app) as client:
        listed = client.get("/api/documents/d1/baselines").json()
        assert listed["baselines"][0]["id"] == baseline_id
        assert listed["baselines"][0]["matches_current"] is True
        assert listed["baselines"][0]["node_count"] == 1

        refused = client.post("/api/documents/d1/restore-baseline", json={})
        assert refused.status_code == 400

        restored = client.post(
            "/api/documents/d1/restore-baseline",
            json={"baseline_id": baseline_id, "confirm": True},
        )
        assert restored.status_code == 200, restored.text
        assert restored.json()["restored_nodes"] == 1

        detail = client.get("/api/documents/d1").json()
        assert detail["document"]["summary"] == "baseline summary"


@pytest.mark.asyncio
async def test_restore_hash_mismatch_returns_409_with_force_hint(tmp_path):
    db_path, engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        row = await create_baseline(db, "d1", "pre_curator_write")
        await db.commit()
        baseline_id = row.id
        doc = await db.get(Document, "d1")
        doc.content_hash = "h2"
        await db.commit()
    await engine.dispose()

    app = create_app(db_path, tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/documents/d1/restore-baseline",
            json={"baseline_id": baseline_id, "confirm": True},
        )
        assert response.status_code == 409
        assert response.json()["detail"]["can_force"] is True

        forced = client.post(
            "/api/documents/d1/restore-baseline",
            json={"baseline_id": baseline_id, "confirm": True, "force": True},
        )
        assert forced.status_code == 200
        assert forced.json()["forced"] is True


@pytest.mark.asyncio
async def test_restore_unknown_baseline_returns_404(tmp_path):
    db_path, engine = await _seed(tmp_path)
    await engine.dispose()
    app = create_app(db_path, tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/documents/d1/restore-baseline", json={"confirm": True}
        )
        assert response.status_code == 404


def test_llm_usage_endpoints_read_the_jsonl_log(tmp_path):
    data_dir = tmp_path / "data"
    settings = Settings(tocmemex_data_dir=data_dir, tocmemex_llm_log="full")
    llm_log.append_call(
        settings.tocmemex_data_dir,
        call_site="curator.tool_loop",
        endpoint="e",
        model="m",
        prompt="hello",
        content="world",
        tokens={"prompt": 10, "completion": 4, "total": 14, "source": "api_usage"},
        elapsed_ms=12,
        run_id="run_1",
        doc_id="d1",
        log_mode=settings.tocmemex_llm_log,
    )
    llm_log.append_call(
        settings.tocmemex_data_dir,
        call_site="llm_toc",
        endpoint="e",
        model="m",
        prompt="x",
        content=None,
        tokens={"prompt": 2, "completion": 0, "total": 2, "source": "estimated"},
        elapsed_ms=3,
        status="error",
        error="boom",
        log_mode=settings.tocmemex_llm_log,
    )

    app = create_app(tmp_path / "tocmemex.db", data_dir)
    with TestClient(app) as client:
        usage = client.get("/api/llm/usage").json()
        assert usage["total"]["calls"] == 2
        assert usage["total"]["tokens_in"] == 12
        assert usage["total"]["errors"] == 1
        assert any(run["run_id"] == "run_1" for run in usage["by_run"])

        calls = client.get("/api/llm/calls", params={"doc_id": "d1"}).json()
        assert len(calls["calls"]) == 1
        assert calls["calls"][0]["tokens"]["source"] == "api_usage"


def test_llm_log_estimates_tokens_when_usage_missing():
    tokens = llm_log.usage_from_response({}, "中文提示", "answer")
    assert tokens["source"] == "estimated"
    assert tokens["total"] == tokens["prompt"] + tokens["completion"]


def test_llm_log_reads_api_usage_exactly():
    payload = {"usage": {"prompt_tokens": 42, "completion_tokens": 8, "total_tokens": 50}}
    tokens = llm_log.usage_from_response(payload, "prompt", "content")
    assert tokens == {"prompt": 42, "completion": 8, "total": 50, "source": "api_usage"}


def test_llm_calls_api_accepts_since_and_rejects_garbage(tmp_path):
    """`since` is an ISO-8601 filter; a typo is a 400, not a silent no-match."""
    data_dir = tmp_path / "data"
    settings = Settings(tocmemex_data_dir=data_dir, tocmemex_llm_log="full")
    llm_log.append_call(
        settings.tocmemex_data_dir,
        call_site="t", endpoint="e", model="m", prompt="p", content="c",
        tokens={"prompt": 1, "completion": 1, "total": 2, "source": "api_usage"},
        elapsed_ms=1, log_mode="full",
    )
    app = create_app(tmp_path / "tocmemex.db", data_dir)
    with TestClient(app) as client:
        ok = client.get("/api/llm/calls", params={"since": "2026-01-01T00:00:00+00:00"})
        assert ok.status_code == 200
        assert len(ok.json()["calls"]) == 1

        bad = client.get("/api/llm/calls", params={"since": "not-a-date"})
        assert bad.status_code == 400
        assert "ISO-8601" in bad.json()["detail"]


@pytest.mark.asyncio
async def test_pending_proposal_can_be_listed_and_approved(tmp_path):
    """A set_page op is L2: it lands as pending, then a human approves it."""
    db_path, engine = await _seed(tmp_path)
    _write_ocr_cache(tmp_path, "d1")
    async with session_factory(engine)() as db:
        from app.curation.dispatch import Dispatcher

        dispatcher = Dispatcher(db, tmp_path, "d1")
        result = await dispatcher.dispatch(
            "propose_ops",
            {
                "ops": [
                    {"op": "set_page", "node_id": "d1:h:1", "page_start": 0,
                     "page_end": 1, "evidence": "1 范围"},
                ],
                "rationale": "expand the parent range",
            },
        )
        await db.commit()
        assert result["pending_ops"] == 1
        proposal_id = result["proposal_id"]
    await engine.dispose()

    app = create_app(db_path, tmp_path)
    with TestClient(app) as client:
        listed = client.get("/api/curation").json()
        assert listed["count"] == 1
        row = listed["proposals"][0]
        assert row["docId"] == "d1"
        assert row["ops"][0]["op"] == "set_page"
        assert row["results"][0]["gate"] == "pending_human"

        approved = client.post(f"/api/curation/{proposal_id}/approve")
        assert approved.status_code == 200, approved.text

        after = client.get("/api/curation").json()
        assert after["count"] == 0

        detail = client.get("/api/documents/d1").json()
        node = next(n for n in detail["nodes"] if n["node_id"] == "d1:h:1")
        assert node["page_end"] == 1


@pytest.mark.asyncio
async def test_rejecting_a_proposal_changes_nothing(tmp_path):
    db_path, engine = await _seed(tmp_path)
    _write_ocr_cache(tmp_path, "d1")
    async with session_factory(engine)() as db:
        from app.curation.dispatch import Dispatcher

        dispatcher = Dispatcher(db, tmp_path, "d1")
        result = await dispatcher.dispatch(
            "propose_ops",
            {"ops": [{"op": "set_page", "node_id": "d1:h:1", "page_start": 0,
                      "page_end": 1, "evidence": "1 范围"}]},
        )
        await db.commit()
        proposal_id = result["proposal_id"]
    await engine.dispose()

    app = create_app(db_path, tmp_path)
    with TestClient(app) as client:
        rejected = client.post(f"/api/curation/{proposal_id}/reject")
        assert rejected.status_code == 200
        assert client.get("/api/curation").json()["count"] == 0
        detail = client.get("/api/documents/d1").json()
        node = next(n for n in detail["nodes"] if n["node_id"] == "d1:h:1")
        assert node["page_end"] == 1  # the seed value, unchanged
