"""纲目派工链路 v1 (docs/m3b-gangmu-dispatch.md).

编修 files a gangmu-review task instead of patching structural TOC errors;
纲目 consumes the task, reviews the document (or the disputed subtree) and
files a pending proposal through the normal op path.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.curation.dispatch import DispatchError, Dispatcher
from app.curation.ops import ProposalContext
from app.db import init_db, make_engine, session_factory
from app.index.activity import AgentNote, Task, task_get
from app.ingest import toc_review
from app.models import Chunk, Document, Namespace, TocNode


async def _seed(tmp_path):
    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(
            Document(
                id="d1",
                namespace_id="shared",
                title="温度试验方法",
                filename="a.pdf",
                file_path="a.pdf",
                content_hash="h",
                page_count=5,
                status="ready",
            )
        )
        await db.flush()
        db.add_all(
            [
                TocNode(
                    doc_id="d1",
                    node_id="d1:h:1",
                    level=1,
                    heading_num="1",
                    title="1 温度试验方法",
                    heading_path="1 温度试验方法",
                    page_start=1,
                    page_end=1,
                ),
                TocNode(
                    doc_id="d1",
                    node_id="d1:h:2",
                    level=1,
                    heading_num="2",
                    title="2 另一节",
                    heading_path="2 另一节",
                    page_start=3,
                    page_end=3,
                ),
            ]
        )
        await db.flush()
        db.add(
            Chunk(
                doc_id="d1",
                node_id="d1:h:1",
                chunk_index=0,
                content="温度试验方法正文。",
                heading_path="1 温度试验方法",
                page_start=1,
                page_end=1,
            )
        )
        await db.commit()
    return engine


@pytest.mark.asyncio
async def test_request_toc_rebuild_files_a_work_order(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        dispatcher = Dispatcher(db, tmp_path, "d1")
        result = await dispatcher.dispatch(
            "request_toc_rebuild",
            {"node_id": "d1:h:1", "reason": "页码整体漂移两页"},
        )
        await db.commit()
        task = await db.get(Task, result["task_id"])
        notes = (await db.execute(select(AgentNote))).scalars().all()
    assert task.kind == "gangmu-review"
    assert task.status == "open"
    goal = json.loads(task.goal)
    assert goal["doc_id"] == "d1"
    assert goal["node_id"] == "d1:h:1"
    assert "漂移" in goal["reason"]
    assert notes and notes[0].task_id == task.id and notes[0].doc_id == "d1"
    await engine.dispose()


@pytest.mark.asyncio
async def test_request_toc_rebuild_rejects_a_foreign_node(tmp_path):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        dispatcher = Dispatcher(db, tmp_path, "d1")
        with pytest.raises(DispatchError):
            await dispatcher.dispatch(
                "request_toc_rebuild", {"node_id": "not-this-doc", "reason": "x"}
            )
    await engine.dispose()


@pytest.mark.asyncio
async def test_consume_review_task_files_a_pending_proposal(tmp_path, monkeypatch):
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        dispatcher = Dispatcher(db, tmp_path, "d1")
        result = await dispatcher.dispatch(
            "request_toc_rebuild",
            {"node_id": "d1:h:1", "reason": "第一节页码漂移"},
        )
        await db.commit()
        task_id = result["task_id"]

        async def fake_review(db_, doc, pages, *, limit=20, window=10):
            return {
                "doc_id": doc.id,
                "checked": 2,
                "ok": 0,
                "catalog_pages": [],
                "proposed": [
                    {
                        "node_id": "d1:h:1",
                        "title": "1 温度试验方法",
                        "declared": [1, 1],
                        "found": 2,
                        "op": {
                            "op": "set_page",
                            "node_id": "d1:h:1",
                            "page_start": 2,
                            "page_end": 2,
                            "evidence": "1 温度试验方法",
                        },
                    },
                    {
                        "node_id": "d1:h:2",
                        "title": "2 另一节",
                        "declared": [3, 3],
                        "found": 4,
                        "op": {
                            "op": "set_page",
                            "node_id": "d1:h:2",
                            "page_start": 4,
                            "page_end": 4,
                            "evidence": "2 另一节",
                        },
                    },
                ],
                "ambiguous": [],
                "unlocatable": [],
                "skipped": 0,
            }

        async def fake_context(db_, doc, data_dir):
            return ProposalContext(
                db=db_,
                doc=doc,
                pages={2: "1 温度试验方法", 4: "2 另一节"},
                data_dir=data_dir,
            )

        monkeypatch.setattr(toc_review, "review_document", fake_review)
        monkeypatch.setattr("app.curation.dispatch.build_context", fake_context)
        monkeypatch.setattr("app.curation.ops.build_context", fake_context)

        outcome = await toc_review.consume_review_task(db, tmp_path, task_id)
        await db.commit()
        task = await task_get(db, task_id)
        notes = (await db.execute(select(AgentNote))).scalars().all()
        latest = notes[-1]

    assert outcome["report"]["filtered_to"] == "d1:h:1"
    assert outcome["report"]["filtered_ops"] == 1
    proposal = outcome["proposal"]
    assert proposal["pending_ops"] == 1
    assert proposal["applied_ops"] == 0
    assert task.status == "done"
    assert latest.author == "gangmu" and latest.task_id == task_id
    await engine.dispose()
