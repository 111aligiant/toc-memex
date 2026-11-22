"""S4: MCP identity boundary — server-assigned session, not self-reported."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.db import init_db, make_engine, session_factory
from app.models import AgentNote, Document, Namespace


async def _seed(tmp_path, monkeypatch):
    from app import mcp_server
    from app.config import Settings

    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    Session = session_factory(engine)
    async with Session() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(
            Document(
                id="d1", namespace_id="shared", title="T", filename="a.pdf",
                file_path="a.pdf", content_hash="h", status="ready",
            )
        )
        await db.commit()
    settings = Settings(
        tocmemex_db_path=tmp_path / "tocmemex.db",
        tocmemex_data_dir=tmp_path / "data",
    )
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
    return engine


def _ctx(session_token: str):
    """A fake MCP Context whose connection carries a fixed server session id."""
    connection = SimpleNamespace(state={"memex_session_id": session_token})
    return SimpleNamespace(session=SimpleNamespace(_connection=connection))


# ── S4-1: activity_write ignores a client-supplied session_id ────────────


@pytest.mark.asyncio
async def test_activity_write_uses_server_session_not_client_claim(tmp_path, monkeypatch):
    from app import mcp_server
    import app.mcp_server as _m

    engine = await _seed(tmp_path, monkeypatch)
    ctx = _ctx("server-session-A")
    # Client tries to forge a different session id.
    await mcp_server.activity_write(
        kind="note",
        actor_agent="agent-b",
        content="hello",
        session_id="FORGED-client-session",
        ctx=ctx,
    )
    Session = session_factory(engine)
    async with Session() as db:
        from sqlalchemy import select
        from app.index.activity import Activity

        row = (await db.execute(select(Activity))).scalars().first()
        assert row is not None
        assert row.session_id == "server-session-A"
        assert row.session_id != "FORGED-client-session"
    await engine.dispose()


# ── S4-2: delete_note is owned by session, not the self-reported author ──


@pytest.mark.asyncio
async def test_delete_note_requires_matching_session(tmp_path, monkeypatch):
    from app import mcp_server

    engine = await _seed(tmp_path, monkeypatch)
    writer = _ctx("session-writer")
    attacker = _ctx("session-attacker")

    # Writer creates a note; its session is stamped server-side.
    created = await mcp_server.add_note(body="mine", author="alice", ctx=writer)
    note_id = created["id"]
    assert created["session_id"] == "session-writer"

    # An attacker who knows the author name cannot delete it from another
    # connection -- ownership is the session, not the spoofable name.
    from app.mcp_server import ToolError

    with pytest.raises(ToolError):
        await mcp_server.delete_note(note_id, author="alice", ctx=attacker)

    Session = session_factory(engine)
    async with Session() as db:
        assert await db.get(AgentNote, note_id) is not None

    # The owning connection can delete it.
    deleted = await mcp_server.delete_note(note_id, author="alice", ctx=writer)
    assert deleted["deleted"] == 1
    async with Session() as db:
        assert await db.get(AgentNote, note_id) is None
    await engine.dispose()


# ── S4-3: one session cannot inflate lesson support_count / overwrite ────


@pytest.mark.asyncio
async def test_lesson_write_no_self_inflation_within_one_session(tmp_path, monkeypatch):
    from app import mcp_server
    from app.index.activity import Lesson
    from sqlalchemy import select

    engine = await _seed(tmp_path, monkeypatch)
    same = _ctx("session-1")

    await mcp_server.lesson_write(
        trigger="t", issue="i", solution="fix-v1", actor_agent="a", ctx=same
    )
    await mcp_server.lesson_write(
        trigger="t", issue="i", solution="fix-v2-attempt", actor_agent="a", ctx=same
    )

    Session = session_factory(engine)
    async with Session() as db:
        row = (await db.execute(select(Lesson))).scalars().first()
        assert row is not None
        # Same session twice: no rank inflation and no solution overwrite.
        assert row.support_count == 1
        assert row.solution == "fix-v1"

    # A *different* session does bump support and may update the solution.
    other = _ctx("session-2")
    await mcp_server.lesson_write(
        trigger="t", issue="i", solution="fix-by-other", actor_agent="b", ctx=other
    )
    async with Session() as db:
        row = (await db.execute(select(Lesson))).scalars().first()
        assert row.support_count == 2
        assert row.solution == "fix-by-other"
    await engine.dispose()


@pytest.mark.asyncio
async def test_confirm_erratum_rejects_same_session_different_name(tmp_path, monkeypatch):
    """A reviewer sharing the proposer's session cannot confirm, even if it
    declares a different actor_agent name."""
    from app.index import activity as A

    engine = await _seed(tmp_path, monkeypatch)
    del monkeypatch  # no longer needed
    Session = session_factory(engine)
    async with Session() as db:
        await A.task_create(db, "t")
        original = await A.activity_write(
            db, "erratum", "alice", content="this is wrong",
            doc_id="d1", session_id="session-shared",
        )
        review = await A.activity_write(
            db, "review", "bob", content="looks fine",
            reply_to=original.id,
            session_id="session-shared",  # same connection!
        )
        await db.commit()

        with pytest.raises(ValueError):
            await A.confirm_erratum(
                db, review.id, "bob", True, reviewer_session="session-shared"
            )
    await engine.dispose()
