from __future__ import annotations

import pytest

from app.db import init_db, make_engine, session_factory
from app.index.activity import (
    activity_list,
    activity_write,
    confirm_erratum,
    confirmed_errata_hint,
    errata_for_chunks,
    lesson_search,
    lesson_write,
    score_list,
    score_write,
    usage_log_write,
    ERRATUM_STATUSES,
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
async def test_erratum_lifecycle(tmp_path):
    engine = await _seed(tmp_path)
    Session = session_factory(engine)
    async with Session() as db:
        # erratum write creates the errata row as proposed
        activity = await activity_write(
            db, "erratum", "agent-a",
            "12.2 类别表 S 类写成 C 类",
            doc_id="d1", node_id="d1:12.2",
        )
        await db.commit()

        from app.index.activity import Erratum
        from sqlalchemy import select

        errata = (await db.execute(select(Erratum))).scalars().all()
        assert len(errata) == 1
        erratum = errata[0]
        assert erratum.status == "proposed"
        assert erratum.activity_id == activity.id

        # proposed errata must NOT appear in retrieval hints
        hits = [{"doc_id": "d1", "node_id": "d1:12.2", "chunk_ids": [1]}]
        assert await confirmed_errata_hint(db, hits) == []

        # rejecting review demotes to rejected
        review1 = await activity_write(
            db, "review", "agent-b", "核实后认为是误报", reply_to=activity.id
        )
        result = await confirm_erratum(db, review1.id, "agent-b", False)
        await db.commit()
        assert result.status == "rejected"

        # second erratum + accepting review -> confirmed
        activity2 = await activity_write(
            db, "erratum", "agent-a", "公差 0.2±0.05 被打散",
            doc_id="d1", node_id="d1:12.3",
        )
        await db.commit()
        review2 = await activity_write(
            db, "review", "agent-c", "独立复核确认", reply_to=activity2.id
        )
        result = await confirm_erratum(db, review2.id, "agent-c", True)
        await db.commit()
        assert result.status == "confirmed"
        assert result.confirmed_by == "agent-c"

        # confirmed errata ride along with retrieval
        hints = await confirmed_errata_hint(
            db, [{"doc_id": "d1", "node_id": "d1:12.3", "chunk_ids": [5]}]
        )
        assert len(hints) == 1
        assert hints[0]["status"] == "confirmed"
        assert "0.2±0.05" in hints[0]["body"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_confirm_erratum_backfills_from_log(tmp_path):
    """An erratum activity written before the errata table existed gets
    back-filled when someone reviews it (migration path for old data)."""
    engine = await _seed(tmp_path)
    Session = session_factory(engine)
    async with Session() as db:
        from sqlalchemy import delete

        from app.index.activity import Erratum

        activity = await activity_write(
            db, "erratum", "agent-a", "老数据", doc_id="d1", node_id="d1:1"
        )
        # simulate pre-errata-table row: drop the errata row
        await db.execute(delete(Erratum))
        await db.commit()

        review = await activity_write(
            db, "review", "agent-b", "确认", reply_to=activity.id
        )
        result = await confirm_erratum(db, review.id, "agent-b", True)
        await db.commit()
        assert result is not None
        assert result.status == "confirmed"
        assert result.body == "老数据"
    await engine.dispose()


@pytest.mark.asyncio
async def test_erratum_supersede_and_retract_scopes(tmp_path):
    """Legal supersede and academic retraction are erratum dialects: same
    lifecycle (proposed -> confirmed), extra metadata for the replacement."""
    engine = await _seed(tmp_path)
    Session = session_factory(engine)
    async with Session() as db:
        # legal: a clause repealed by another document
        activity = await activity_write(
            db, "erratum", "agent-a",
            "本条已由新标准第5章代替",
            doc_id="d1", node_id="d1:4.1",
            scope="superseded",
            supersedes_doc_id="d2", supersedes_node_id="d2:5",
        )
        await db.commit()
        review = await activity_write(
            db, "review", "agent-b", "核对修订单确认", reply_to=activity.id
        )
        result = await confirm_erratum(db, review.id, "agent-b", True)
        await db.commit()
        assert result.status == "confirmed"
        assert result.scope == "superseded"
        assert result.supersedes_doc_id == "d2"

        # confirmed supersede rides along with retrieval of the repealed node
        hints = await confirmed_errata_hint(
            db, [{"doc_id": "d1", "node_id": "d1:4.1", "chunk_ids": []}]
        )
        assert len(hints) == 1
        assert hints[0]["scope"] == "superseded"

        # academic retraction
        activity2 = await activity_write(
            db, "erratum", "agent-c",
            "该文于 2025-06 被 IEEE 撤稿，引用需附撤稿声明",
            doc_id="d1", node_id="d1:ref-07",
            scope="retracted",
        )
        await db.commit()
        assert activity2.id > 0

        # invalid scope rejected
        with pytest.raises(ValueError, match="invalid scope"):
            await activity_write(
                db, "erratum", "agent-a", "x", doc_id="d1", scope="bogus"
            )
        # superseded without replacement info rejected (content present,
        # but neither supersedes_doc_id nor a body pointing at one)
        with pytest.raises(ValueError, match="superseded erratum must name"):
            await activity_write(
                db, "erratum", "agent-a", "本条已废止",
                doc_id="d1", scope="superseded",
            )
    await engine.dispose()


@pytest.mark.asyncio
async def test_erratum_decision_is_frozen(tmp_path):
    """Once confirmed/rejected, later reviews cannot flip the verdict."""
    engine = await _seed(tmp_path)
    Session = session_factory(engine)
    async with Session() as db:
        activity = await activity_write(
            db, "erratum", "agent-a", "X", doc_id="d1", node_id="d1:1"
        )
        review = await activity_write(
            db, "review", "agent-b", "确认", reply_to=activity.id
        )
        result = await confirm_erratum(db, review.id, "agent-b", True)
        await db.commit()
        assert result.status == "confirmed"

        # another agent's later review must not flip it
        review2 = await activity_write(
            db, "review", "agent-c", "我不同意", reply_to=activity.id
        )
        with pytest.raises(ValueError, match="already confirmed"):
            await confirm_erratum(db, review2.id, "agent-c", False)
        await db.rollback()

        # verdict survived
        from app.index.activity import Erratum
        from sqlalchemy import select

        erratum = (await db.execute(select(Erratum))).scalars().first()
        assert erratum.status == "confirmed"
    await engine.dispose()


@pytest.mark.asyncio
async def test_erratum_self_confirmation_forbidden(tmp_path):
    """Security: the proposer cannot confirm their own erratum — one
    compromised/buggy agent must not inject corrections into retrieval."""
    engine = await _seed(tmp_path)
    Session = session_factory(engine)
    async with Session() as db:
        activity = await activity_write(
            db, "erratum", "agent-a", "自导自演的勘误", doc_id="d1", node_id="d1:1"
        )
        review = await activity_write(
            db, "review", "agent-a", "我自己确认", reply_to=activity.id
        )
        await db.commit()
        with pytest.raises(ValueError, match="self-confirmation"):
            await confirm_erratum(db, review.id, "agent-a", True)
        await db.commit()
        # still proposed — nothing changed
        from app.index.activity import Erratum
        from sqlalchemy import select

        erratum = (await db.execute(select(Erratum))).scalars().first()
        assert erratum.status == "proposed"
        await db.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_erratum_subtree_match(tmp_path):
    """An erratum anchored to a chapter node covers all subsection hits:
    node_ids are flat sequence numbers, so ancestry comes from toc_nodes
    parent chains, not string prefixes."""
    engine = await _seed(tmp_path)
    Session = session_factory(engine)
    from sqlalchemy import text

    from app.models import TocNode

    async with Session() as db:
        db.add_all([
            TocNode(doc_id="d1", node_id="d1:10", parent_id=None, level=1,
                    heading_num="第10章", title="第10章 试验", heading_path="第10章 试验"),
            TocNode(doc_id="d1", node_id="d1:11", parent_id="d1:10", level=2,
                    heading_num="10.1", title="10.1 程序", heading_path="第10章 试验 > 10.1 程序"),
        ])
        await db.commit()
        activity = await activity_write(
            db, "erratum", "agent-a", "全章公差有误", doc_id="d1", node_id="d1:10"
        )
        review = await activity_write(
            db, "review", "agent-b", "确认", reply_to=activity.id
        )
        await confirm_erratum(db, review.id, "agent-b", True)
        await db.commit()

        # hit is the child subsection; chunk id belongs to it
        hints = await confirmed_errata_hint(
            db, [{"doc_id": "d1", "node_id": "d1:11", "chunk_ids": [42]}]
        )
        assert len(hints) == 1
        assert hints[0]["body"] == "全章公差有误"

        # unrelated node gets nothing
        hints = await confirmed_errata_hint(
            db, [{"doc_id": "d1", "node_id": "d1:99", "chunk_ids": [43]}]
        )
        assert hints == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_usage_log_and_mark_used(tmp_path):
    engine = await _seed(tmp_path)
    Session = session_factory(engine)
    from app.index.activity import UsageLog
    from sqlalchemy import select, update

    async with Session() as db:
        hits = [
            {"doc_id": "d1", "node_id": "d1:1", "chunk_id": 11},
            {"doc_id": "d1", "node_id": "d1:2", "chunk_id": 12},
        ]
        n = await usage_log_write(db, "温度试验", "search", hits, actor_agent="agent-a")
        await db.commit()
        assert n == 2

        rows = (await db.execute(select(UsageLog).order_by(UsageLog.id))).scalars().all()
        assert [r.rank for r in rows] == [1, 2]
        assert all(r.used == 0 for r in rows)

        await db.execute(
            update(UsageLog).where(UsageLog.chunk_id == 11).values(used=1)
        )
        await db.commit()

        used = (await db.execute(
            select(UsageLog).where(UsageLog.used == 1)
        )).scalars().all()
        assert len(used) == 1 and used[0].chunk_id == 11
    await engine.dispose()


@pytest.mark.asyncio
async def test_usage_log_bad_task_never_breaks(tmp_path):
    engine = await _seed(tmp_path)
    Session = session_factory(engine)
    async with Session() as db:
        # nonexistent task_id is dropped silently, rows still logged
        n = await usage_log_write(
            db, "q", "search",
            [{"doc_id": "d1", "chunk_id": 1}],
            task_id="no-such-task",
        )
        await db.commit()
        assert n == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_lesson_write_and_search(tmp_path):
    engine = await _seed(tmp_path)
    Session = session_factory(engine)
    async with Session() as db:
        await lesson_write(
            db,
            trigger="检索中文标准里的试验条件表",
            issue="BM25 被'试验/类别'泛词带偏，命中的是参考文献",
            solution="加章节号路由或用标题通道，同义词桥接中英文",
            author_agent="claude-code",
            keywords="BM25 泛词 同义词 检索",
        )
        await lesson_write(
            db,
            trigger="OCR 数字公差",
            issue="±公差被 OCR 打散成乱码",
            solution="对照括号内英制值和上下文交叉印证",
            author_agent="agent-alpha",
            keywords="OCR 公差 乱码",
        )
        await db.commit()

        hits = await lesson_search(db, "检索 泛词 BM25")
        assert len(hits) == 1
        assert "BM25" in hits[0].issue

        hits = await lesson_search(db, "OCR 公差")
        assert len(hits) == 1
        assert "OCR" in hits[0].issue

        assert await lesson_search(db, "完全不相关的查询词") == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_lesson_validation(tmp_path):
    engine = await _seed(tmp_path)
    Session = session_factory(engine)
    async with Session() as db:
        with pytest.raises(ValueError):
            await lesson_write(db, "", "issue", "solution", "a")
        with pytest.raises(ValueError):
            await lesson_write(db, "t", "issue", "", "a")
        with pytest.raises(ValueError):
            await lesson_write(db, "t", "issue", "s", "", task_id="nope")
    await engine.dispose()


@pytest.mark.asyncio
async def test_score_write_and_list(tmp_path):
    engine = await _seed(tmp_path)
    Session = session_factory(engine)
    async with Session() as db:
        from app.index.activity import task_create

        task = await task_create(db, "整理温度剖面")
        await db.commit()

        await score_write(db, task.id, "hit_rate", "0.8", "agent-b", comment="3/4 命中")
        await score_write(db, task.id, "rework", "yes", "agent-a", comment="返工一次")
        await db.commit()

        rows = await score_list(db, task_id=task.id)
        assert len(rows) == 2
        assert {r.dimension for r in rows} == {"hit_rate", "rework"}

        rows = await score_list(db, dimension="hit_rate")
        assert len(rows) == 1

        with pytest.raises(ValueError):
            await score_write(db, "nope", "hit_rate", "1", "agent-a")
        with pytest.raises(ValueError):
            await score_write(db, task.id, "", "1", "agent-a")
    await engine.dispose()


@pytest.mark.asyncio
async def test_mcp_phase2_tools_registered():
    from app.mcp_server import server

    tools = {tool.name for tool in server._tool_manager._tools.values()}
    assert {
        "mark_used",
        "lesson_write",
        "lesson_search",
        "score_write",
        "score_list",
    } <= tools
    assert ERRATUM_STATUSES == ("proposed", "confirmed", "rejected")


@pytest.mark.asyncio
async def test_lesson_search_matches_a_natural_chinese_sentence(tmp_path):
    """A sentence, not a keyword list, must reach the lesson.

    The old splitter only knew whitespace/punctuation, so
    "砂尘浓度单位被拆开怎么办" became one 11-character token and matched
    nothing while "砂尘" matched. jieba segmentation recovers the terms.
    """
    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        await lesson_write(
            db,
            trigger="砂尘试验的浓度单位被拆开",
            issue="OCR 把 mg/m3 拆成两段",
            solution="按原文合并后再引用",
            author_agent="agent-a",
            keywords="砂尘,单位,OCR",
        )
        await lesson_write(
            db,
            trigger="湿热试验",
            issue="恒定湿热与交变湿热混淆",
            solution="看标题 Cab/Db",
            author_agent="agent-a",
            keywords="湿热",
        )
        await db.commit()

        rows = await lesson_search(db, "砂尘浓度单位被拆开怎么办")
        assert [row.trigger for row in rows] == ["砂尘试验的浓度单位被拆开"]
        # The plain keyword query still works.
        assert [row.trigger for row in await lesson_search(db, "砂尘")] == [
            "砂尘试验的浓度单位被拆开"
        ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_errata_by_hit_keeps_ancestor_anchored_correction(tmp_path):
    """A chapter-level erratum must reach a hit on one of its subsections.

    ``confirmed_errata_hint`` already used the ancestor chain; MCP's
    per-hit filter then kept only exact ``node_id`` equality, so a
    confirmed correction on 第12章 vanished when searching 12.3.2.
    """
    from sqlalchemy import select

    from app.index.activity import errata_by_hit
    from app.models import Chunk, TocNode

    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        db.add_all(
            [
                TocNode(
                    doc_id="d1", node_id="d1:12", level=1, title="第12章",
                    heading_path="第12章", page_start=10, page_end=20,
                ),
                TocNode(
                    doc_id="d1", node_id="d1:12.3.2", parent_id="d1:12",
                    level=3, title="12.3.2", heading_path="第12章 › 12.3.2",
                    page_start=15, page_end=15,
                ),
            ]
        )
        await db.flush()
        chunk = Chunk(
            doc_id="d1", node_id="d1:12.3.2", chunk_index=0,
            content="砂尘 浓度 单位", heading_path="第12章 › 12.3.2",
            page_start=15, page_end=15,
        )
        db.add(chunk)
        await db.flush()

        erratum = await activity_write(
            db, "erratum", "agent-a", "本章浓度单位应为 mg/m3",
            doc_id="d1", node_id="d1:12",
        )
        review = await activity_write(
            db, "review", "agent-b", "核对原文确认", reply_to=erratum.id
        )
        await confirm_erratum(db, review.id, "agent-b", True)
        await db.commit()

        hits = [{"doc_id": "d1", "node_id": "d1:12.3.2", "chunk_ids": [chunk.id]}]
        by_hit = await errata_by_hit(db, hits)
        assert [e["node_id"] for e in by_hit[0]] == ["d1:12"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_task_timeline_merges_handoff_notes(tmp_path):
    """MCP's read path must see notes written through add_note."""
    from app.index.activity import task_create, task_timeline
    from app.models import AgentNote

    engine = await _seed(tmp_path)
    async with session_factory(engine)() as db:
        task = await task_create(db, "交接任务")
        await db.flush()
        await activity_write(
            db, "cite", "agent-a", "引用 12.3.2", task_id=task.id, doc_id="d1"
        )
        db.add(
            AgentNote(
                author="agent-a", kind="handoff", body="12.3.2 已核，剩附录",
                doc_id="d1", task_id=task.id,
            )
        )
        await db.commit()

        payload = await task_timeline(db, task.id)
        assert payload is not None
        assert payload["counts"] == {"activities": 1, "notes": 1, "total": 2}
        assert {item["source"] for item in payload["timeline"]} == {"activity", "note"}
        handoff = next(item for item in payload["timeline"] if item["source"] == "note")
        assert handoff["kind"] == "handoff"
        assert "附录" in handoff["content"]
    await engine.dispose()


def test_task_timeline_and_notes_are_exposed_over_mcp():
    from app.mcp_server import server

    tools = {tool.name for tool in server._tool_manager._tools.values()}
    assert {"task_timeline", "list_notes", "search", "activity_list"} <= tools
