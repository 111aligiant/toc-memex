from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import urllib.request
from pathlib import Path

import pytest

from app.db import init_db, make_engine, session_factory
from app.inspector import (
    document_payload,
    list_documents,
    make_server,
    page_payload,
)
from app.models import Chunk, Document, Namespace, TocNode

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
                    content="内容一",
                    heading_path="1 范围",
                    page_start=0,
                    page_end=1,
                    token_count=3,
                ),
                Chunk(
                    doc_id="doc1",
                    node_id="d:1.1",
                    chunk_index=1,
                    content="内容二",
                    heading_path="1 范围 > 1.1 目的",
                    page_start=1,
                    page_end=2,
                    token_count=3,
                ),
            ]
        )
        await db.commit()
    await engine.dispose()
    return db_path


@pytest.mark.asyncio
async def test_inspector_payload(tmp_path):
    db_path = await seed_database(tmp_path)
    docs = list_documents(db_path)
    assert len(docs) == 1
    assert docs[0]["id"] == "doc1"
    assert docs[0]["node_count"] == 2
    assert docs[0]["chunk_count"] == 2

    payload = document_payload(db_path, "doc1", tmp_path)
    assert payload is not None
    assert payload["document"]["title"] == "Demo document"
    assert payload["stats"]["nodes"] == 2
    assert payload["stats"]["chunks"] == 2
    assert payload["stats"]["roots"] == 1
    assert payload["stats"]["covered_pages"] == 3
    assert payload["stats"]["missing_pages"] == 1
    by_id = {node["node_id"]: node for node in payload["nodes"]}
    assert by_id["d:1"]["direct_chunks"] == 1
    assert by_id["d:1"]["subtree_chunks"] == 2


@pytest.mark.asyncio
async def test_inspector_front_matter_stats(tmp_path):
    db_path = await seed_database(tmp_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE chunks SET page_start = 2, page_end = 2")
        connection.execute("UPDATE toc_nodes SET page_start = 2, page_end = 2")

    payload = document_payload(db_path, "doc1", tmp_path)
    assert payload is not None
    assert payload["stats"]["non_content_pages"] == 2
    assert payload["stats"]["content_pages"] == 2
    assert payload["stats"]["covered_pages"] == 1
    assert payload["stats"]["missing_pages"] == 1
    assert payload["coverage"][0]["non_content"] is True
    assert payload["coverage"][1]["non_content"] is True
    assert payload["coverage"][2]["non_content"] is False


@pytest.mark.skipif(
    not FIXTURE.exists(),
    reason="OCR fixtures are not tracked; see tests/fixtures/README.md",
)
@pytest.mark.asyncio
async def test_inspector_page_payload(tmp_path):
    await seed_database(tmp_path)
    ocr_dir = tmp_path / "ocr" / "doc1"
    ocr_dir.mkdir(parents=True)
    shutil.copyfile(FIXTURE, ocr_dir / "raw.jsonl")

    payload = page_payload(tmp_path / "tocmemex.db", "doc1", 1, tmp_path)
    assert payload is not None
    assert payload["page"] == 1
    assert payload["blocks"]
    assert payload["text"]


@pytest.mark.asyncio
async def test_inspector_http_endpoints(tmp_path):
    db_path = await seed_database(tmp_path)
    server = make_server(db_path, "127.0.0.1", 0, tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/api/documents") as response:
            assert response.status == 200
            data = json.loads(response.read().decode("utf-8"))
            assert data["documents"][0]["id"] == "doc1"

        with urllib.request.urlopen(f"{base}/api/documents/doc1") as response:
            assert response.status == 200
            data = json.loads(response.read().decode("utf-8"))
            assert len(data["nodes"]) == 2

        with urllib.request.urlopen(f"{base}/") as response:
            assert response.status == 200
            html = response.read().decode("utf-8")
            assert "Toc Memex Inspector" in html
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)