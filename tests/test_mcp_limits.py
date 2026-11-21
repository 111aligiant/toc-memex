"""S7: MCP list/ask tools must clamp their size parameters.

``get_chunks(limit=-1)`` used to reach SQL ``.limit(-1)`` (which SQLite reads
as "no limit"), and ``ask`` forwarded ``top_k`` / ``max_chars`` unclamped, so
a caller could pull the whole document or an unbounded answer. Both now clamp
like ``search`` does; these tests pin the clamp by capturing the forwarded
arguments.
"""

from __future__ import annotations

import pytest

from app import mcp_server


@pytest.mark.asyncio
async def test_get_chunks_clamps_limit_and_offset(monkeypatch):
    captured = {}

    async def fake_curation_call(doc_id, name, args=None):
        captured["name"] = name
        captured["args"] = args
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "_curation_call", fake_curation_call)

    # limit=-1 (unbounded in SQLite) and a huge limit both clamp to a sane max.
    await mcp_server.get_chunks("d1", limit=-1, offset=-5)
    assert captured["name"] == "get_chunks"
    assert captured["args"]["limit"] == 1
    assert captured["args"]["offset"] == 0

    await mcp_server.get_chunks("d1", limit=100000, offset=0)
    assert captured["args"]["limit"] == 500


@pytest.mark.asyncio
async def test_ask_clamps_top_k_and_max_chars(monkeypatch):
    captured = {}

    async def fake_ask_question(db, question, **kwargs):
        captured.update(kwargs)
        return {"hits": [], "citations": [], "answer": ""}

    monkeypatch.setattr(mcp_server, "ask_question", fake_ask_question)

    # top_k=-1 / max_chars=-1 and oversized values both clamp.
    await mcp_server.ask("q", top_k=-1, max_chars=-1)
    assert captured["top_k"] == 1
    assert captured["max_chars"] == 200

    await mcp_server.ask("q", top_k=9999, max_chars=999999)
    assert captured["top_k"] == 12
    assert captured["max_chars"] == 20000
