from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import init_db, make_engine, session_factory
from app.index.activity import Activity, Task
from app.models import AgentNote, Document, Namespace
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
                id="d1", namespace_id="shared", title="Demo", filename="a.pdf",
                file_path="a.pdf", content_hash="h", page_count=3, status="ready",
            )
        )
        db.add(
            Task(id="t1", title="DO-160G 第12章", goal="补页", status="doing",
                 owner_agent="agent-a")
        )
        await db.flush()
        db.add_all(
            [
                Activity(kind="read", task_id="t1", doc_id="d1",
                         actor_agent="agent-a", created_at="2026-11-13T02:00:00+00:00"),
                Activity(kind="cite", task_id="t1", doc_id="d1",
                         actor_agent="agent-a", content="12.2 设备要求 p81",
                         created_at="2026-11-13T02:01:00+00:00"),
                Activity(kind="erratum", task_id="t1", doc_id="d1",
                         actor_agent="agent-a", content="砂浓度 OCR 串行",
                         created_at="2026-11-13T02:02:00+00:00"),
                Activity(kind="review", task_id="t1", doc_id="d1",
                         actor_agent="agent-b", content="确认",
                         created_at="2026-11-13T02:03:00+00:00"),
            ]
        )
        db.add(
            AgentNote(
                author="agent-b", kind="handoff", body="任务完成",
                doc_id="d1", task_id="t1",
                created_at="2026-11-13T02:04:00+00:00",
            )
        )
        await db.commit()
    return db_path, engine


@pytest.mark.asyncio
async def test_task_detail_merges_activities_and_notes(tmp_path):
    db_path, engine = await _seed(tmp_path)
    await engine.dispose()

    app = create_app(db_path, tmp_path)
    with TestClient(app) as client:
        data = client.get("/api/tasks/t1").json()

    assert data["task"]["id"] == "t1"
    assert data["counts"] == {"activities": 4, "notes": 1, "runs": 0, "total": 5}
    kinds = [item["kind"] for item in data["timeline"]]
    assert kinds == ["read", "cite", "erratum", "review", "handoff"]
    # The note is merged in at its own timestamp, not appended out of order.
    assert data["timeline"][-1]["source"] == "note"
    assert data["timeline"][-1]["actor"] == "agent-b"


@pytest.mark.asyncio
async def test_task_detail_404_for_unknown_task(tmp_path):
    db_path, engine = await _seed(tmp_path)
    await engine.dispose()
    app = create_app(db_path, tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/tasks/nope").status_code == 404


@pytest.mark.asyncio
async def test_note_can_be_attached_to_a_task(tmp_path):
    db_path, engine = await _seed(tmp_path)
    await engine.dispose()
    app = create_app(db_path, tmp_path)
    with TestClient(app) as client:
        created = client.post(
            "/api/notes",
            json={"author": "agent-c", "kind": "note", "body": "接着做",
                  "doc_id": "d1", "task_id": "t1"},
        )
        assert created.status_code == 200, created.text
        assert created.json()["taskId"] == "t1"

        detail = client.get("/api/tasks/t1").json()
        assert detail["counts"]["notes"] == 2


@pytest.mark.asyncio
async def test_note_with_unknown_task_is_rejected(tmp_path):
    db_path, engine = await _seed(tmp_path)
    await engine.dispose()
    app = create_app(db_path, tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/notes",
            json={"author": "agent-c", "kind": "note", "body": "x", "task_id": "nope"},
        )
        assert response.status_code == 404
        assert "task not found" in response.json()["detail"]


@pytest.mark.asyncio
async def test_notes_without_task_id_stay_unattached(tmp_path):
    """Existing notes predate task_id; they must not appear on any task."""
    db_path, engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        db.add(
            AgentNote(author="agent-x", kind="note", body="公共区笔记", doc_id="d1")
        )
        await db.commit()
        notes = (await db.execute(select(AgentNote))).scalars().all()
        assert sum(1 for note in notes if note.task_id is None) == 1
    await engine.dispose()

    app = create_app(db_path, tmp_path)
    with TestClient(app) as client:
        detail = client.get("/api/tasks/t1").json()
    assert detail["counts"]["notes"] == 1


@pytest.mark.asyncio
async def test_notes_filter_by_task_id(tmp_path):
    """The MCP/Web note readers need the same task filter as activities."""
    db_path, engine = await _seed(tmp_path)
    await engine.dispose()
    app = create_app(db_path, tmp_path)
    with TestClient(app) as client:
        notes = client.get("/api/notes", params={"task_id": "t1"}).json()["notes"]
        assert [note["body"] for note in notes] == ["任务完成"]
        assert notes[0]["taskId"] == "t1"
        empty = client.get("/api/notes", params={"task_id": "nope"}).json()["notes"]
        assert empty == []


@pytest.mark.asyncio
async def test_reply_inherits_the_parents_task(tmp_path):
    """A reply without task_id still lands on the same work unit."""
    db_path, engine = await _seed(tmp_path)
    await engine.dispose()
    app = create_app(db_path, tmp_path)
    with TestClient(app) as client:
        parent = client.post(
            "/api/notes",
            json={"author": "agent-c", "kind": "note", "body": "先看 12.3.2",
                  "task_id": "t1"},
        ).json()
        reply = client.post(
            "/api/notes",
            json={"author": "agent-d", "kind": "note", "body": "已看",
                  "reply_to": parent["id"]},
        )
        assert reply.status_code == 200, reply.text
        assert reply.json()["taskId"] == "t1"
        detail = client.get("/api/tasks/t1").json()
        assert detail["counts"]["notes"] == 3
