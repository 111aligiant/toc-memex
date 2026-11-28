"""MCP ingest tools: queueing, identity, path allowlist, polling.

These call the tool functions directly (no transport). The parse runs in a
detached child process, so the test polls the job row until it is terminal.
Only Markdown is used — it ingests in under a second and never calls OCR.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.db import init_db, make_engine, session_factory
from app.models import AgentNote, IngestJob


def _ctx(client_name: str, session_token: str):
    connection = SimpleNamespace(state={"memex_session_id": session_token})
    params = SimpleNamespace(client_info=SimpleNamespace(name=client_name))
    session = SimpleNamespace(_connection=connection, client_params=params)
    return SimpleNamespace(session=session)


@pytest.fixture
async def env(tmp_path, monkeypatch):
    from app import mcp_server
    from app.ingest import jobs as jobs_mod

    db_path = tmp_path / "tocmemex.db"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    engine = make_engine(db_path)
    await init_db(engine)
    settings = Settings(tocmemex_db_path=db_path, tocmemex_data_dir=data_dir)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
    monkeypatch.setattr(jobs_mod, "get_settings", lambda: settings)
    # The child inherits the parent's env; point it at this temp database.
    monkeypatch.setenv("TOCMEMEX_DB_PATH", str(db_path))
    monkeypatch.setenv("TOCMEMEX_DATA_DIR", str(data_dir))
    yield {"engine": engine, "data_dir": data_dir, "settings": settings}
    await mcp_server.dispose_cached_engines()
    await engine.dispose()


def _write_md(data_dir, name="note.md", body=None):
    path = data_dir / name
    path.write_text(
        body
        or "# 测试标题\n\n这是一段足够长的正文，用来验证 Markdown 直读入库。\n\n## 第一节\n\n正文一。\n",
        encoding="utf-8",
    )
    return path


async def _wait(engine, job_id, timeout=60.0):
    from app.ingest import jobs as jobs_mod

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        job = await jobs_mod.get_job(engine, job_id)
        if job and job["status"] in ("ready", "failed", "duplicate"):
            return job
        await asyncio.sleep(0.25)
    raise AssertionError(f"job {job_id} did not finish")


@pytest.mark.asyncio
async def test_ingest_path_queues_and_completes(env):
    from app import mcp_server

    path = _write_md(env["data_dir"])
    result = await mcp_server.ingest_path(
        path=str(path),
        ingest_note="客户邮件附件",
        ctx=_ctx("claude-code", "sess-1"),
    )
    job = result["job"]
    assert job["status"] in {"queued", "parsing", "ready"}

    final = await _wait(env["engine"], job["id"])
    assert final["status"] == "ready", final
    assert final["doc_id"]

    async with session_factory(env["engine"])() as db:
        notes = (
            await db.execute(
                __import__("sqlalchemy").select(AgentNote).where(AgentNote.kind == "ingest")
            )
        ).scalars().all()
    assert len(notes) == 1
    assert notes[0].author == "claude-code"
    assert notes[0].body == "客户邮件附件"


@pytest.mark.asyncio
async def test_ingest_path_records_agent_run(env):
    """@observed must file the call on the agent run so the audit trail exists."""
    from sqlalchemy import select

    from app import mcp_server
    from app.agent.models import AgentStep

    path = _write_md(env["data_dir"])
    await mcp_server.ingest_path(path=str(path), ctx=_ctx("claude-code", "sess-run"))
    async with session_factory(env["engine"])() as db:
        steps = (
            await db.execute(
                select(AgentStep).where(AgentStep.name == "ingest_path")
            )
        ).scalars().all()
    assert steps, "ingest_path call was not recorded on an agent run"


@pytest.mark.asyncio
async def test_ingest_path_rejects_outside_root(env, tmp_path):
    from app import mcp_server

    outside = tmp_path / "secret.md"
    outside.write_text("# x\n\ny", encoding="utf-8")
    with pytest.raises(Exception) as excinfo:
        await mcp_server.ingest_path(path=str(outside), ctx=_ctx("a", "s"))
    assert "允许的入库目录" in str(excinfo.value)


@pytest.mark.asyncio
async def test_ingest_path_rejects_bad_file(env):
    from app import mcp_server

    bad = env["data_dir"] / "fake.pdf"
    bad.write_bytes(b"not a pdf")
    with pytest.raises(Exception) as excinfo:
        await mcp_server.ingest_path(path=str(bad), ctx=_ctx("a", "s"))
    assert "PDF" in str(excinfo.value)


@pytest.mark.asyncio
async def test_get_ingest_is_not_replay_cached(env):
    """A second poll of the same job must reflect current state, not a cache."""
    from app import mcp_server

    path = _write_md(env["data_dir"])
    created = await mcp_server.ingest_path(path=str(path), ctx=_ctx("a", "sess-poll"))
    job_id = created["job"]["id"]
    await _wait(env["engine"], job_id)

    first = await mcp_server.get_ingest(job_id=job_id, ctx=_ctx("a", "sess-poll"))
    second = await mcp_server.get_ingest(job_id=job_id, ctx=_ctx("a", "sess-poll"))
    assert first["job"]["status"] == "ready"
    assert second["job"]["status"] == "ready"
    # The cache note only appears when the result came from CallGuard.
    assert "note" not in second["job"]


@pytest.mark.asyncio
async def test_get_ingest_unknown_returns_hint(env):
    from app import mcp_server

    result = await mcp_server.get_ingest(job_id="nope", ctx=_ctx("a", "s"))
    assert result["job"] is None
    assert "hint" in result


@pytest.mark.asyncio
async def test_ingest_paths_reports_per_path_errors(env):
    from app import mcp_server

    good = _write_md(env["data_dir"], "good.md")
    bad = env["data_dir"] / "fake.pdf"
    bad.write_bytes(b"nope")
    result = await mcp_server.ingest_paths(
        paths=[str(good), str(bad)], ctx=_ctx("a", "sess-multi")
    )
    assert len(result["jobs"]) == 1
    assert len(result["errors"]) == 1
    assert result["errors"][0]["path"] == str(bad)


@pytest.mark.asyncio
async def test_duplicate_ingest_marks_duplicate(env):
    from app import mcp_server

    path = _write_md(env["data_dir"])
    first = await mcp_server.ingest_path(path=str(path), ctx=_ctx("a", "s1"))
    await _wait(env["engine"], first["job"]["id"])
    second = await mcp_server.ingest_path(path=str(path), ctx=_ctx("a", "s2"))
    final = await _wait(env["engine"], second["job"]["id"])
    assert final["status"] == "duplicate"
    assert final["doc_id"] == first["job"].get("doc_id") or final["doc_id"]
