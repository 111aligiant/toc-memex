from __future__ import annotations

import io
import json
import shutil
import sqlite3
import time
import zipfile

import httpx
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select as sqlalchemy_select

from app.db import init_db, make_engine, session_factory
from app.models import AgentNote, Chunk, Collection, Document, Namespace, Resource, TocNode
from app.web_api import compute_doc_stats, create_app

FIXTURE = Path(__file__).parent / "fixtures" / "paddleocr_sample.jsonl"


async def seed_database(tmp_path: Path) -> Path:
    db_path = tmp_path / "tocmemex.db"
    engine = make_engine(db_path)
    await init_db(engine)
    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(
            Document(
                id="doc1",
                namespace_id="shared",
                title="Demo document",
                filename="demo.pdf",
                file_path="demo.pdf",
                content_hash="hash",
                page_count=4,
                status="ready",
            )
        )
        await db.flush()
        db.add_all(
            [
                TocNode(
                    doc_id="doc1",
                    node_id="d:1",
                    parent_id=None,
                    level=1,
                    heading_num="1",
                    title="1 范围",
                    heading_path="1 范围",
                    page_start=0,
                    page_end=3,
                    chunk_start=0,
                    chunk_end=1,
                    source="bookmark",
                    confidence=1.0,
                ),
                TocNode(
                    doc_id="doc1",
                    node_id="d:1.1",
                    parent_id="d:1",
                    level=2,
                    heading_num="1.1",
                    title="1.1 目的",
                    heading_path="1 范围 > 1.1 目的",
                    page_start=1,
                    page_end=2,
                    chunk_start=1,
                    chunk_end=1,
                    source="bookmark",
                    confidence=1.0,
                ),
            ]
        )
        db.add_all(
            [
                Chunk(
                    doc_id="doc1",
                    node_id="d:1",
                    chunk_index=0,
                    content="温度试验内容一",
                    heading_path="1 范围",
                    page_start=0,
                    page_end=1,
                    token_count=3,
                ),
                Chunk(
                    doc_id="doc1",
                    node_id="d:1.1",
                    chunk_index=1,
                    content="温度试验内容二",
                    heading_path="1 范围 > 1.1 目的",
                    page_start=1,
                    page_end=2,
                    token_count=3,
                ),
            ]
        )
        db.add(
            Resource(
                doc_id="doc1",
                resource_id="tbl_1_0",
                type="table",
                title="表1 试验条件",
                content="<table/>",
                page_idx=1,
            )
        )
        await db.commit()
    # populate FTS the way ingest would
    from app.index.fts import index_document

    engine = make_engine(db_path)
    async with session_factory(engine)() as db:
        doc = await db.get(Document, "doc1")
        chunks = (await db.execute(sqlalchemy_select(Chunk).where(Chunk.doc_id == "doc1"))).scalars().all()
        await index_document(db, doc, chunks)
        await db.commit()
    await engine.dispose()
    return db_path


def make_client(tmp_path: Path) -> TestClient:
    db_path = tmp_path / "tocmemex.db"
    app = create_app(db_path, tmp_path)
    return TestClient(app)


def wait_upload(client: TestClient, upload_id: str, timeout: float = 60.0) -> dict:
    """Poll a detached ingest task; the POST returns before it finishes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = client.get(f"/api/uploads/{upload_id}").json()
        if payload["status"] in {"ready", "duplicate", "failed"}:
            return payload
        time.sleep(0.15)
    raise AssertionError(f"upload {upload_id} did not finish in {timeout}s")


@pytest.mark.asyncio
async def test_api_upload_ingests_and_files_into_collection(tmp_path):
    db_path = await seed_database(tmp_path)
    engine = make_engine(db_path)
    async with session_factory(engine)() as db:
        db.add(Collection(id="col1", name="标准资料", description="test"))
        await db.commit()
    await engine.dispose()

    with make_client(tmp_path) as client:
        response = client.post(
            "/api/uploads",
            files={"file": ("note.md", "# 标题\n\n正文内容".encode(), "text/markdown")},
            data={"collectionId": "col1"},
        )
        assert response.status_code == 200
        upload = response.json()["upload"]
        assert upload["status"] in {"queued", "indexing", "ready"}

        status = wait_upload(client, upload["id"])
        assert status["status"] == "ready", status
        assert status["docId"]
        document = client.get(f"/api/documents/{status['docId']}").json()["document"]
        assert document["title"] == "note"
        assert document["collectionId"] == "col1"
        assert document["collectionName"] == "标准资料"


@pytest.mark.asyncio
async def test_api_upload_duplicate_returns_existing_document(tmp_path):
    await seed_database(tmp_path)
    payload = ("# 标题\n\n正文内容".encode(), "text/markdown")
    with make_client(tmp_path) as client:
        first = client.post("/api/uploads", files={"file": ("note.md", *payload)}).json()["upload"]
        first_status = wait_upload(client, first["id"])
        assert first_status["status"] == "ready"

        second = client.post("/api/uploads", files={"file": ("note.md", *payload)}).json()["upload"]
        second_status = wait_upload(client, second["id"])
        assert second_status["status"] == "duplicate"
        assert second_status["docId"] == first_status["docId"]
        assert "未重复入库" in second_status["message"]


@pytest.mark.asyncio
async def test_api_upload_rejects_unsupported_and_empty_files(tmp_path):
    await seed_database(tmp_path)
    with make_client(tmp_path) as client:
        bad = client.post(
            "/api/uploads", files={"file": ("notes.exe", b"nope", "application/octet-stream")}
        )
        assert bad.status_code == 400
        empty = client.post(
            "/api/uploads", files={"file": ("empty.md", b"", "text/markdown")}
        )
        assert empty.status_code == 400
        for name, media in (
            ("old.xls", "application/vnd.ms-excel"),
            (
                "book.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
            ("rows.csv", "text/csv"),
            ("rows.tsv", "text/tab-separated-values"),
        ):
            sheet = client.post("/api/uploads", files={"file": (name, b"a,b\n1,2", media)})
            assert sheet.status_code == 400
            assert "暂未开放" in sheet.json()["detail"]


@pytest.mark.asyncio
async def test_api_upload_reports_missing_collection_without_losing_document(tmp_path):
    await seed_database(tmp_path)
    with make_client(tmp_path) as client:
        response = client.post(
            "/api/uploads",
            files={"file": ("note.md", "# 标题\n\n正文内容".encode(), "text/markdown")},
            data={"collectionId": "missing"},
        )
        assert response.status_code == 200
        status = wait_upload(client, response.json()["upload"]["id"])
        assert status["status"] == "ready"
        assert "分区不存在" in status["message"]
        assert status["docId"]


@pytest.mark.asyncio
async def test_api_overview(tmp_path):
    db_path = await seed_database(tmp_path)
    with make_client(tmp_path) as client:
        data = client.get("/api/overview").json()
    assert data["stats"]["docs"] == 1
    assert data["stats"]["ready"] == 1
    assert data["stats"]["pages"] == 4
    assert data["stats"]["chunks"] == 2


@pytest.mark.asyncio
async def test_api_documents_summary(tmp_path):
    await seed_database(tmp_path)
    with make_client(tmp_path) as client:
        data = client.get("/api/documents").json()
    doc = data["documents"][0]
    assert doc["id"] == "doc1"
    assert doc["ns"] == "shared"
    assert doc["type"] == "pdf"
    assert doc["pages"] == 4
    assert doc["nodes"] == 2
    assert doc["chunks"] == 2
    assert doc["covered"] == 3
    assert doc["contentPages"] == 4
    assert doc["status"] == "ready"
    assert doc["updatedAt"]


@pytest.mark.asyncio
async def test_api_document_search_ignores_summary_mentions(tmp_path):
    """A document that only mentions a term in its summary is not a match.

    The summary is the first ~300 chars of the body, and 26 HB 6167 parts
    open with the same preamble listing all 26 part names — so 「盐雾」
    appears in every one of their summaries while only HB 6167.12 is
    actually about salt fog. Matching summaries would return the whole
    family for every query; keywords and TOC headings carry the topic.
    """
    db_path = await seed_database(tmp_path)
    engine = make_engine(db_path)
    async with session_factory(engine)() as db:
        db.add(
            Document(
                id="doc2",
                namespace_id="shared",
                title="HB 6167.1-2014 总则",
                filename="hb.pdf",
                file_path="hb.pdf",
                content_hash="hash2",
                page_count=2,
                status="ready",
                summary="本系列包括：第1部分：总则；第12部分：盐雾试验。",
            )
        )
        await db.flush()
        db.add(
            Document(
                id="doc3",
                namespace_id="shared",
                title="HB 6167.12-2014 第12部分",
                filename="hb12.pdf",
                file_path="hb12.pdf",
                content_hash="hash3",
                page_count=2,
                status="ready",
                summary="本系列包括：第1部分：总则；第12部分：盐雾试验。",
                keywords="盐雾, 试验, 民用飞机",
            )
        )
        await db.flush()
        db.add(
            TocNode(
                doc_id="doc3",
                node_id="d3:1",
                parent_id=None,
                level=1,
                heading_num="1",
                title="1 盐雾试验",
                heading_path="1 盐雾试验",
                page_start=0,
                page_end=1,
                source="bookmark",
                confidence=1.0,
            )
        )
        await db.commit()
        from app.index.fts import index_document

        for doc_id in ("doc2", "doc3"):
            doc = await db.get(Document, doc_id)
            await index_document(db, doc, [])
        await db.commit()
    await engine.dispose()

    with make_client(tmp_path) as client:
        ids = client.get("/api/documents/search", params={"q": "盐雾"}).json()["ids"]

    assert "doc3" in ids
    assert "doc2" not in ids, "a summary-only mention is the shared preamble, not the topic"


@pytest.mark.asyncio
async def test_api_document_detail_with_resources(tmp_path):
    await seed_database(tmp_path)
    with make_client(tmp_path) as client:
        data = client.get("/api/documents/doc1").json()
    assert data["document"]["title"] == "Demo document"
    assert data["document"]["ns"] == "shared"
    assert len(data["nodes"]) == 2
    assert len(data["chunks"]) == 2
    assert data["stats"]["coveredPages"] == 3
    chunk = next(c for c in data["chunks"] if c["chunk_index"] == 1)
    assert chunk["resources"][0]["id"] == "tbl_1_0"
    assert chunk["resources"][0]["type"] == "table"


@pytest.mark.asyncio
async def test_each_resource_is_attached_to_exactly_one_chunk(tmp_path):
    """Both seeded chunks cover page 1; the table must appear only once.

    Page-overlap attachment used to put it on both, which is why the same
    table showed up repeatedly in the reading stream.
    """
    await seed_database(tmp_path)
    with make_client(tmp_path) as client:
        data = client.get("/api/documents/doc1").json()
    holders = [
        chunk["chunk_index"]
        for chunk in data["chunks"]
        if any(resource["id"] == "tbl_1_0" for resource in chunk["resources"])
    ]
    assert holders == [1]
    assert sum(len(chunk["resources"]) for chunk in data["chunks"]) == 1


@pytest.mark.asyncio
async def test_node_bound_resource_goes_to_the_chunk_covering_its_page(tmp_path):
    """A narrower chunk elsewhere in the same section must not win on span."""
    db_path = await seed_database(tmp_path)
    engine = make_engine(db_path)
    async with session_factory(engine)() as db:
        db.add(
            Chunk(
                doc_id="doc1",
                node_id="d:1.1",
                chunk_index=2,
                content="更窄的切片",
                heading_path="1 范围 > 1.1 目的",
                page_start=3,
                page_end=3,
                token_count=3,
            )
        )
        resource = (
            await db.execute(
                sqlalchemy_select(Resource).where(Resource.doc_id == "doc1")
            )
        ).scalar_one()
        resource.node_id = "d:1.1"
        await db.commit()
    await engine.dispose()

    with make_client(tmp_path) as client:
        data = client.get("/api/documents/doc1").json()
    holders = [
        chunk["chunk_index"]
        for chunk in data["chunks"]
        if any(resource["id"] == "tbl_1_0" for resource in chunk["resources"])
    ]
    assert holders == [1]


@pytest.mark.asyncio
async def test_api_document_not_found(tmp_path):
    await seed_database(tmp_path)
    with make_client(tmp_path) as client:
        assert client.get("/api/documents/missing").status_code == 404


@pytest.mark.asyncio
async def test_api_original_file_is_served_inline(tmp_path):
    await seed_database(tmp_path)
    original = tmp_path / "demo.pdf"
    original.write_bytes(b"%PDF-1.4 original")
    with sqlite3.connect(tmp_path / "tocmemex.db") as connection:
        connection.execute(
            "UPDATE documents SET file_path = ? WHERE id = 'doc1'", (str(original),)
        )
        connection.commit()

    with make_client(tmp_path) as client:
        response = client.get("/api/documents/doc1/file")

    assert response.status_code == 200
    assert response.content == b"%PDF-1.4 original"
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("inline")


@pytest.mark.asyncio
async def test_api_original_file_missing_is_404(tmp_path):
    await seed_database(tmp_path)  # file_path points at a demo.pdf that never existed
    with make_client(tmp_path) as client:
        assert client.get("/api/documents/doc1/file").status_code == 404


@pytest.mark.asyncio
async def test_spa_rejects_path_traversal(tmp_path):
    await seed_database(tmp_path)
    web_dir = tmp_path / "dist"
    web_dir.mkdir()
    (web_dir / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    app = create_app(tmp_path / "tocmemex.db", tmp_path, web_dir)
    with TestClient(app) as client:
        ok = client.get("/")
        assert ok.status_code == 200
        traversal = client.get("/..%2Fsecret.txt")
        assert b"secret" not in traversal.content
        nested = client.get("/../secret.txt")
        assert b"secret" not in nested.content


@pytest.mark.asyncio
async def test_api_namespaces(tmp_path):
    await seed_database(tmp_path)
    with make_client(tmp_path) as client:
        data = client.get("/api/namespaces").json()
    assert data["namespaces"][0]["id"] == "shared"
    assert data["namespaces"][0]["count"] == 1


@pytest.mark.asyncio
async def test_api_search(tmp_path):
    await seed_database(tmp_path)
    with make_client(tmp_path) as client:
        data = client.get("/api/search", params={"q": "温度", "top": 5}).json()
    assert data["query"] == "温度"
    assert len(data["results"]) >= 1
    result = data["results"][0]
    assert result["docId"] == "doc1"
    assert result["chunkId"]


@pytest.mark.asyncio
async def test_api_quality(tmp_path):
    await seed_database(tmp_path)
    with make_client(tmp_path) as client:
        data = client.get("/api/quality").json()
    assert "doc1" in data["checks"]
    labels = [item["label"] for item in data["checks"]["doc1"]]
    assert labels == ["无直接切片", "叶子无切片", "未归属切片", "短切片", "未定位节点", "缺失页"]


@pytest.mark.asyncio
async def test_api_quality_summary_is_self_consistent(tmp_path):
    """The summary must add up: per-kind counts are what `issues` totals.

    The quality page used to show three numbers from three different
    computations (sum of doc warnings, sum of group counts, a frontend
    page-gap total) that never matched. This pins one口径.
    """
    await seed_database(tmp_path)
    with make_client(tmp_path) as client:
        data = client.get("/api/quality").json()
    summary = data["summary"]
    assert summary["issues"] == sum(group["count"] for group in data["groups"])
    assert summary["groups"] == len(data["groups"])
    assert sum(summary["byKind"].values()) == summary["issues"]
    assert summary["traced"] == sum(1 for group in data["groups"] if group.get("noteCount"))
    # TOC warnings are reported separately, never folded into the total
    assert "tocWarnings" in summary


@pytest.mark.asyncio
async def test_api_settings_reports_effective_chunk_params(tmp_path):
    """The settings page shows real values, not literals in the template."""
    await seed_database(tmp_path)
    with make_client(tmp_path) as client:
        data = client.get("/api/settings").json()
    assert data["chunk"]["maxChars"] > 0
    assert data["chunk"]["overlap"] >= 0
    assert data["readOnly"] is True


@pytest.mark.asyncio
async def test_api_jobs_derived_from_status(tmp_path):
    await seed_database(tmp_path)
    with sqlite3.connect(tmp_path / "tocmemex.db") as connection:
        connection.execute(
            "UPDATE documents SET status = 'failed', error = 'OCR 任务超时', "
            "stage = '解析失败', progress = 100, parse_plan = 'OCR 结构化'"
        )
    with make_client(tmp_path) as client:
        data = client.get("/api/jobs").json()
    job = data["jobs"][0]
    assert job["id"] == "doc1"
    assert job["docId"] == "doc1"
    assert job["status"] == "failed"
    assert job["error"] == "OCR 任务超时"
    assert job["stage"] == "解析失败"
    assert job["progress"] == 100
    assert job["plan"] == "OCR 结构化"
    # Retry sends the user back to the upload page with these choices.
    assert job["canRetry"] is True
    assert job["imageMode"] == "structured"
    assert job["collectionId"] is None
    assert job["ingestNote"] is None


@pytest.mark.asyncio
async def test_api_page_endpoint(tmp_path):
    await seed_database(tmp_path)
    if not FIXTURE.exists():
        pytest.skip("OCR fixtures are not tracked")
    ocr_dir = tmp_path / "ocr" / "doc1"
    ocr_dir.mkdir(parents=True)
    shutil.copyfile(FIXTURE, ocr_dir / "raw.jsonl")
    with make_client(tmp_path) as client:
        response = client.get("/api/documents/doc1/page/1")
        assert response.status_code == 200
        assert response.json()["page"] == 1


@pytest.mark.asyncio
async def test_compute_doc_stats(tmp_path):
    db_path = await seed_database(tmp_path)
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        doc_row = connection.execute(
            """
            SELECT d.*,
                   (SELECT COUNT(*) FROM toc_nodes t WHERE t.doc_id = d.id) AS node_count,
                   (SELECT COUNT(*) FROM chunks c WHERE c.doc_id = d.id) AS chunk_count
            FROM documents d WHERE d.id = 'doc1'
            """
        ).fetchone()
    stats = compute_doc_stats(db_path, doc_row, tmp_path)
    assert stats["covered"] == 3
    assert stats["contentPages"] == 4
    assert stats["missingPages"] == 1
    assert stats["shortChunks"] == 2  # contents are < 50 chars


# ── endpoint management API ──


def test_api_endpoints_crud_roundtrip_no_key_leak(tmp_path):
    with make_client(tmp_path) as client:
        created = client.post(
            "/api/endpoints",
            json={
                "name": "main",
                "base_url": "http://x/v1",
                "api_key": "sk-secret-4321",
                "model": "demo-model",
            },
        )
        assert created.status_code == 200, created.text
        row = created.json()["endpoint"]
        assert row["name"] == "main"
        assert "sk-secret-4321" not in created.text
        assert row["has_key"] is True
        assert row["key_tail"] == "4321"

        eid = row["id"]
        listed = client.get("/api/endpoints").json()["endpoints"]
        assert len(listed) == 1
        assert "sk-secret-4321" not in client.get("/api/endpoints").text

        updated = client.put(
            f"/api/endpoints/{eid}",
            json={"name": "renamed", "base_url": "http://y/v1", "model": "m2"},
        )
        assert updated.status_code == 200, updated.text
        upd = updated.json()["endpoint"]
        assert upd["name"] == "renamed"
        assert upd["base_url"] == "http://y/v1"
        assert upd["model"] == "m2"
        assert upd["has_key"] is True  # key kept when not supplied
        assert upd["key_tail"] == "4321"

        deleted = client.delete(f"/api/endpoints/{eid}")
        assert deleted.status_code == 200
        assert client.get("/api/endpoints").json()["endpoints"] == []
        assert client.put(
            f"/api/endpoints/{eid}",
            json={"name": "x", "base_url": "http://x/v1", "model": "m"},
        ).status_code == 404


def test_api_endpoints_validation(tmp_path):
    with make_client(tmp_path) as client:
        bad = client.post("/api/endpoints", json={"name": "x"})
        assert bad.status_code == 422


def test_api_endpoints_default_switch(tmp_path):
    with make_client(tmp_path) as client:
        first = client.post(
            "/api/endpoints",
            json={"name": "a", "base_url": "http://a/v1", "api_key": "k1", "model": "m"},
        ).json()["endpoint"]
        second = client.post(
            "/api/endpoints",
            json={"name": "b", "base_url": "http://b/v1", "api_key": "k2", "model": "m"},
        ).json()["endpoint"]

        none_before = client.get("/api/endpoints/default").json()["default"]
        assert none_before is None

        set_default = client.put(f"/api/endpoints/{second['id']}/default")
        assert set_default.status_code == 200, set_default.text
        default = client.get("/api/endpoints/default").json()["default"]
        assert default["id"] == second["id"]

        client.put(f"/api/endpoints/{first['id']}/default")
        rows = client.get("/api/endpoints").json()["endpoints"]
        flags = {r["id"]: r["is_default"] for r in rows}
        assert flags[first["id"]] is True
        assert flags[second["id"]] is False


def test_api_endpoints_test_pings_llm(tmp_path, monkeypatch):
    from app.llm.client import LLMError

    captured = {}

    def fake_complete(prompt, settings=None, **kwargs):
        captured["model"] = settings.tocmemex_llm_model
        captured["base_url"] = settings.tocmemex_llm_base_url
        captured["effort"] = settings.tocmemex_llm_reasoning_effort
        return "ok"

    monkeypatch.setattr("app.web_api.llm_complete", fake_complete)

    with make_client(tmp_path) as client:
        row = client.post(
            "/api/endpoints",
            json={
                "name": "t",
                "base_url": "http://t/v1",
                "api_key": "kk-1111",
                "model": "tm",
                "reasoning_effort": "low",
            },
        ).json()["endpoint"]
        result = client.post(f"/api/endpoints/{row['id']}/test").json()
        assert result["ok"] is True
        assert result["model"] == "tm"
        assert result["base_url"] == "http://t/v1"
        assert result["elapsed_ms"] >= 0
        assert captured["effort"] == "low"

        monkeypatch.setattr(
            "app.web_api.llm_complete",
            lambda *a, **kw: (_ for _ in ()).throw(LLMError("down")),
        )
        failed = client.post(f"/api/endpoints/{row['id']}/test").json()
        assert failed["ok"] is False
        assert "down" in failed["error"]


def test_api_endpoints_test_not_found(tmp_path):
    with make_client(tmp_path) as client:
        assert client.post("/api/endpoints/nope/test").status_code == 404


# ── bench API ──


def _seed_endpoint(tmp_path: Path) -> str:
    with make_client(tmp_path) as client:
        row = client.post(
            "/api/endpoints",
            json={"name": "e", "base_url": "http://e/v1", "api_key": "kk-8888", "model": "m"},
        ).json()["endpoint"]
        client.put(f"/api/endpoints/{row['id']}/default")
        return row["id"]


@pytest.mark.asyncio
async def test_api_bench_submit_runs_in_background(tmp_path, monkeypatch):
    from app.models import TocBenchScenario

    db_path = await seed_database(tmp_path)
    endpoint_id = _seed_endpoint(tmp_path)

    calls = []

    def fake_runner(
        scenario_id,
        doc_ids,
        endpoint_ids,
        settings,
        session_factory=None,
        engine=None,
        ocr_endpoint_ids=None,
    ):
        calls.append(
            (scenario_id, list(doc_ids), list(endpoint_ids), list(ocr_endpoint_ids or []))
        )

    monkeypatch.setattr("app.web_api.run_scenario", fake_runner)

    with make_client(tmp_path) as client:
        response = client.post(
            "/api/bench",
            json={"doc_ids": ["doc1"], "endpoint_ids": [endpoint_id]},
        )
        assert response.status_code == 200, response.text
        scenario_id = response.json()["scenario"]["id"]
        assert response.json()["scenario"]["status"] == "processing"
        assert len(calls) == 1
        assert calls[0][0] == scenario_id
        assert calls[0][1] == ["doc1"]
        assert calls[0][2] == [endpoint_id]
        assert calls[0][3] == ["cached"]

        # list shows the scenario
        listed = client.get("/api/bench").json()["scenarios"]
        assert len(listed) == 1
        assert listed[0]["id"] == scenario_id

        # detail: scenario + empty runs (runner was faked)
        detail = client.get(f"/api/bench/{scenario_id}").json()
        assert detail["scenario"]["id"] == scenario_id
        assert detail["runs"] == []

        # delete
        assert client.delete(f"/api/bench/{scenario_id}").status_code == 200
        assert client.get("/api/bench").json()["scenarios"] == []
        assert client.get(f"/api/bench/{scenario_id}").status_code == 404


@pytest.mark.asyncio
async def test_api_bench_submit_without_default_endpoint_400(tmp_path, monkeypatch):
    await seed_database(tmp_path)
    # endpoint exists but is not the enabled default -> no active endpoint
    with make_client(tmp_path) as client:
        row = client.post(
            "/api/endpoints",
            json={"name": "nd", "base_url": "http://n/v1", "api_key": "kk-0000", "model": "m"},
        ).json()["endpoint"]
        monkeypatch.setattr("app.web_api.run_scenario", lambda *a, **k: None)
        response = client.post(
            "/api/bench", json={"doc_ids": ["doc1"], "endpoint_ids": [row["id"]]}
        )
        assert response.status_code == 400
        # nothing was persisted
        assert client.get("/api/bench").json()["scenarios"] == []


@pytest.mark.asyncio
async def test_api_bench_submit_unknown_doc_404(tmp_path, monkeypatch):
    monkeypatch.setattr("app.web_api.run_scenario", lambda *a, **k: None)
    endpoint_id = _seed_endpoint(tmp_path)
    with make_client(tmp_path) as client:
        response = client.post(
            "/api/bench", json={"doc_ids": ["ghost"], "endpoint_ids": [endpoint_id]}
        )
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_api_bench_detail_with_runs(tmp_path):
    from app.db import make_engine, session_factory
    from app.models import TocBenchRun, TocBenchScenario

    db_path = tmp_path / "tocmemex.db"
    engine = make_engine(db_path)
    await init_db(engine)
    async with session_factory(engine)() as db:
        db.add(TocBenchScenario(id="sc9", doc_ids='["doc1"]', endpoint_ids='[]', status="done"))
        await db.flush()
        db.add(
            TocBenchRun(
                scenario_id="sc9",
                doc_id="doc1",
                side="rule",
                status="ok",
                node_count=3,
                validate_errors=0,
                validate_warnings=1,
                coverage=0.5,
                score_json="[0, -1, 3, 0.5]",
                elapsed_ms=12,
            )
        )
        db.add(
            TocBenchRun(
                scenario_id="sc9",
                doc_id="doc1",
                endpoint_id="ep1",
                side="llm",
                status="error",
                error="boom",
                node_count=0,
                elapsed_ms=88,
                endpoint_name="e",
                base_url="http://e/v1",
                model="m",
            )
        )
        await db.commit()
    await engine.dispose()

    with make_client(tmp_path) as client:
        detail = client.get("/api/bench/sc9").json()
        assert detail["scenario"]["status"] == "done"
        assert len(detail["runs"]) == 2
        by_side = {r["side"]: r for r in detail["runs"]}
        assert by_side["rule"]["score"] == [0, -1, 3, 0.5]
        assert by_side["llm"]["status"] == "error"
        assert by_side["llm"]["error"] == "boom"


# ── magpie gateway probe ──

MAGPIE_PORT = 3425


def _fake_httpx_get(responses):
    """Return a fake httpx.Client.get keyed by URL path.

    ``responses``: {"path": json | Exception}; unmatched paths raise ConnectError.
    """

    def fake_get(self, url, **kwargs):
        for path, value in responses.items():
            if url.rstrip("/").endswith(path):
                if isinstance(value, Exception):
                    raise value
                from types import SimpleNamespace

                return SimpleNamespace(status_code=200, json=lambda v=value: v)
        raise httpx.ConnectError("no such service")

    return fake_get


def test_probe_when_magpie_running(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setattr(
        httpx.Client,
        "get",
        _fake_httpx_get(
            {
                "/api/hello": {"name": "magpie", "version": "1.0"},
                "/v1/models": {
                    "data": [{"id": "provider/gpt", "display_name": "GPT"}]
                },
            }
        ),
    )
    with make_client(tmp_path) as client:
        r = client.get("/api/endpoints/magpie/probe")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is True
        assert body["base_url"] == f"http://127.0.0.1:{MAGPIE_PORT}/v1"
        assert body["models"][0]["id"] == "provider/gpt"
        assert body["models"][0]["display_name"] == "GPT"


def test_probe_when_gateway_down(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setattr(
        httpx.Client,
        "get",
        _fake_httpx_get({"/api/hello": httpx.ConnectError("down")}),
    )
    with make_client(tmp_path) as client:
        r = client.get("/api/endpoints/magpie/probe")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is False
        assert body["reason"]


def test_probe_rejects_non_magpie_service(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setattr(
        httpx.Client,
        "get",
        _fake_httpx_get({"/api/hello": {"name": "other-gateway"}}),
    )
    with make_client(tmp_path) as client:
        r = client.get("/api/endpoints/magpie/probe")
        assert r.status_code == 200
        assert r.json()["available"] is False


def test_probe_empty_models(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setattr(
        httpx.Client,
        "get",
        _fake_httpx_get(
            {"/api/hello": {"name": "magpie"}, "/v1/models": {"data": []}}
        ),
    )
    with make_client(tmp_path) as client:
        r = client.get("/api/endpoints/magpie/probe")
        body = r.json()
        assert body["available"] is True
        assert body["models"] == []


def test_api_endpoints_effort_roundtrip(tmp_path):
    with make_client(tmp_path) as client:
        row = client.post(
            "/api/endpoints",
            json={
                "name": "n",
                "base_url": "http://x/v1",
                "api_key": "kk-1111",
                "model": "m",
                "reasoning_effort": "low",
            },
        ).json()["endpoint"]
        assert row["reasoning_effort"] == "low"

        eid = row["id"]
        upd = client.put(
            f"/api/endpoints/{eid}",
            json={"name": "n", "base_url": "http://x/v1", "model": "m", "reasoning_effort": "high"},
        ).json()["endpoint"]
        assert upd["reasoning_effort"] == "high"

        cleared = client.put(
            f"/api/endpoints/{eid}",
            json={"name": "n", "base_url": "http://x/v1", "model": "m", "reasoning_effort": ""},
        ).json()["endpoint"]
        assert cleared["reasoning_effort"] == ""


def test_probe_returns_reasoning_levels(tmp_path, monkeypatch):
    monkeypatch.setattr(
        httpx.Client,
        "get",
        _fake_httpx_get(
            {
                "/api/hello": {"name": "magpie"},
                "/v1/models": {
                    "data": [
                        {
                            "id": "m1",
                            "supported_reasoning_levels": [
                                {"effort": "low"},
                                {"effort": "high"},
                            ],
                        },
                        {"id": "m2"},
                    ]
                },
            }
        ),
    )
    with make_client(tmp_path) as client:
        body = client.get("/api/endpoints/magpie/probe").json()
        by_id = {m["id"]: m for m in body["models"]}
        assert by_id["m1"]["reasoning_levels"] == ["low", "high"]
        assert by_id["m2"]["reasoning_levels"] == []


# ── OCR provider status API ──


def test_api_ocr_providers_catalog_prefills_defaults(tmp_path, monkeypatch):
    from app.config import Settings as _Settings

    monkeypatch.setattr(
        "app.config.Settings",
        lambda **kwargs: _Settings(
            **{**kwargs, "paddleocr_api_token": "tok-secret"}
        ),
    )
    with make_client(tmp_path) as client:
        payload = client.get("/api/ocr/providers").json()

    assert set(payload["supported"]) == {"paddle", "mistral", "mineru", "azure"}
    by_name = {row["name"]: row for row in payload["providers"]}
    assert by_name["paddle"]["model"] == "PaddleOCR-VL-1.6"
    assert by_name["mistral"]["base_url"].endswith("/v1/ocr")
    assert "tok-secret" not in client.get("/api/ocr/providers").text


def test_api_ocr_endpoints_rejects_bad_base_url(tmp_path):
    """Malformed / credential-bearing base_url is refused at write time."""
    with make_client(tmp_path) as client:
        for bad in (
            "file:///etc/passwd",
            "not-a-url",
            "https://user:pw@api.example.com/v1/ocr",
        ):
            response = client.post(
                "/api/ocr/endpoints",
                json={
                    "name": "bad",
                    "provider": "mistral",
                    "base_url": bad,
                    "api_key": "k",
                    "model": "mistral-ocr-latest",
                },
            )
            assert response.status_code == 422, (bad, response.text)
        assert client.get("/api/ocr/endpoints").json()["endpoints"] == []


def test_api_ocr_endpoints_crud_test_and_default(tmp_path, monkeypatch):
    # A fresh checkout has no .env provider token; make the env fallback
    # deterministic so this test checks resolution, not local secrets.
    monkeypatch.setenv("PADDLEOCR_API_TOKEN", "test-ocr-token")
    with make_client(tmp_path) as client:
        created = client.post(
            "/api/ocr/endpoints",
            json={
                "name": "mistral-main",
                "provider": "mistral",
                "base_url": "http://127.0.0.1:9/v1/ocr",
                "api_key": "sk-ocr-secret-9876",
                "model": "mistral-ocr-latest",
            },
        )
        assert created.status_code == 200, created.text
        row = created.json()["endpoint"]
        assert row["provider"] == "mistral"
        assert "sk-ocr-secret-9876" not in created.text
        assert row["has_key"] is True
        assert row["key_tail"] == "9876"

        listed = client.get("/api/ocr/endpoints").json()
        assert len(listed["endpoints"]) == 1
        # No DB default yet -> the .env path is reported as active.
        assert listed["active"]["source"] in {"env", "db"}
        assert "sk-ocr-secret-9876" not in client.get("/api/ocr/endpoints").text

        eid = row["id"]
        defaulted = client.put(f"/api/ocr/endpoints/{eid}/default").json()
        assert defaulted["default"]["is_default"] is True

        listed = client.get("/api/ocr/endpoints").json()
        assert listed["active"] == {
            "name": "mistral-main",
            "provider": "mistral",
            "source": "db",
        }

        updated = client.put(
            f"/api/ocr/endpoints/{eid}",
            json={
                "name": "mistral-renamed",
                "provider": "mistral",
                "base_url": "http://127.0.0.1:9/v1/ocr",
                "api_key": "",  # keep the stored key
                "model": "mistral-ocr-latest",
            },
        ).json()["endpoint"]
        assert updated["name"] == "mistral-renamed"
        assert updated["has_key"] is True

        # Unreachable host: the probe reports failure instead of raising.
        tested = client.post(f"/api/ocr/endpoints/{eid}/test").json()
        assert tested["ok"] is False
        assert tested["error"]

        assert client.delete(f"/api/ocr/endpoints/{eid}").status_code == 200
        assert client.get("/api/ocr/endpoints").json()["endpoints"] == []

@pytest.mark.asyncio
async def test_api_tasks_reads_activity_layer(tmp_path):
    """Tasks come from the activity layer, not the ingest queue: a fully
    ready library still lists them."""
    db_path = await seed_database(tmp_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO tasks (id, title, goal, status, owner_agent, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            ("t1", "整理第12章要点", "给出页码引用", "open", "agent-a",
             "2026-11-13T00:00:00+00:00", "2026-11-13T00:00:00+00:00"),
        )
        connection.commit()
    with make_client(tmp_path) as client:
        data = client.get("/api/tasks").json()
    assert data["tasks"][0]["id"] == "t1"
    assert data["tasks"][0]["owner"] == "agent-a"
    assert data["counts"]["open"] == 1
    assert data["counts"]["done"] == 0


@pytest.mark.asyncio
async def test_api_document_chunks_returns_anchor_list(tmp_path):
    db_path = await seed_database(tmp_path)
    with make_client(tmp_path) as client:
        chunks = client.get("/api/documents/doc1/chunks").json()["chunks"]
        missing = client.get("/api/documents/nope/chunks")
    assert chunks, "fixture has chunks"
    assert {"id", "index", "nodeId", "path", "page"} <= set(chunks[0])
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_api_documents_expose_created_at(tmp_path):
    await seed_database(tmp_path)
    with make_client(tmp_path) as client:
        doc = client.get("/api/documents").json()["documents"][0]
    assert doc["createdAt"], "overview needs ingest time to label recent docs"


def _seed_ocr_cache(tmp_path: Path, doc_id: str = "doc1") -> Path:
    cache_dir = tmp_path / "ocr" / doc_id
    cache_dir.mkdir(parents=True)
    (cache_dir / "raw.jsonl").write_text('{"logId":"x"}\n', encoding="utf-8")
    return cache_dir


@pytest.mark.asyncio
async def test_export_ocr_document_ships_raw_jsonl(tmp_path):
    await seed_database(tmp_path)
    _seed_ocr_cache(tmp_path)
    with make_client(tmp_path) as client:
        response = client.get("/api/documents/doc1/export")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["x-export-kind"] == "ocr"
    assert "attachment" in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert {"raw.jsonl", "manifest.json"} <= names
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["export_kind"] == "ocr"
    assert any(item["role"] == "ocr-raw" for item in manifest["files"])


@pytest.mark.asyncio
async def test_export_text_layer_document_reparses_original(tmp_path):
    await seed_database(tmp_path)
    source = tmp_path / "notes.md"
    source.write_text("# 一\n正文一\n\n# 二\n正文二\n", encoding="utf-8")
    with sqlite3.connect(tmp_path / "tocmemex.db") as connection:
        connection.execute(
            "UPDATE documents SET file_path = ?, filename = 'notes.md' WHERE id = 'doc1'",
            (str(source),),
        )
        connection.commit()

    with make_client(tmp_path) as client:
        response = client.get("/api/documents/doc1/export")

    assert response.status_code == 200
    assert response.headers["x-export-kind"] == "text-layer"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert {"notes.md", "pages.json", "manifest.json"} <= names
        pages = json.loads(archive.read("pages.json"))
        manifest = json.loads(archive.read("manifest.json"))
    assert pages["pages"], "re-parsed pages are in the archive"
    assert manifest["parse_channel"] == "markdown"
    assert manifest["ocr"]["used"] is False


@pytest.mark.asyncio
async def test_export_without_cache_or_original_is_409(tmp_path):
    await seed_database(tmp_path)  # file_path points at a demo.pdf that never existed
    with make_client(tmp_path) as client:
        response = client.get("/api/documents/doc1/export")
        detail = client.get("/api/documents/doc1").json()
    assert response.status_code == 409
    assert detail["exportKind"] is None


@pytest.mark.asyncio
async def test_document_detail_reports_export_kind(tmp_path):
    await seed_database(tmp_path)
    with make_client(tmp_path) as client:
        assert client.get("/api/documents/doc1").json()["exportKind"] is None
    _seed_ocr_cache(tmp_path)
    with make_client(tmp_path) as client:
        assert client.get("/api/documents/doc1").json()["exportKind"] == "ocr"


@pytest.mark.asyncio
async def test_api_search_writes_usage_log(tmp_path):
    """Web retrieval must feed the usage signal the curator's heat reads.

    Before this, only MCP search wrote usage rows; a library read through
    the Web had ``usage_log = 0`` and every document's heat stayed 0.
    """
    import sqlite3

    await seed_database(tmp_path)
    with make_client(tmp_path) as client:
        response = client.get("/api/search", params={"q": "温度试验"})
        assert response.status_code == 200
        hits = response.json()["results"]
        assert hits

    with sqlite3.connect(tmp_path / "tocmemex.db") as connection:
        rows = connection.execute(
            "SELECT query, tool, doc_id, rank, used FROM usage_log ORDER BY id"
        ).fetchall()
    assert len(rows) == len(hits)
    assert {row[1] for row in rows} == {"web"}
    assert {row[0] for row in rows} == {"温度试验"}
    assert [row[3] for row in rows] == list(range(1, len(hits) + 1))


@pytest.mark.asyncio
async def test_api_upload_rejects_corrupt_content(tmp_path):
    await seed_database(tmp_path)
    with make_client(tmp_path) as client:
        for name, data, media in (
            ("bad.pdf", b"not a pdf", "application/pdf"),
            ("bad.png", b"not an image", "image/png"),
            (
                "bad.docx",
                b"not a zip",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
            ("bad.doc", b"not a compound file", "application/msword"),
        ):
            response = client.post(
                "/api/uploads", files={"file": (name, data, media)}
            )
            assert response.status_code == 400, (name, response.text)
            assert "有效" in response.json()["detail"] or "损坏" in response.json()["detail"]


def test_friendly_ingest_error_hides_tracebacks():
    from app.web_api import _friendly_ingest_error

    pdf = _friendly_ingest_error(
        "Traceback (most recent call last):\n"
        "PyPDF2.errors.PdfReadError: EOF marker not found"
    )
    assert "PDF" in pdf
    assert "Traceback" not in pdf
    generic = _friendly_ingest_error("something exploded in a worker")
    assert "解析失败" in generic
    assert "something exploded" not in generic


async def test_get_resource_json(tmp_path):
    await seed_database(tmp_path)
    client = make_client(tmp_path)
    response = client.get("/api/documents/doc1/resources/tbl_1_0")
    assert response.status_code == 200
    resource = response.json()["resource"]
    assert resource["title"] == "表1 试验条件"
    assert "<table" in resource["content"]
    assert resource["image"] is None

    missing = client.get("/api/documents/doc1/resources/nope")
    assert missing.status_code == 404


async def _seed_console_run(tmp_path, status: str) -> str:
    from app.agent.models import AgentRun

    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    async with session_factory(engine)() as db:
        run = AgentRun(
            question="取消测试",
            status=status,
            endpoint="test",
            model="fake",
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        run_id = run.id
    await engine.dispose()
    return run_id


async def test_cancel_running_run_sets_status(tmp_path):
    run_id = await _seed_console_run(tmp_path, "running")
    client = make_client(tmp_path)
    response = client.post(f"/api/runs/{run_id}/cancel")
    assert response.status_code == 200
    assert response.json()["run"]["status"] == "cancelled"
    assert response.json()["run"]["error"] == "用户取消"


async def test_cancel_terminal_run_returns_409(tmp_path):
    run_id = await _seed_console_run(tmp_path, "done")
    client = make_client(tmp_path)
    response = client.post(f"/api/runs/{run_id}/cancel")
    assert response.status_code == 409


def test_friendly_error_names_vendor_job_failure():
    """A remote OCR job 5xx is not an endpoint-config problem."""
    from app.web_api import _friendly_ingest_error

    message = _friendly_ingest_error(
        "Traceback (most recent call last):\n"
        '  File "paddle.py", line 124\n'
        '    raise PaddleOcrError(f"job failed: {data.get(\'errorMsg\')}")\n'
        "app.ocr.providers.paddle.PaddleOcrError: "
        "job failed (job 123): OCR识别任务失败（状态： 500"
    )
    # The last "job failed" is the exception; the first is source code.
    assert "OCR识别任务失败" in message
    assert "返回 500" in message
    assert "重新上传" in message
    assert "端点配置" not in message
    assert "data.get" not in message


@pytest.mark.asyncio
async def test_api_upload_stores_and_human_edits_ingest_note(tmp_path):
    """入库备注: written at upload, shown on the document, editable by humans."""
    await seed_database(tmp_path)
    with make_client(tmp_path) as client:
        response = client.post(
            "/api/uploads",
            files={"file": ("memo.md", "# 记录\n\n正文".encode(), "text/markdown")},
            data={"ingestNote": "客户邮件附件，页码待核"},
        )
        status = wait_upload(client, response.json()["upload"]["id"])
        assert status["status"] == "ready"
        doc_id = status["docId"]

        documents = client.get("/api/documents").json()["documents"]
        doc = next(item for item in documents if item["id"] == doc_id)
        assert doc["ingestNote"] == "客户邮件附件，页码待核"
        assert doc["ingestNoteId"]

        detail = client.get(f"/api/documents/{doc_id}").json()["document"]
        assert detail["ingestNote"] == "客户邮件附件，页码待核"

        updated = client.put(
            f"/api/documents/{doc_id}/ingest-note", json={"body": "已核对，可用"}
        ).json()
        assert updated["note"]["kind"] == "ingest"
        assert updated["note"]["body"] == "已核对，可用"

        cleared = client.put(
            f"/api/documents/{doc_id}/ingest-note", json={"body": "  "}
        ).json()
        assert cleared["note"] is None
        detail = client.get(f"/api/documents/{doc_id}").json()["document"]
        assert detail["ingestNote"] is None


@pytest.mark.asyncio
async def test_api_note_delete_removes_replies_but_refuses_ingest(tmp_path):
    """The 手记 page lets a human clean up agent notes, not 入库备注."""
    await seed_database(tmp_path)
    with make_client(tmp_path) as client:
        root = client.post(
            "/api/notes",
            json={"author": "claude", "kind": "note", "body": "做完了", "doc_id": "doc1"},
        ).json()
        reply = client.post(
            "/api/notes",
            json={
                "author": "codex",
                "kind": "note",
                "body": "收到",
                "doc_id": "doc1",
                "reply_to": root["id"],
            },
        ).json()

        deleted = client.delete(f"/api/notes/{root['id']}")
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": 2}
        assert client.get("/api/notes", params={"doc_id": "doc1"}).json()["notes"] == []

        ingest = client.put(
            "/api/documents/doc1/ingest-note", json={"body": "人类写的备注"}
        ).json()["note"]
        refused = client.delete(f"/api/notes/{ingest['id']}")
        assert refused.status_code == 400
        assert "入库备注" in refused.json()["detail"]
        # The refused delete left the row alone.
        assert client.get("/api/documents/doc1").json()["document"]["ingestNote"] == "人类写的备注"


@pytest.mark.asyncio
async def test_api_delete_document_purges_rows_files_and_index(tmp_path):
    """Deleting a document reclaims its files and every row keyed by doc_id."""
    from app.index.activity import UsageLog
    from app.index.fts import index_document

    db_path = await seed_database(tmp_path)
    upload_dir = tmp_path / "uploads" / "up-del"
    upload_dir.mkdir(parents=True)
    original = upload_dir / "gone.md"
    original.write_text("# 删除验证\n\n唯一正文", encoding="utf-8")
    ocr_dir = tmp_path / "ocr" / "doc2"
    ocr_dir.mkdir(parents=True)
    (ocr_dir / "pages.json").write_text("[]", encoding="utf-8")
    resource_dir = tmp_path / "resources" / "doc2"
    resource_dir.mkdir(parents=True)

    engine = make_engine(db_path)
    async with session_factory(engine)() as db:
        doc = Document(
            id="doc2",
            namespace_id="shared",
            title="删除我",
            filename="gone.md",
            file_path=str(original),
            content_hash="hash2",
            page_count=1,
            status="ready",
        )
        db.add(doc)
        await db.flush()
        chunk = Chunk(doc_id="doc2", chunk_index=0, content="唯一正文")
        db.add(chunk)
        db.add(AgentNote(author="claude", kind="note", body="旧手记", doc_id="doc2"))
        db.add(UsageLog(query="温度", tool="search", doc_id="doc2"))
        await db.flush()
        await index_document(db, doc, [chunk])
        await db.commit()
    await engine.dispose()

    with make_client(tmp_path) as client:
        response = client.delete("/api/documents/doc2")
        assert response.status_code == 200
        assert response.json()["deleted"] == "doc2"
        assert client.get("/api/documents/doc2").status_code == 404
        assert all(
            item["id"] != "doc2"
            for item in client.get("/api/documents").json()["documents"]
        )
        assert client.delete("/api/documents/doc2").status_code == 404

    assert not original.exists()
    assert not upload_dir.exists()
    assert not ocr_dir.exists()
    assert not resource_dir.exists()

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM documents WHERE id = ?", ("doc2",)
        ).fetchone()[0] == 0
        for table in (
            "toc_nodes",
            "chunks",
            "resources",
            "agent_notes",
            "usage_log",
            "chunk_fts",
            "doc_fts",
        ):
            count = connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE doc_id = ?", ("doc2",)
            ).fetchone()[0]
            assert count == 0, f"{table} still holds rows for the deleted document"
