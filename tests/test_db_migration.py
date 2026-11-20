"""init_db migrates legacy databases, so the curator CLI does not need ensure_schema.

The migration gap: every column patch used to live only in ``ensure_schema``
(web_api / MCP), while the curator and the CLI call ``init_db`` directly — an
old database opened by ``python -m app.curator`` failed with "no such column"
on the first ORM query.
"""
from __future__ import annotations

import sqlite3 as _sqlite3

import pytest

from app.db import init_db, make_engine


LEGACY_AGENT_NOTES = """
CREATE TABLE agent_notes (
    id VARCHAR(36) PRIMARY KEY,
    author VARCHAR(80) NOT NULL,
    kind VARCHAR(20) NOT NULL,
    body TEXT NOT NULL,
    doc_id VARCHAR(36),
    node_id VARCHAR(120),
    chunk_id VARCHAR(36),
    created_at VARCHAR(40) NOT NULL
)
"""


@pytest.mark.asyncio
async def test_init_db_adds_agent_note_columns(tmp_path):
    db_path = tmp_path / "old.db"
    with _sqlite3.connect(db_path) as conn:
        conn.execute(LEGACY_AGENT_NOTES)
        conn.execute(
            "INSERT INTO agent_notes (id, author, kind, body, created_at) "
            "VALUES ('n1','agent','pitfall','beware','2026-11-01')"
        )

    engine = make_engine(db_path)
    await init_db(engine)  # what curator.py calls — no ensure_schema
    await engine.dispose()

    with _sqlite3.connect(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(agent_notes)")}
        assert {"reply_to", "issue_id", "session_id", "task_id"} <= cols
        # the backfilled row keeps its defaults (NULL) but is queryable
        row = conn.execute("SELECT reply_to, task_id FROM agent_notes WHERE id='n1'").fetchone()
        assert row == (None, None)


@pytest.mark.asyncio
async def test_init_db_adds_errata_lessons_and_endpoint_columns(tmp_path):
    db_path = tmp_path / "old.db"
    with _sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE errata (
                id VARCHAR(36) PRIMARY KEY,
                doc_id VARCHAR(36) NOT NULL,
                node_id VARCHAR(120),
                created_at VARCHAR(40) NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE lessons (
                id VARCHAR(36) PRIMARY KEY,
                text TEXT NOT NULL,
                created_at VARCHAR(40) NOT NULL
            )
            """
        )

    engine = make_engine(db_path)
    await init_db(engine)
    await engine.dispose()

    with _sqlite3.connect(db_path) as conn:
        errata_cols = {row[1] for row in conn.execute("PRAGMA table_info(errata)")}
        assert {"scope", "supersedes_doc_id", "supersedes_node_id"} <= errata_cols
        lesson_cols = {row[1] for row in conn.execute("PRAGMA table_info(lessons)")}
        assert {"support_count", "updated_at"} <= lesson_cols

    # idempotent: a second init_db on the same database must not raise
    engine = make_engine(db_path)
    await init_db(engine)
    await engine.dispose()


@pytest.mark.asyncio
async def test_init_db_adds_document_progress_columns(tmp_path):
    """The 解析队列 reads stage/progress/parse_plan from documents."""
    db_path = tmp_path / "old.db"
    with _sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE documents (
                id VARCHAR(36) PRIMARY KEY,
                namespace_id VARCHAR(36) NOT NULL,
                title VARCHAR(500) NOT NULL,
                filename VARCHAR(500) NOT NULL,
                file_path VARCHAR(1000) NOT NULL,
                content_hash VARCHAR(64) NOT NULL,
                doc_type VARCHAR(40),
                status VARCHAR(20) NOT NULL,
                created_at VARCHAR(40) NOT NULL,
                updated_at VARCHAR(40) NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO documents VALUES "
            "('d1','shared','t','f.pdf','f.pdf','h',NULL,'ready','2026-11-01','2026-11-01')"
        )

    engine = make_engine(db_path)
    await init_db(engine)
    await engine.dispose()

    with _sqlite3.connect(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
        assert {"stage", "progress", "parse_plan"} <= cols
