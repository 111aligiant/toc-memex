from __future__ import annotations

import json
from types import SimpleNamespace

from agent_helpers import call, reply, seed_figure
from sqlalchemy import select

from app.agent.models import AgentRun, AgentStep
from app.agent.runner import execute_run
from app.agent.tools import ToolContext, dispatch
from app.db import make_engine, session_factory
from app.llm.client import ChatReply


def _settings(tmp_path):
    return SimpleNamespace(resources_dir=tmp_path / "data" / "resources")


async def test_read_resource_image_attaches_memory_image(tmp_path):
    db_path = await seed_figure(tmp_path)
    engine = make_engine(db_path)
    async with session_factory(engine)() as db:
        ctx = ToolContext(
            db=db,
            run_id="test",
            allow_notes=False,
            resources_root=tmp_path / "data" / "resources",
        )
        result = await dispatch(
            ctx,
            "read_resource",
            {"doc_id": "doc1", "resource_id": "fig_6_0", "include_image": True},
        )
    await engine.dispose()
    assert result["image_meta"]["mime"] == "image/png"
    assert result["image_meta"]["bytes"] > 0
    assert result["_image"]["data_url"].startswith("data:image/png;base64,")


async def test_read_resource_missing_image_returns_meta_only(tmp_path):
    db_path = await seed_figure(tmp_path)
    engine = make_engine(db_path)
    async with session_factory(engine)() as db:
        ctx = ToolContext(
            db=db,
            run_id="test",
            allow_notes=False,
            resources_root=tmp_path / "not-the-resources-root",
        )
        result = await dispatch(
            ctx,
            "read_resource",
            {"doc_id": "doc1", "resource_id": "fig_6_0", "include_image": True},
        )
    await engine.dispose()
    assert "_image" not in result
    assert result["image_include_error"]


async def test_read_resource_skips_image_when_vision_blocked(tmp_path):
    db_path = await seed_figure(tmp_path)
    engine = make_engine(db_path)
    async with session_factory(engine)() as db:
        ctx = ToolContext(
            db=db,
            run_id="test",
            allow_notes=False,
            resources_root=tmp_path / "data" / "resources",
            vision_blocked=True,
        )
        result = await dispatch(
            ctx,
            "read_resource",
            {"doc_id": "doc1", "resource_id": "fig_6_0", "include_image": True},
        )
    await engine.dispose()
    assert "_image" not in result
    assert "不接受图片输入" in result["image_note"]


async def test_runner_does_not_persist_base64_and_appends_image_message(
    tmp_path, monkeypatch
):
    db_path = await seed_figure(tmp_path)
    engine, run_id = await _make_run(db_path)
    captured = []

    replies = [
        reply(
            call(
                "read_resource",
                {"doc_id": "doc1", "resource_id": "fig_6_0", "include_image": True},
                call_id="call_1",
            )
        ),
        reply(
            call(
                "final_answer",
                {
                    "answer": "档案中没有足够证据。",
                    "sources": [],
                    "insufficient": True,
                },
                call_id="call_2",
            )
        ),
    ]

    def fake_chat(messages, settings=None, **kwargs):
        captured.append(list(messages))
        return replies.pop(0)

    monkeypatch.setattr("app.agent.runner.llm_chat", fake_chat)
    await execute_run(run_id, engine=engine, settings=_settings(tmp_path))

    async with session_factory(engine)() as db:
        steps = (
            (
                await db.execute(
                    select(AgentStep)
                    .where(AgentStep.run_id == run_id)
                    .order_by(AgentStep.seq)
                )
            )
            .scalars()
            .all()
        )
    await engine.dispose()

    read_step = next(
        step
        for step in steps
        if step.name == "read_resource" and step.type == "tool_result"
    )
    assert "base64" not in read_step.result_json
    assert "_image" not in json.loads(read_step.result_json)
    assert any(m.images for m in captured[-1])


async def _make_run(db_path):
    engine = make_engine(db_path)
    async with session_factory(engine)() as db:
        run = AgentRun(
            question="测点布置在哪里？",
            allow_notes=False,
            max_calls=6,
            endpoint="test",
            model="fake",
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        run_id = run.id
    return engine, run_id


async def test_runner_marks_vision_blocked_after_drop(tmp_path, monkeypatch):
    db_path = await seed_figure(tmp_path)
    engine, run_id = await _make_run(db_path)
    captured = []

    def fake_chat(messages, settings=None, **kwargs):
        captured.append(list(messages))
        if len(captured) == 1:
            return reply(
                call(
                    "read_resource",
                    {
                        "doc_id": "doc1",
                        "resource_id": "fig_6_0",
                        "include_image": True,
                    },
                    call_id="call_1",
                )
            )
        return ChatReply(
            content=None,
            tool_calls=[
                call(
                    "final_answer",
                    {
                        "answer": "档案中没有足够证据。",
                        "sources": [],
                        "insufficient": True,
                    },
                    call_id="call_2",
                )
            ],
            tokens={"prompt": 10, "completion": 5, "total": 15},
            images_dropped=True,
        )

    monkeypatch.setattr("app.agent.runner.llm_chat", fake_chat)
    await execute_run(run_id, engine=engine, settings=_settings(tmp_path))

    async with session_factory(engine)() as db:
        steps = (
            (
                await db.execute(
                    select(AgentStep)
                    .where(AgentStep.run_id == run_id)
                    .order_by(AgentStep.seq)
                )
            )
            .scalars()
            .all()
        )
    await engine.dispose()
    notes = [step for step in steps if step.type == "note"]
    assert any("端点拒绝了图片输入" in (step.content or "") for step in notes)

async def test_runner_strips_dropped_images_from_future_turns(tmp_path, monkeypatch):
    db_path = await seed_figure(tmp_path)
    engine, run_id = await _make_run(db_path)
    captured = []

    def fake_chat(messages, settings=None, **kwargs):
        captured.append(list(messages))
        if len(captured) == 1:
            return reply(
                call(
                    "read_resource",
                    {
                        "doc_id": "doc1",
                        "resource_id": "fig_6_0",
                        "include_image": True,
                    },
                    call_id="call_1",
                )
            )
        if len(captured) == 2:
            return ChatReply(
                content=None,
                tool_calls=[call("list_notes", {}, call_id="call_2")],
                tokens={"prompt": 10, "completion": 5, "total": 15},
                images_dropped=True,
            )
        return reply(
            call(
                "final_answer",
                {
                    "answer": "档案中没有足够证据。",
                    "sources": [],
                    "insufficient": True,
                },
                call_id="call_3",
            )
        )

    monkeypatch.setattr("app.agent.runner.llm_chat", fake_chat)
    await execute_run(run_id, engine=engine, settings=_settings(tmp_path))
    await engine.dispose()

    assert not any(message.images for message in captured[-1])
    assert any(
        "图片输入被端点拒绝" in (message.content or "")
        for message in captured[-1]
    )
