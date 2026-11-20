from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from app.agent.models import AgentRun, AgentStep
from app.agent.runner import execute_run
from app.db import init_db, make_engine, session_factory
from app.llm.client import ChatReply
from app.models import Chunk, Document, Namespace, TocNode


async def seed(tmp_path: Path) -> Path:
    db_path = tmp_path / "tocmemex.db"
    engine = make_engine(db_path)
    await init_db(engine)
    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(
            Document(
                id="doc1",
                namespace_id="shared",
                title="振动试验规范",
                filename="vibration.pdf",
                file_path="vibration.pdf",
                content_hash="hash",
                page_count=8,
                status="ready",
            )
        )
        await db.flush()
        db.add(
            TocNode(
                doc_id="doc1",
                node_id="d:8.3",
                parent_id=None,
                level=1,
                heading_num="8.3",
                title="8.3 振动试验",
                heading_path="8.3 振动试验",
                page_start=5,
                page_end=6,
                chunk_start=0,
                chunk_end=1,
                source="bookmark",
                confidence=1.0,
            )
        )
        db.add_all(
            [
                Chunk(
                    doc_id="doc1",
                    node_id="d:8.3",
                    chunk_index=0,
                    content="8.3 振动试验应采用正弦振动，频率范围 10 Hz 至 2000 Hz。",
                    heading_path="8.3 振动试验",
                    page_start=5,
                    page_end=5,
                    token_count=20,
                ),
                Chunk(
                    doc_id="doc1",
                    node_id="d:8.3",
                    chunk_index=1,
                    content="试验持续时间按表 8-1 执行，每个轴向 15 分钟。",
                    heading_path="8.3 振动试验",
                    page_start=6,
                    page_end=6,
                    token_count=18,
                ),
            ]
        )
        await db.commit()
    await engine.dispose()
    return db_path


async def seed_gjb(tmp_path: Path) -> Path:
    """One section, two chunks, each with its own heading path."""
    db_path = tmp_path / "gjb.db"
    engine = make_engine(db_path)
    await init_db(engine)
    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(
            Document(
                id="doc1",
                namespace_id="shared",
                title="GJB 150.16A 振动试验",
                filename="gjb.pdf",
                file_path="gjb.pdf",
                content_hash="gjb",
                page_count=20,
                status="ready",
            )
        )
        await db.flush()
        db.add(
            TocNode(
                doc_id="doc1",
                node_id="doc1:h:4",
                parent_id=None,
                level=1,
                heading_num="4",
                title="4 剪裁指南",
                heading_path="4 剪裁指南",
                page_start=6,
                page_end=6,
                chunk_start=0,
                chunk_end=1,
                source="bookmark",
                confidence=1.0,
            )
        )
        await db.flush()
        db.add_all(
            [
                Chunk(
                    doc_id="doc1",
                    node_id="doc1:h:4",
                    chunk_index=0,
                    content="4.1 选择试验方法\n\n分析有关技术文件的要求。",
                    heading_path="4 剪裁指南 > 4.1 选择试验方法",
                    page_start=6,
                    page_end=6,
                    token_count=10,
                ),
                Chunk(
                    doc_id="doc1",
                    node_id="doc1:h:4",
                    chunk_index=1,
                    content="4.2 选择振动试验方法\n\n试验方法根据表1选择振动环境类别。",
                    heading_path="4 剪裁指南 > 4.2 选择振动试验方法",
                    page_start=6,
                    page_end=7,
                    token_count=10,
                ),
            ]
        )
        await db.commit()
    await engine.dispose()
    return db_path


def call(name: str, args: dict, call_id: str = "call_1") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }


def reply(
    *tool_calls: dict,
    content: str | None = None,
    finish_reason: str | None = None,
) -> ChatReply:
    return ChatReply(
        content=content,
        tool_calls=list(tool_calls),
        tokens={"prompt": 10, "completion": 5, "total": 15},
        finish_reason=finish_reason,
    )


async def make_run(
    db_path: Path,
    *,
    allow_notes: bool = True,
    max_calls: int = 6,
    max_tokens: int = 0,
) -> tuple:
    engine = make_engine(db_path)
    async with session_factory(engine)() as db:
        run = AgentRun(
            question="8.3 振动试验要求是什么？",
            allow_notes=allow_notes,
            max_calls=max_calls,
            max_tokens=max_tokens,
            endpoint="test",
            model="fake",
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        run_id = run.id
    return engine, run_id


async def load_run(engine, run_id: str) -> tuple[AgentRun, list[AgentStep]]:
    async with session_factory(engine)() as db:
        run = await db.get(AgentRun, run_id)
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
        return run, list(steps)


async def execute_with_fake_llm(
    db_path: Path,
    monkeypatch,
    replies: list[ChatReply],
    *,
    allow_notes: bool = True,
    max_calls: int = 6,
    settings=None,
):
    """Run one console run against a canned reply list.

    Returns a lightweight snapshot (with ``.steps``) so tests do not keep a
    disposed engine alive.
    """
    engine, run_id = await make_run(
        db_path, allow_notes=allow_notes, max_calls=max_calls
    )

    def fake_chat(messages, settings=None, **kwargs):
        return replies.pop(0)

    monkeypatch.setattr("app.agent.runner.llm_chat", fake_chat)
    await execute_run(run_id, engine=engine, settings=settings or object())
    run, steps = await load_run(engine, run_id)
    await engine.dispose()
    return SimpleNamespace(
        id=run.id,
        question=run.question,
        status=run.status,
        error=run.error,
        answer=run.answer,
        citations_json=run.citations_json,
        usage_json=run.usage_json,
        steps=steps,
    )

async def seed_heading(tmp_path: Path) -> Path:
    """A chunk whose whole heading is a long, quotable sentence."""
    db_path = tmp_path / "heading.db"
    engine = make_engine(db_path)
    await init_db(engine)
    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(
            Document(
                id="doc1",
                namespace_id="shared",
                title="试验大纲",
                filename="plan.pdf",
                file_path="plan.pdf",
                content_hash="heading",
                page_count=4,
                status="ready",
            )
        )
        await db.flush()
        db.add(
            TocNode(
                doc_id="doc1",
                node_id="doc1:h:1",
                parent_id=None,
                level=1,
                heading_num="1",
                title="1 试验目的与适用范围及一般要求",
                heading_path="1 试验目的与适用范围及一般要求",
                page_start=0,
                page_end=0,
                chunk_start=0,
                chunk_end=0,
                source="bookmark",
                confidence=1.0,
            )
        )
        await db.flush()
        db.add(
            Chunk(
                doc_id="doc1",
                node_id="doc1:h:1",
                chunk_index=0,
                content=(
                    "1 试验目的与适用范围及一般要求\n\n"
                    "本试验用于验证装备在振动环境下的适应性。"
                ),
                heading_path="1 试验目的与适用范围及一般要求",
                page_start=0,
                page_end=0,
                token_count=20,
            )
        )
        await db.commit()
    await engine.dispose()
    return db_path


async def seed_figure(tmp_path: Path) -> Path:
    """A document whose chunk refers to one figure with a real image file."""
    db_path = tmp_path / "figure.db"
    image_dir = tmp_path / "data" / "resources" / "doc1"
    image_dir.mkdir(parents=True, exist_ok=True)
    image_path = image_dir / "fig_6_0.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    engine = make_engine(db_path)
    await init_db(engine)
    async with session_factory(engine)() as db:
        from app.models import Resource

        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(
            Document(
                id="doc1",
                namespace_id="shared",
                title="振动试验规范",
                filename="vibration.pdf",
                file_path="vibration.pdf",
                content_hash="figure",
                page_count=8,
                status="ready",
            )
        )
        await db.flush()
        db.add(
            TocNode(
                doc_id="doc1",
                node_id="n1",
                parent_id=None,
                level=1,
                heading_num="6",
                title="6 测点布置",
                heading_path="6 测点布置",
                page_start=6,
                page_end=6,
                chunk_start=0,
                chunk_end=0,
                source="bookmark",
                confidence=1.0,
            )
        )
        await db.flush()
        db.add(
            Chunk(
                doc_id="doc1",
                node_id="n1",
                chunk_index=0,
                content="6 测点布置 测点布置见图6。",
                heading_path="6 测点布置",
                page_start=6,
                page_end=6,
                token_count=10,
            )
        )
        db.add(
            Resource(
                doc_id="doc1",
                resource_id="fig_6_0",
                type="figure",
                title="图6 夹具示意",
                image_path=str(image_path),
                page_idx=6,
            )
        )
        await db.commit()
    await engine.dispose()
    return db_path


async def seed_33950(tmp_path: Path) -> Path:
    """The exact 33949/33950 shape from run fda35e29."""
    db_path = tmp_path / "cite33950.db"
    engine = make_engine(db_path)
    await init_db(engine)
    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(
            Document(
                id="doc1",
                namespace_id="shared",
                title="GJB 150.16A 振动试验",
                filename="gjb.pdf",
                file_path="gjb.pdf",
                content_hash="cite33950",
                page_count=20,
                status="ready",
            )
        )
        await db.flush()
        db.add(
            TocNode(
                doc_id="doc1",
                node_id="doc1:h:4",
                parent_id=None,
                level=1,
                heading_num="4",
                title="4 剪裁指南",
                heading_path="4 剪裁指南",
                page_start=6,
                page_end=6,
                chunk_start=0,
                chunk_end=1,
                source="bookmark",
                confidence=1.0,
            )
        )
        await db.flush()
        db.add_all(
            [
                Chunk(
                    id=33949,
                    doc_id="doc1",
                    node_id="doc1:h:4",
                    chunk_index=9,
                    content="4.1 选择试验方法\n\n分析有关技术文件的要求。",
                    heading_path="4 剪裁指南 > 4.1 选择试验方法",
                    page_start=6,
                    page_end=6,
                    token_count=10,
                ),
                Chunk(
                    id=33950,
                    doc_id="doc1",
                    node_id="doc1:h:4",
                    chunk_index=10,
                    content=(
                        "4.2 选择振动试验方法\n\n4.2.1 概述\n\n"
                        "试验方法根据表1选择振动环境类别和试验程序。"
                    ),
                    heading_path="4 剪裁指南 > 4.2 选择振动试验方法",
                    page_start=6,
                    page_end=6,
                    token_count=14,
                ),
            ]
        )
        await db.commit()
    await engine.dispose()
    return db_path
