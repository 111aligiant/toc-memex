from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import init_db, make_engine
from app.web_api import create_app

from test_web_api import seed_database


@pytest.mark.asyncio
async def test_note_roundtrip(tmp_path):
    db_path = tmp_path / "tocmemex.db"
    engine = make_engine(db_path)
    await init_db(engine)
    await engine.dispose()

    app = create_app(db_path, tmp_path)
    with TestClient(app) as client:
        # create (common-area note, no doc anchor)
        created = client.post(
            "/api/notes",
            json={
                "author": "agent-a",
                "kind": "pitfall",
                "body": "附录页码缺失，跳过 pages ? 节点再切片",
            },
        )
        assert created.status_code == 200
        note = created.json()
        assert note["author"] == "agent-a"
        assert note["kind"] == "pitfall"
        assert note["docId"] is None

        # list all
        listed = client.get("/api/notes").json()["notes"]
        assert len(listed) == 1
        assert listed[0]["body"].startswith("附录页码缺失")

        # filter by author
        by_author = client.get("/api/notes", params={"author": "agent-b"}).json()["notes"]
        assert by_author == []

        # filter by kind
        by_kind = client.get("/api/notes", params={"kind": "pitfall"}).json()["notes"]
        assert len(by_kind) == 1

        # doc filter for nonexistent doc
        missing = client.get("/api/notes", params={"doc_id": "nope"}).json()["notes"]
        assert missing == []


@pytest.mark.asyncio
async def test_note_reply_thread(tmp_path):
    db_path = tmp_path / "tocmemex.db"
    engine = make_engine(db_path)
    await init_db(engine)
    await engine.dispose()

    app = create_app(db_path, tmp_path)
    with TestClient(app) as client:
        parent = client.post(
            "/api/notes",
            json={"author": "agent-a", "kind": "pitfall", "body": "附录没有页码，先跳过"},
        ).json()
        reply = client.post(
            "/api/notes",
            json={
                "author": "agent-b",
                "kind": "note",
                "body": "@agent-a 收到，已按 pages ? 过滤",
                "reply_to": parent["id"],
            },
        ).json()
        assert reply["replyTo"] == parent["id"]
        listed = client.get("/api/notes").json()["notes"]
        assert {n["id"] for n in listed} == {parent["id"], reply["id"]}


@pytest.mark.asyncio
async def test_note_with_document_anchor(tmp_path):
    await seed_database(tmp_path)
    app = create_app(tmp_path / "tocmemex.db", tmp_path)
    with TestClient(app) as client:
        created = client.post(
            "/api/notes",
            json={
                "author": "agent-a",
                "kind": "handoff",
                "body": "已复核 doc1 全部切片，2 处需调整",
                "doc_id": "doc1",
                "node_id": "d:1",
            },
        )
        assert created.status_code == 200
        anchored = client.get("/api/notes", params={"doc_id": "doc1"}).json()["notes"]
        assert len(anchored) == 1
        assert anchored[0]["nodeId"] == "d:1"
        # unanchored notes don't appear under doc filter
        common = client.get("/api/notes").json()["notes"]
        assert len(common) == 1


@pytest.mark.asyncio
async def test_note_rejects_unknown_document(tmp_path):
    db_path = tmp_path / "tocmemex.db"
    engine = make_engine(db_path)
    await init_db(engine)
    await engine.dispose()

    app = create_app(db_path, tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/notes",
            json={"author": "agent-a", "body": "hello", "doc_id": "ghost"},
        )
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_reply_to_column_patched_on_existing_db(tmp_path):
    """A database created before reply_to existed gets the column on startup."""
    db_path = tmp_path / "tocmemex.db"
    engine = make_engine(db_path)
    await init_db(engine)
    await engine.dispose()
    # drop the column by rebuilding the table the old way
    import sqlite3

    connection = sqlite3.connect(db_path)
    connection.execute("DROP TABLE agent_notes")
    connection.execute(
        """
        CREATE TABLE agent_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author VARCHAR(80) NOT NULL,
            kind VARCHAR(20) NOT NULL,
            body TEXT NOT NULL,
            doc_id VARCHAR(36),
            node_id VARCHAR(120),
            chunk_id INTEGER,
            created_at VARCHAR(40) NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO agent_notes (author, kind, body, created_at) VALUES ('old', 'note', 'before migration', 't')"
    )
    connection.commit()
    connection.close()

    app = create_app(db_path, tmp_path)
    with TestClient(app) as client:
        notes = client.get("/api/notes").json()["notes"]
        assert len(notes) == 1 and notes[0]["replyTo"] is None
        created = client.post(
            "/api/notes",
            json={"author": "a", "body": "after migration", "reply_to": 1},
        ).json()
        assert created["replyTo"] == 1


@pytest.mark.asyncio
async def test_note_rejects_bad_kind_and_empty_body(tmp_path):
    db_path = tmp_path / "tocmemex.db"
    engine = make_engine(db_path)
    await init_db(engine)
    await engine.dispose()

    app = create_app(db_path, tmp_path)
    with TestClient(app) as client:
        bad_kind = client.post(
            "/api/notes", json={"author": "a", "kind": "gossip", "body": "x"}
        )
        assert bad_kind.status_code == 422
        empty = client.post("/api/notes", json={"author": "a", "body": ""})
        assert empty.status_code == 422


@pytest.mark.asyncio
async def test_note_issue_anchor_roundtrip(tmp_path):
    """A note posted from a quality issue keeps that issue id, and the
    overview/quality payloads count it as traced."""
    db_path = await seed_database(tmp_path)
    app = create_app(db_path, tmp_path)
    with TestClient(app) as client:
        groups = client.get("/api/quality").json()["groups"]
        assert groups, "fixture should produce at least one issue"
        issue = groups[0]
        assert issue["id"] and issue["noteCount"] == 0

        created = client.post(
            "/api/notes",
            json={
                "author": "agent-a",
                "kind": "note",
                "body": "已核对，引用时跳过缺页",
                "doc_id": issue["docId"],
                "issue_id": issue["id"],
            },
        ).json()
        assert created["issueId"] == issue["id"]

        # filtering by issue returns just that note
        filtered = client.get(f"/api/notes?issue_id={issue['id']}").json()["notes"]
        assert [n["id"] for n in filtered] == [created["id"]]

        # both payloads now report the issue as traced
        again = client.get("/api/quality").json()["groups"]
        assert next(g for g in again if g["id"] == issue["id"])["noteCount"] == 1
        overview = client.get("/api/overview").json()
        assert overview["stats"]["tracedIssues"] == 1


@pytest.mark.asyncio
async def test_web_note_stores_client_session_id(tmp_path):
    """The web UI supplies its own session id so notes from one sitting
    group together; it is a grouping hint, not a verified identity."""
    db_path = await seed_database(tmp_path)
    app = create_app(db_path, tmp_path)
    with TestClient(app) as client:
        created = client.post(
            "/api/notes",
            json={
                "author": "claude-code",
                "body": "from the browser",
                "session_id": "abc123",
            },
        ).json()
    assert created["sessionId"] == "abc123"
