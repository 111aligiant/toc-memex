from __future__ import annotations

import pytest

from app.db import init_db, make_engine, session_factory
from app.index.activity import (
    activity_list,
    activity_out,
    activity_write,
    task_create,
    task_get,
    task_list,
    task_update,
)
from app.models import Document, Namespace


async def _seed(tmp_path):
    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    Session = session_factory(engine)
    async with Session() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(
            Document(
                id="d1",
                namespace_id="shared",
                title="测试标准",
                filename="a.pdf",
                file_path="a.pdf",
                content_hash="h",
                status="ready",
            )
        )
        await db.commit()
    return engine


@pytest.mark.asyncio
async def test_task_lifecycle(tmp_path):
    engine = await _seed(tmp_path)
    Session = session_factory(engine)
    async with Session() as db:
        task = await task_create(db, "整理温度剖面", goal="输出 4 章剖面表", owner_agent="agent-a")
        await db.commit()
        assert task.status == "open"

        task = await task_update(db, task.id, status="done")
        await db.commit()
        assert task.status == "done"

        fetched = await task_get(db, task.id)
        assert fetched is not None
        assert fetched.title == "整理温度剖面"

        tasks = await task_list(db, status="done")
        assert [t.id for t in tasks] == [task.id]
    await engine.dispose()


@pytest.mark.asyncio
async def test_task_update_invalid_status(tmp_path):
    engine = await _seed(tmp_path)
    Session = session_factory(engine)
    async with Session() as db:
        task = await task_create(db, "t")
        await db.commit()
        with pytest.raises(ValueError):
            await task_update(db, task.id, status="cancelled")
    await engine.dispose()


@pytest.mark.asyncio
async def test_activity_append_and_filters(tmp_path):
    engine = await _seed(tmp_path)
    Session = session_factory(engine)
    async with Session() as db:
        task = await task_create(db, "任务 T")
        await db.commit()

        await activity_write(db, "cite", "agent-a", task_id=task.id, doc_id="d1", node_id="d1:4.5")
        await activity_write(db, "note", "agent-a", "第2段表格 OCR 断列", task_id=task.id, doc_id="d1", node_id="d1:4.5")
        await activity_write(db, "erratum", "agent-b", "12.2 的类别表把 S 类写成 C 类", doc_id="d1", node_id="d1:12.2")
        await db.commit()
        rows = await activity_list(db)
        erratum = next(a for a in rows if a.kind == "erratum")

        await activity_write(db, "review", "agent-c", "核实原文，确认是 OCR 误字", reply_to=erratum.id)
        await db.commit()

        by_task = await activity_list(db, task_id=task.id)
        assert {a.kind for a in by_task} == {"cite", "note"}

        by_kind = await activity_list(db, kind="erratum")
        assert len(by_kind) == 1

        replies = await activity_list(db, reply_to=erratum.id)
        assert len(replies) == 1
        assert replies[0].actor_agent == "agent-c"

        by_agent = await activity_list(db, actor_agent="agent-b")
        assert [a.kind for a in by_agent] == ["erratum"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_activity_validation(tmp_path):
    engine = await _seed(tmp_path)
    Session = session_factory(engine)
    async with Session() as db:
        with pytest.raises(ValueError):
            await activity_write(db, "bogus", "agent-a", "x")
        with pytest.raises(ValueError):
            await activity_write(db, "note", "agent-a")  # no content
        with pytest.raises(ValueError):
            await activity_write(db, "erratum", "agent-a", "x")  # no anchor
        with pytest.raises(ValueError):
            await activity_write(db, "review", "agent-a", "x")  # no reply_to
        with pytest.raises(ValueError):
            await activity_write(db, "cite", "agent-a", task_id="nope", doc_id="d1")
    await engine.dispose()


@pytest.mark.asyncio
async def test_read_section_auto_logs(tmp_path):
    engine = await _seed(tmp_path)
    Session = session_factory(engine)
    from sqlalchemy import text

    from app.models import Chunk, TocNode

    async with Session() as db:
        db.add(
            TocNode(
                doc_id="d1",
                node_id="d1:1",
                parent_id=None,
                level=1,
                heading_num="1",
                title="1 范围",
                heading_path="1 范围",
                page_start=1,
                page_end=1,
                chunk_start=0,
                chunk_end=0,
            )
        )
        db.add(
            Chunk(
                doc_id="d1",
                node_id="d1:1",
                chunk_index=0,
                content="本文件规定了范围。",
                heading_path="1 范围",
                page_start=1,
                page_end=1,
            )
        )
        await db.commit()

    from app.index.sections import read_section

    async with Session() as db:
        task = await task_create(db, "验收任务")
        await db.commit()
        await read_section(db, "d1", node_id="d1:1", reader_agent="agent-a", task_id=task.id)
        await db.commit()
        reads = await activity_list(db, kind="read", actor_agent="agent-a")
        assert len(reads) == 1
        assert reads[0].doc_id == "d1"
        assert reads[0].node_id == "d1:1"
        assert reads[0].task_id == task.id

    # reading without agent identity must not crash or log
    async with Session() as db:
        await read_section(db, "d1", node_id="d1:1")
        await db.commit()
        reads = await activity_list(db, kind="read")
        assert len(reads) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_mcp_read_section_commits_read_log(tmp_path, monkeypatch):
    """Regression: the MCP read_section tool must commit the auto-logged
    read; a flush-only session rolls back on close (agent-alpha found this
    in acceptance testing 2026-11-13)."""
    from app import mcp_server
    from app.config import Settings

    engine = await _seed(tmp_path)
    settings = Settings(
        tocmemex_db_path=tmp_path / "tocmemex.db",
        tocmemex_data_dir=tmp_path / "data",
    )
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)

    from sqlalchemy import text

    from app.models import Chunk, TocNode

    Session = session_factory(engine)
    async with Session() as db:
        db.add(
            TocNode(
                doc_id="d1",
                node_id="d1:1",
                parent_id=None,
                level=1,
                heading_num="1",
                title="1 范围",
                heading_path="1 范围",
                page_start=1,
                page_end=1,
                chunk_start=0,
                chunk_end=0,
            )
        )
        db.add(
            Chunk(
                doc_id="d1",
                node_id="d1:1",
                chunk_index=0,
                content="本文件规定了范围。",
                heading_path="1 范围",
                page_start=1,
                page_end=1,
            )
        )
        await db.commit()

    result = await mcp_server.server.call_tool(
        "read_section",
        {"doc_id": "d1", "node_id": "d1:1", "actor_agent": "agent-a", "task_id": ""},
    )
    assert result.is_error is False

    # verify through a *fresh* session — the old one is closed and rolled back
    async with Session() as db:
        rows = (await db.execute(text("select count(*) from activities"))).scalar()
        assert rows == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_mcp_activity_tools_registered():
    from app.mcp_server import server

    tools = {tool.name for tool in server._tool_manager._tools.values()}
    assert {
        "task_create",
        "task_update",
        "task_list",
        "activity_write",
        "activity_list",
    } <= tools


@pytest.mark.asyncio
async def test_mcp_note_session_id_and_author_fallback(tmp_path, monkeypatch):
    """Two identity layers on notes written through MCP:

    - session_id is assigned server-side and is stable within one
      connection, distinct between connections;
    - author falls back to the client's declared name when omitted.

    Neither proves which model wrote the note; the test pins the behaviour,
    not a security guarantee.
    """
    from app import mcp_server
    from app.config import Settings

    engine = await _seed(tmp_path)
    settings = Settings(
        tocmemex_db_path=tmp_path / "tocmemex.db",
        tocmemex_data_dir=tmp_path / "data",
    )
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)

    # No Context is injected when calling through the in-process tool
    # registry, which is exactly the "no client info available" case.
    result = await mcp_server.server.call_tool(
        "add_note", {"body": "note without an author"}
    )
    assert result.is_error is False

    Session = session_factory(engine)
    async with Session() as db:
        from sqlalchemy import select

        from app.models import AgentNote

        row = (await db.execute(select(AgentNote))).scalars().first()
        assert row is not None
        assert row.body == "note without an author"
        assert row.author == "anonymous"
        assert row.session_id is None
    await engine.dispose()


def test_session_id_is_stable_per_connection():
    """The id lives on the connection object: same object -> same id,
    different objects -> different ids."""
    from types import SimpleNamespace

    from app.mcp_server import _session_id

    connection = SimpleNamespace()
    ctx = SimpleNamespace(session=SimpleNamespace(_connection=connection))

    first = _session_id(ctx)
    second = _session_id(ctx)
    other = _session_id(SimpleNamespace(session=SimpleNamespace(_connection=SimpleNamespace())))

    assert first and first == second
    assert other and other != first
    assert _session_id(None) is None


@pytest.mark.asyncio
async def test_ingest_note_is_agent_readable_but_not_writable(tmp_path, monkeypatch):
    """入库备注: humans write it at upload; agents may read, never write.

    The MCP surface must expose it (list_notes / get_document) and must
    refuse both creation and deletion, whatever author an agent claims.
    """
    import json

    from app import mcp_server
    from app.config import Settings
    from app.index.activity import upsert_ingest_note

    engine = await _seed(tmp_path)
    settings = Settings(
        tocmemex_db_path=tmp_path / "tocmemex.db",
        tocmemex_data_dir=tmp_path / "data",
    )
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)

    async with session_factory(engine)() as db:
        row = await upsert_ingest_note(db, "d1", "客户邮件附件，核对页码后再用")
        await db.commit()
        note_id = row.id

    listed = await mcp_server.server.call_tool(
        "list_notes", {"doc_id": "d1", "kind": "ingest"}
    )
    assert listed.is_error is False
    notes = listed.structured_content["result"]
    assert [note["kind"] for note in notes] == ["ingest"]
    assert notes[0]["body"] == "客户邮件附件，核对页码后再用"

    document = await mcp_server.server.call_tool("get_document", {"doc_id": "d1"})
    assert document.is_error is False
    payload = json.loads(document.content[0].text)
    assert payload["ingest_note"]["body"] == "客户邮件附件，核对页码后再用"

    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="入库备注"):
        await mcp_server.server.call_tool(
            "add_note", {"body": "冒充入库备注", "kind": "ingest", "doc_id": "d1"}
        )

    with pytest.raises(ToolError, match="入库备注"):
        await mcp_server.server.call_tool(
            "delete_note", {"note_id": note_id, "author": "human"}
        )

    async with session_factory(engine)() as db:
        from app.index.activity import ingest_note

        assert await ingest_note(db, "d1") is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_ingest_note_overwrite_always_keeps_a_revision(tmp_path):
    """A writer claiming the current author's name must not erase history.

    The author on the MCP path is a self-declared handshake name, so "same
    author, overwrite in place" was an eraser: an agent could pass
    ``--actor human`` and replace the human's 备注 with no revision. Every
    overwrite now snapshots the replaced body, and the session stamp
    describes the writer of the current body rather than a carried-over one.
    """
    from sqlalchemy import select

    from app.index.activity import ingest_note, upsert_ingest_note
    from app.models import NoteRevision

    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        first = await upsert_ingest_note(db, "d1", "人类写的初始备注", author="human")
        await db.commit()
        note_id = first.id

        second = await upsert_ingest_note(
            db, "d1", "冒名覆盖的备注", author="human", session_id="sess-agent"
        )
        await db.commit()
        assert second.id == note_id
        assert second.body == "冒名覆盖的备注"
        assert second.current_rev == 2
        assert second.session_id == "sess-agent"

        revs = (
            (
                await db.execute(
                    select(NoteRevision)
                    .where(NoteRevision.note_id == note_id)
                    .order_by(NoteRevision.rev)
                )
            )
            .scalars()
            .all()
        )
        assert [(rev.rev, rev.author, rev.body) for rev in revs] == [
            (1, "human", "人类写的初始备注"),
            (2, "human", "冒名覆盖的备注"),
        ]

        # A later writer with no server session must not inherit the old one.
        third = await upsert_ingest_note(db, "d1", "网页上编辑的备注", author="human")
        await db.commit()
        assert third.session_id is None
        assert third.current_rev == 3
        assert (await ingest_note(db, "d1")).body == "网页上编辑的备注"
    await engine.dispose()


@pytest.mark.asyncio
async def test_add_note_rejects_dangling_chunk_and_node(tmp_path, monkeypatch):
    """A note on a non-existent anchor is refused, not silently stored.

    The old behaviour wrote the note and returned success; the reader could
    never find it by chunk/node, so the note was lost while the writer
    believed it had been filed.
    """
    import pytest as _pytest
    from mcp.server.mcpserver.exceptions import ToolError

    from app import mcp_server
    from app.config import Settings
    from app.models import TocNode

    engine = await _seed(tmp_path)
    Session = session_factory(engine)
    async with Session() as db:
        db.add(
            TocNode(
                doc_id="d1", node_id="d1:1", parent_id=None, level=1,
                title="第1章", heading_path="第1章", source="bookmark", confidence=1.0,
            )
        )
        await db.commit()
    settings = Settings(
        tocmemex_db_path=tmp_path / "tocmemex.db",
        tocmemex_data_dir=tmp_path / "data",
    )
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)

    with _pytest.raises(ToolError, match="切片不存在"):
        await mcp_server.server.call_tool(
            "add_note", {"body": "x", "doc_id": "d1", "chunk_id": 999999}
        )
    with _pytest.raises(ToolError, match="节点不存在"):
        await mcp_server.server.call_tool(
            "add_note", {"body": "x", "doc_id": "d1", "node_id": "d1:404"}
        )
    await engine.dispose()
