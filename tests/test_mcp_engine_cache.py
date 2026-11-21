"""T9: the MCP server must reuse one engine per database, not build and
dispose an engine (and re-run ensure_schema) on every tool call.

Was: each ``_with_db`` / ``_SessionCtx`` call did make_engine() +
ensure_schema() and dispose() on the way out. That re-created a connection
pool and re-ran the whole idempotent migration sweep per tool call. Now the
engine and its schema are cached process-wide by database path; these tests
pin that contract by counting make_engine / ensure_schema / dispose calls
across repeated tool calls against the same database.
"""

from __future__ import annotations

import pytest

from app import mcp_server
from app.db import make_engine as real_make_engine
from app.db import ensure_schema as real_ensure_schema


@pytest.fixture()
async def _clean_cache():
    await mcp_server.dispose_cached_engines()
    yield
    await mcp_server.dispose_cached_engines()


@pytest.mark.asyncio
async def test_with_db_reuses_one_engine_and_schema(tmp_path, monkeypatch):
    db_path = tmp_path / "cache.db"

    counts = {"make": 0, "ensure": 0, "dispose": 0}
    real_engines = []

    def counting_make_engine(path):
        counts["make"] += 1
        return real_make_engine(path)

    async def counting_ensure_schema(engine):
        counts["ensure"] += 1
        return await real_ensure_schema(engine)

    monkeypatch.setattr(mcp_server, "make_engine", counting_make_engine)
    monkeypatch.setattr(mcp_server, "ensure_schema", counting_ensure_schema)

    class _Settings:
        tocmemex_db_path = db_path

    monkeypatch.setattr(mcp_server, "get_settings", lambda: _Settings())

    await mcp_server.dispose_cached_engines()
    counts.update(make=0, ensure=0)

    async def op(db):
        return "ok"

    # Several calls against the same database must not each build an engine.
    for _ in range(5):
        assert await mcp_server._with_db(op) == "ok"
        ctx = mcp_server._session()
        async with ctx as db:
            assert db is not None

    assert counts["make"] == 1, f"engine built {counts['make']}x, want 1"
    assert counts["ensure"] == 1, f"ensure_schema ran {counts['ensure']}x, want 1"

    # The cached engine is the same object throughout (never disposed and
    # rebuilt between calls): a per-call dispose() would have dropped it from
    # the pool and forced a fresh make_engine on the next call (counts > 1).
    assert len(mcp_server._ENGINE_CACHE) == 1

    await mcp_server.dispose_cached_engines()
    assert mcp_server._ENGINE_CACHE == {}


@pytest.mark.asyncio
async def test_separate_databases_get_separate_engines(tmp_path, monkeypatch):
    counts = {"make": 0}

    def counting_make_engine(path):
        counts["make"] += 1
        return real_make_engine(path)

    monkeypatch.setattr(mcp_server, "make_engine", counting_make_engine)

    current = {"path": tmp_path / "a.db"}

    class _Settings:
        @property
        def tocmemex_db_path(self):
            return current["path"]

    monkeypatch.setattr(mcp_server, "get_settings", lambda: _Settings())
    await mcp_server.dispose_cached_engines()
    counts["make"] = 0

    async def op(db):
        return 1

    await mcp_server._with_db(op)
    await mcp_server._with_db(op)
    assert counts["make"] == 1  # second call reuses

    current["path"] = tmp_path / "b.db"
    await mcp_server._with_db(op)
    assert counts["make"] == 2  # new path, new engine
    await mcp_server.dispose_cached_engines()
