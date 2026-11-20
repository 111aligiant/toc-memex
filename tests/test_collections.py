from __future__ import annotations

import json
import sqlite3

import pytest
from sqlalchemy import text

from app.db import ensure_schema, init_db, make_engine, session_factory
from app.index.collections import (
    CollectionNotFound,
    UNASSIGNED,
    assign_documents,
    collections_by_doc,
    create_collection,
    delete_collection,
    doc_ids_in_scope,
    list_collections,
    resolve_scope,
    unassigned_doc_count,
    update_collection,
)
from app.models import Chunk, Collection, Document, Namespace, TocNode


@pytest.mark.asyncio
async def test_legacy_documents_table_gets_collection_column(tmp_path):
    db_path = tmp_path / "tocmemex.db"
    # 模拟分区功能之前建的库：documents 没有 collection_id
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE namespaces (id VARCHAR(64) PRIMARY KEY, kind VARCHAR(20) NOT NULL,
            description TEXT, created_at VARCHAR(40) NOT NULL);
        CREATE TABLE documents (
            id VARCHAR(36) PRIMARY KEY, namespace_id VARCHAR(64) NOT NULL,
            title VARCHAR(500) NOT NULL, filename VARCHAR(500) NOT NULL,
            file_path VARCHAR(1000) NOT NULL, content_hash VARCHAR(64) NOT NULL,
            version INTEGER NOT NULL, doc_type VARCHAR(40), doc_date VARCHAR(20),
            source VARCHAR(40), summary TEXT, keywords TEXT,
            page_count INTEGER, char_count INTEGER,
            status VARCHAR(20) NOT NULL, error TEXT,
            created_at VARCHAR(40) NOT NULL, updated_at VARCHAR(40) NOT NULL);
        """
    )
    conn.commit()
    conn.close()

    engine = make_engine(db_path)
    await ensure_schema(engine)
    async with engine.begin() as connection:
        cols = {
            row[1]
            for row in (
                await connection.execute(text("PRAGMA table_info(documents)"))
            ).fetchall()
        }
        indexes = {
            row[1]
            for row in (
                await connection.execute(text("PRAGMA index_list(documents)"))
            ).fetchall()
        }
    await engine.dispose()

    assert "collection_id" in cols
    assert "ix_documents_collection_id" in indexes


@pytest.mark.asyncio
async def test_new_database_has_collection_table_and_column(tmp_path):
    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(Collection(id="c1", name="电子标准", description="国标行标"))
        await db.flush()
        db.add(
            Document(
                id="d1",
                namespace_id="shared",
                title="t",
                filename="t.pdf",
                file_path="t.pdf",
                content_hash="h",
                status="ready",
                collection_id="c1",
            )
        )
        await db.commit()
        doc = await db.get(Document, "d1")
        assert doc.collection_id == "c1"
    await engine.dispose()


async def _seed_two_collections(tmp_path):
    """d1,d2 在「电子标准」；d3 在「求职」；d4 未分区。"""
    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    Session = session_factory(engine)
    async with Session() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        c1 = await create_collection(db, "电子标准", "国标行标")
        c2 = await create_collection(db, "求职", "JD 与个人经历")
        for doc_id, collection_id in (
            ("d1", c1.id), ("d2", c1.id), ("d3", c2.id), ("d4", None)
        ):
            db.add(Document(
                id=doc_id, namespace_id="shared", title=f"doc {doc_id}",
                filename=f"{doc_id}.pdf", file_path=f"{doc_id}.pdf",
                content_hash=doc_id, status="ready", collection_id=collection_id,
            ))
        await db.commit()
    return engine, Session


@pytest.mark.asyncio
async def test_resolve_scope_by_id_and_name(tmp_path):
    engine, Session = await _seed_two_collections(tmp_path)
    async with Session() as db:
        by_name = await resolve_scope(db, "电子标准")
        by_id = await resolve_scope(db, by_name.collection.id)
        assert by_name.collection.id == by_id.collection.id
        assert by_name.active is True
        assert by_name.unassigned is False
        assert (await resolve_scope(db, "")).active is False
        assert (await resolve_scope(db, UNASSIGNED)).unassigned is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_resolve_scope_unknown_lists_valid(tmp_path):
    engine, Session = await _seed_two_collections(tmp_path)
    async with Session() as db:
        with pytest.raises(CollectionNotFound) as excinfo:
            await resolve_scope(db, "电子类标准")
        message = str(excinfo.value)
        assert "电子类标准" in message
        assert "电子标准 (2)" in message
        assert "unassigned" in message
    await engine.dispose()


@pytest.mark.asyncio
async def test_doc_ids_in_scope(tmp_path):
    engine, Session = await _seed_two_collections(tmp_path)
    async with Session() as db:
        assert await doc_ids_in_scope(db, await resolve_scope(db, "")) is None
        unassigned = await doc_ids_in_scope(db, await resolve_scope(db, UNASSIGNED))
        assert unassigned == ["d4"]
        electronics = await doc_ids_in_scope(db, await resolve_scope(db, "电子标准"))
        assert sorted(electronics) == ["d1", "d2"]
        empty = await create_collection(db, "空分区")
        await db.commit()
        assert await doc_ids_in_scope(db, await resolve_scope(db, empty.id)) == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_collections_by_doc_maps_null_to_none(tmp_path):
    engine, Session = await _seed_two_collections(tmp_path)
    async with Session() as db:
        mapping = await collections_by_doc(db, ["d1", "d3", "d4", "nope"])
        assert mapping["d1"]["name"] == "电子标准"
        assert mapping["d3"]["name"] == "求职"
        assert mapping["d4"] is None
        assert mapping["nope"] is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_list_collections_and_unassigned_count(tmp_path):
    engine, Session = await _seed_two_collections(tmp_path)
    async with Session() as db:
        rows = await list_collections(db)
        # 同 sort_order 时按 name 排；SQLite BINARY collation 即码点序，
        # 「求」(U+6C42) 在「电」(U+7535) 之前。要人工顺序请用 sort_order。
        assert [row["name"] for row in rows] == ["求职", "电子标准"]
        assert rows[1]["doc_count"] == 2
        assert rows[0]["description"] == "JD 与个人经历"
        assert await unassigned_doc_count(db) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_rename_keeps_id_and_rejects_duplicate(tmp_path):
    engine, Session = await _seed_two_collections(tmp_path)
    async with Session() as db:
        original = await resolve_scope(db, "求职")
        renamed = await update_collection(db, original.collection.id, name="求职资料")
        await db.commit()
        assert renamed.id == original.collection.id
        assert (await resolve_scope(db, renamed.id)).collection.name == "求职资料"
        with pytest.raises(CollectionNotFound):
            await resolve_scope(db, "求职")
        with pytest.raises(ValueError):
            await update_collection(db, original.collection.id, name="电子标准")
    await engine.dispose()


@pytest.mark.asyncio
async def test_assign_documents_reports_missing_and_keeps_updated_at(tmp_path):
    engine, Session = await _seed_two_collections(tmp_path)
    async with Session() as db:
        before = (await db.get(Document, "d1")).updated_at
        updated, missing = await assign_documents(db, ["d1", "nope"], None)
        await db.commit()
        assert (updated, missing) == (1, ["nope"])
        assert (await db.get(Document, "d1")).updated_at == before
        # d1 与 d4 现在都未分区
        assert await unassigned_doc_count(db) == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_delete_collection_unfiles_documents(tmp_path):
    engine, Session = await _seed_two_collections(tmp_path)
    async with Session() as db:
        target = (await resolve_scope(db, "电子标准")).collection
        unassigned_count = await delete_collection(db, target.id)
        await db.commit()
        assert unassigned_count == 2
        assert (await db.get(Document, "d1")).collection_id is None
        assert [row["name"] for row in await list_collections(db)] == ["求职"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_reserved_name_unassigned_is_rejected(tmp_path):
    engine, Session = await _seed_two_collections(tmp_path)
    async with Session() as db:
        with pytest.raises(ValueError):
            await create_collection(db, "UNASSIGNED")
    await engine.dispose()


@pytest.mark.asyncio
async def test_list_documents_filters_by_collection(tmp_path):
    engine, Session = await _seed_two_collections(tmp_path)
    from app.index.documents import list_documents

    async with Session() as db:
        all_docs = await list_documents(db)
        assert {doc["doc_id"] for doc in all_docs} == {"d1", "d2", "d3", "d4"}
        assert all("collection" in doc for doc in all_docs)

        electronics = await list_documents(db, collection="电子标准")
        assert {doc["doc_id"] for doc in electronics} == {"d1", "d2"}
        assert electronics[0]["collection"]["name"] == "电子标准"

        unfiled = await list_documents(db, collection="unassigned")
        assert [doc["doc_id"] for doc in unfiled] == ["d4"]
        assert unfiled[0]["collection"] is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_get_document_and_toc_carry_collection(tmp_path):
    engine, Session = await _seed_two_collections(tmp_path)
    from app.index.documents import get_document, get_toc

    async with Session() as db:
        detail = await get_document(db, "d1")
        assert detail["collection"]["name"] == "电子标准"
        toc = await get_toc(db, "d4", depth=1)
        assert toc["collection"] is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_overview_reports_collection_distribution(tmp_path):
    engine, Session = await _seed_two_collections(tmp_path)
    from app.index.documents import memex_overview

    async with Session() as db:
        overview = await memex_overview(db)
        by_name = {row["name"]: row for row in overview["collections"]}
        assert set(by_name) == {"电子标准", "求职"}
        assert by_name["电子标准"]["doc_count"] == 2
        assert overview["unassigned_doc_count"] == 1
    await engine.dispose()


async def _seed_search_fixture(tmp_path):
    """电子标准区：GBT 38924.2（含「振动试验」章节）；求职区：一份 JD（也提到「振动」）。"""
    from app.index.fts import index_document

    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    Session = session_factory(engine)
    async with Session() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        std = await create_collection(db, "电子标准")
        job = await create_collection(db, "求职")
        specs = [
            ("std", std.id, "GBT 38924.2-2020", "5 振动试验要求", "振动试验按本标准第 5 章执行"),
            ("job", job.id, "某公司结构工程师 JD", "岗位要求", "熟悉振动试验与可靠性测试"),
        ]
        for doc_id, collection_id, title, heading, body in specs:
            doc = Document(
                id=doc_id, namespace_id="shared", title=title,
                filename=f"{doc_id}.pdf", file_path=f"{doc_id}.pdf",
                content_hash=doc_id, status="ready", collection_id=collection_id,
            )
            db.add(doc)
            await db.flush()
            db.add(TocNode(
                doc_id=doc_id, node_id=f"{doc_id}:1", parent_id=None, level=1,
                heading_num="5", title=heading, heading_path=heading,
                source="toc_page", confidence=0.9,
            ))
            chunk = Chunk(
                doc_id=doc_id, node_id=f"{doc_id}:1", chunk_index=0,
                content=body, heading_path=heading,
            )
            db.add(chunk)
            await db.flush()
            await index_document(db, doc, [chunk])
        await db.commit()
    return engine, Session


@pytest.mark.asyncio
async def test_search_scoped_to_collection_excludes_others(tmp_path):
    from app.index.fts import search_chunks

    engine, Session = await _seed_search_fixture(tmp_path)
    async with Session() as db:
        std = (await resolve_scope(db, "电子标准")).collection
        scoped = await search_chunks(
            db, "振动试验", top_k=5,
            scope_doc_ids=await doc_ids_in_scope(db, await resolve_scope(db, "电子标准")),
        )
        assert scoped
        assert {hit["doc_id"] for hit in scoped} == {"std"}
        assert scoped[0]["collection"]["name"] == "电子标准"
        assert std.id == scoped[0]["collection"]["id"]

        unscoped = await search_chunks(db, "振动试验", top_k=5)
        assert {hit["doc_id"] for hit in unscoped} == {"std", "job"}
    await engine.dispose()


@pytest.mark.asyncio
async def test_search_in_empty_collection_returns_nothing(tmp_path):
    from app.index.fts import search_chunks

    engine, Session = await _seed_search_fixture(tmp_path)
    async with Session() as db:
        empty = await create_collection(db, "空分区")
        await db.commit()
        hits = await search_chunks(
            db, "振动试验", top_k=5,
            scope_doc_ids=await doc_ids_in_scope(db, await resolve_scope(db, empty.id)),
        )
        assert hits == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_scoped_search_still_routes_standard_identifier(tmp_path):
    """限定分区后，「GBT 38924.2 振动」仍必须命中那份标准，而不是退化成 BM25。"""
    from app.index.fts import search_chunks

    engine, Session = await _seed_search_fixture(tmp_path)
    async with Session() as db:
        hits = await search_chunks(
            db, "GBT 38924.2 振动", top_k=5,
            scope_doc_ids=await doc_ids_in_scope(db, await resolve_scope(db, "电子标准")),
        )
        assert hits
        assert hits[0]["doc_id"] == "std"
    await engine.dispose()


@pytest.mark.asyncio
async def test_identifier_outside_scope_returns_nothing(tmp_path):
    """标准编号不属于该分区时，返回空，而不是退回全库。"""
    from app.index.fts import search_chunks

    engine, Session = await _seed_search_fixture(tmp_path)
    async with Session() as db:
        hits = await search_chunks(
            db, "GBT 38924.2 振动", top_k=5,
            scope_doc_ids=await doc_ids_in_scope(db, await resolve_scope(db, "求职")),
        )
        assert hits == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_scoped_search_routes_identifier_inside_multi_doc_collection(tmp_path):
    """分区内有多份文档时，标准编号仍要压过同区其它文档的正文命中。

    这条用例专门分辨实现：若把分区当 doc_ids 传（关掉标识符路由），
    「GBT 38924.2 盐雾」会把同区的 GBT 38924.3 也带出来。
    """
    from app.index.fts import index_document, search_chunks

    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    Session = session_factory(engine)
    async with Session() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        std = await create_collection(db, "电子标准")
        for doc_id, title, heading, body in [
            ("vib", "GBT 38924.2-2020", "5 振动试验要求", "振动试验按本标准第 5 章执行"),
            ("salt", "GBT 38924.3-2020", "6 盐雾试验", "盐雾腐蚀试验方法见本章"),
        ]:
            doc = Document(
                id=doc_id, namespace_id="shared", title=title,
                filename=f"{doc_id}.pdf", file_path=f"{doc_id}.pdf",
                content_hash=doc_id, status="ready", collection_id=std.id,
            )
            db.add(doc)
            await db.flush()
            db.add(TocNode(
                doc_id=doc_id, node_id=f"{doc_id}:1", parent_id=None, level=1,
                heading_num=heading.split()[0], title=heading, heading_path=heading,
                source="toc_page", confidence=0.9,
            ))
            chunk = Chunk(
                doc_id=doc_id, node_id=f"{doc_id}:1", chunk_index=0,
                content=body, heading_path=heading,
            )
            db.add(chunk)
            await db.flush()
            await index_document(db, doc, [chunk])
        await db.commit()

        hits = await search_chunks(
            db, "GBT 38924.2 盐雾", top_k=5,
            scope_doc_ids=await doc_ids_in_scope(db, await resolve_scope(db, "电子标准")),
        )
        assert {hit["doc_id"] for hit in hits} == {"vib"}
    await engine.dispose()


@pytest.mark.asyncio
async def test_scoped_search_hint_names_other_collections(tmp_path):
    from app.index.fts import scoped_search

    engine, Session = await _seed_search_fixture(tmp_path)
    async with Session() as db:
        result = await scoped_search(db, "振动试验", top_k=5, collection="求职")
        assert [hit["doc_id"] for hit in result["results"]] == ["job"]
        assert result["hint"] is None
        assert result["scope"]["collection"]["name"] == "求职"

        empty = await create_collection(db, "空分区")
        await db.commit()
        miss = await scoped_search(db, "振动试验", top_k=5, collection=empty.id)
        assert miss["results"] == []
        assert "0 hits in collection '空分区'" in miss["hint"]
        assert "电子标准 (1)" in miss["hint"]
        assert "求职 (1)" in miss["hint"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_scoped_search_rejects_unknown_collection(tmp_path):
    from app.index.fts import scoped_search

    engine, Session = await _seed_search_fixture(tmp_path)
    async with Session() as db:
        with pytest.raises(CollectionNotFound):
            await scoped_search(db, "振动", collection="不存在")
    await engine.dispose()


@pytest.mark.asyncio
async def test_ask_scoped_to_collection_cites_that_collection(tmp_path):
    from app.index.ask import ask_question

    engine, Session = await _seed_search_fixture(tmp_path)
    async with Session() as db:
        result = await ask_question(db, "振动试验", top_k=3, collection="求职")
        assert result["citations"]
        assert {c["doc_id"] for c in result["citations"]} == {"job"}
        assert result["citations"][0]["collection"]["name"] == "求职"
    await engine.dispose()


@pytest.mark.asyncio
async def test_read_section_carries_collection(tmp_path):
    from app.index.sections import read_section

    engine, Session = await _seed_search_fixture(tmp_path)
    async with Session() as db:
        section = await read_section(db, "std", node_id="std:1")
        assert section["collection"]["name"] == "电子标准"
    await engine.dispose()


def _mcp_settings(tmp_path):
    from app.config import Settings

    return Settings(
        tocmemex_db_path=tmp_path / "tocmemex.db",
        tocmemex_data_dir=tmp_path / "data",
    )


def _tool_json(result) -> dict:
    """Read a tool's payload the way an agent does.

    ``structured_content`` is only populated for tools with a typed output
    model; a ``-> dict`` tool sends JSON in the text block instead.
    """
    import json

    assert result.is_error is False
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_mcp_list_collections_tool(tmp_path, monkeypatch):
    from app import mcp_server

    engine, _ = await _seed_two_collections(tmp_path)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: _mcp_settings(tmp_path))

    payload = _tool_json(await mcp_server.server.call_tool("list_collections", {}))
    by_name = {row["name"]: row for row in payload["collections"]}
    assert set(by_name) == {"电子标准", "求职"}
    assert by_name["电子标准"]["doc_count"] == 2
    assert by_name["电子标准"]["description"] == "国标行标"
    assert "description_missing" not in by_name["电子标准"]
    assert payload["unassigned_doc_count"] == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_mcp_list_collections_flags_missing_description(tmp_path, monkeypatch):
    from app import mcp_server

    engine, Session = await _seed_two_collections(tmp_path)
    async with Session() as db:
        await create_collection(db, "无说明分区")
        await db.commit()
    monkeypatch.setattr(mcp_server, "get_settings", lambda: _mcp_settings(tmp_path))

    payload = _tool_json(await mcp_server.server.call_tool("list_collections", {}))
    row = next(r for r in payload["collections"] if r["name"] == "无说明分区")
    assert row["description"] == ""
    assert row["description_missing"] is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_mcp_search_returns_object_with_scope(tmp_path, monkeypatch):
    from app import mcp_server

    engine, _ = await _seed_search_fixture(tmp_path)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: _mcp_settings(tmp_path))

    result = await mcp_server.server.call_tool(
        "search", {"query": "振动试验", "collection": "求职"}
    )
    payload = _tool_json(result)
    assert "results" in payload and "hint" in payload and "scope" in payload
    assert [hit["doc_id"] for hit in payload["results"]] == ["job"]
    assert payload["scope"]["collection"]["name"] == "求职"
    await engine.dispose()


@pytest.mark.asyncio
async def test_mcp_search_unknown_collection_falls_back(tmp_path, monkeypatch):
    from app import mcp_server

    engine, _ = await _seed_search_fixture(tmp_path)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: _mcp_settings(tmp_path))

    result = await mcp_server.server.call_tool(
        "search", {"query": "振动", "collection": "不存在"}
    )
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert "error" not in payload
    note = payload["collection_note"]
    assert "已改为全库检索" in note
    assert "电子标准" in note
    await engine.dispose()


async def _legacy_db(tmp_path):
    """A database created before collections existed."""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE namespaces (id VARCHAR(64) PRIMARY KEY, kind VARCHAR(20) NOT NULL,
            description TEXT, created_at VARCHAR(40) NOT NULL);
        CREATE TABLE documents (
            id VARCHAR(36) PRIMARY KEY, namespace_id VARCHAR(64) NOT NULL,
            title VARCHAR(500) NOT NULL, filename VARCHAR(500) NOT NULL,
            file_path VARCHAR(1000) NOT NULL, content_hash VARCHAR(64) NOT NULL,
            version INTEGER NOT NULL, doc_type VARCHAR(40), doc_date VARCHAR(20),
            source VARCHAR(40), summary TEXT, keywords TEXT,
            page_count INTEGER, char_count INTEGER,
            status VARCHAR(20) NOT NULL, error TEXT,
            created_at VARCHAR(40) NOT NULL, updated_at VARCHAR(40) NOT NULL);
        INSERT INTO namespaces (id, kind, created_at) VALUES ('shared', 'shared', 'x');
        INSERT INTO documents VALUES (
            'd1', 'shared', '旧标准', 'old.pdf', 'old.pdf', 'h', 1,
            NULL, NULL, NULL, NULL, NULL, 10, 100, 'ready', NULL, 'x', 'x');
        """
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.mark.asyncio
async def test_init_db_migrates_legacy_documents_table(tmp_path):
    """The CLI and scripts call init_db, not ensure_schema.

    A pre-collections database must gain the column from either entry
    point, or every ORM query on Document fails with "no such column".
    """
    db_path = await _legacy_db(tmp_path)
    engine = make_engine(db_path)
    await init_db(engine)
    async with session_factory(engine)() as db:
        from app.index.documents import list_documents

        docs = await list_documents(db)
        assert [doc["doc_id"] for doc in docs] == ["d1"]
        assert docs[0]["collection"] is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_ensure_schema_repairs_missing_collection_index(tmp_path):
    """A half-finished migration must heal on the next run.

    If the process dies between ADD COLUMN and CREATE INDEX, the column
    exists but the index never does; later runs must still create it.
    """
    db_path = tmp_path / "half.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE namespaces (id VARCHAR(64) PRIMARY KEY, kind VARCHAR(20) NOT NULL,
            description TEXT, created_at VARCHAR(40) NOT NULL);
        CREATE TABLE documents (
            id VARCHAR(36) PRIMARY KEY, namespace_id VARCHAR(64) NOT NULL,
            title VARCHAR(500) NOT NULL, filename VARCHAR(500) NOT NULL,
            file_path VARCHAR(1000) NOT NULL, content_hash VARCHAR(64) NOT NULL,
            version INTEGER NOT NULL, doc_type VARCHAR(40), doc_date VARCHAR(20),
            source VARCHAR(40), summary TEXT, keywords TEXT,
            page_count INTEGER, char_count INTEGER,
            status VARCHAR(20) NOT NULL, error TEXT,
            created_at VARCHAR(40) NOT NULL, updated_at VARCHAR(40) NOT NULL,
            collection_id VARCHAR(36));
        """
    )
    conn.commit()
    conn.close()

    engine = make_engine(db_path)
    await init_db(engine)
    async with engine.begin() as connection:
        indexes = {
            row[1]
            for row in (
                await connection.execute(text("PRAGMA index_list(documents)"))
            ).fetchall()
        }
    await engine.dispose()
    assert "ix_documents_collection_id" in indexes


@pytest.mark.asyncio
async def test_assign_reports_only_documents_that_changed(tmp_path):
    """Filing documents that are already in the target collection changes
    nothing, so the count must not claim it moved them."""
    engine, Session = await _seed_two_collections(tmp_path)
    async with Session() as db:
        electronics = (await resolve_scope(db, "电子标准")).collection
        updated, missing = await assign_documents(
            db, ["d1", "d2"], electronics.id
        )
        await db.commit()
        assert (updated, missing) == (0, [])
        # one already there, one moving in
        updated, missing = await assign_documents(db, ["d1", "d4"], electronics.id)
        await db.commit()
        assert updated == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_collection_names_are_unique_case_insensitively_for_unicode(tmp_path):
    """SQLite's lower()/NOCASE only fold ASCII, so "Ä" and "ä" both pass the
    pre-check. A Python-casefolded key column is the authority."""
    engine, Session = await _seed_two_collections(tmp_path)
    async with Session() as db:
        await create_collection(db, "Ärger")
        await db.commit()
        with pytest.raises(ValueError) as excinfo:
            await create_collection(db, "ärger")
        assert "already exists" in str(excinfo.value)
    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_duplicate_name_becomes_value_error(tmp_path):
    """Two requests can pass the pre-check together; the unique index then
    rejects the insert. That must surface as the same readable error, not
    an IntegrityError escaping as a 500."""
    from sqlalchemy.exc import IntegrityError

    from app.index import collections as collections_mod

    engine, Session = await _seed_two_collections(tmp_path)
    async with Session() as db:
        await create_collection(db, "并发同名")
        await db.commit()
        # Simulate the loser of the race: its pre-check saw nothing.
        original = collections_mod._name_taken
        collections_mod._name_taken = lambda *a, **k: _false()
        try:
            with pytest.raises(ValueError) as excinfo:
                await create_collection(db, "并发同名")
            assert "already exists" in str(excinfo.value)
        finally:
            collections_mod._name_taken = original
        await db.rollback()
    await engine.dispose()


async def _false() -> bool:
    return False


@pytest.mark.asyncio
async def test_legacy_collections_table_gains_name_key(tmp_path):
    """A database written by the first version of this feature has
    collections.name but no name_key. The migration must backfill it."""
    db_path = tmp_path / "oldcol.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE collections (
            id VARCHAR(36) PRIMARY KEY, name VARCHAR(80) NOT NULL UNIQUE,
            description TEXT, sort_order INTEGER NOT NULL DEFAULT 0,
            created_at VARCHAR(40) NOT NULL, updated_at VARCHAR(40) NOT NULL);
        INSERT INTO collections VALUES ('c1', 'Job', NULL, 0, 'x', 'x');
        """
    )
    conn.commit()
    conn.close()

    engine = make_engine(db_path)
    await init_db(engine)
    async with session_factory(engine)() as db:
        from app.index.collections import create_collection, resolve_scope

        # the backfilled key makes the old row reachable case-insensitively
        scope = await resolve_scope(db, "job")
        assert scope.collection.id == "c1"
        with pytest.raises(ValueError):
            await create_collection(db, "JOB")
    await engine.dispose()


@pytest.mark.asyncio
async def test_migration_survives_preexisting_case_duplicates(tmp_path):
    """A DB that already holds Job/job must still start; the index is
    skipped, not fatal, and the pre-check keeps refusing new ones."""
    db_path = tmp_path / "dups.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE collections (
            id VARCHAR(36) PRIMARY KEY, name VARCHAR(80) NOT NULL UNIQUE,
            description TEXT, sort_order INTEGER NOT NULL DEFAULT 0,
            created_at VARCHAR(40) NOT NULL, updated_at VARCHAR(40) NOT NULL);
        INSERT INTO collections VALUES ('c1', 'Job', NULL, 0, 'x', 'x');
        INSERT INTO collections VALUES ('c2', 'job', NULL, 0, 'x', 'x');
        """
    )
    conn.commit()
    conn.close()

    engine = make_engine(db_path)
    await init_db(engine)  # must not raise
    async with session_factory(engine)() as db:
        from app.index.collections import create_collection

        with pytest.raises(ValueError):
            await create_collection(db, "JOB")
    await engine.dispose()


@pytest.mark.asyncio
async def test_suggest_and_apply_seed_a_designation_library(tmp_path):
    """A fresh numbered library should not stay 100% unfiled.

    Groups by designation family; documents with no designation stay
    unfiled rather than getting an invented collection.
    """
    from app.index.collections import apply_suggestions, suggest_collections

    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        for doc_id, title, filename in [
            ("d1", "ACME 100.3-2024 恒温", "ACME 100.3-2024.pdf"),
            ("d2", "ACME 100.1-2023 低温", "ACME 100.1-2023.pdf"),
            ("d3", "RPT-24 7 方法", "RPT-24-7.pdf"),
            ("d4", "随手记", "notes.txt"),
        ]:
            db.add(
                Document(
                    id=doc_id, namespace_id="shared", title=title,
                    filename=filename, file_path=filename,
                    content_hash=doc_id, status="ready",
                )
            )
        await db.commit()

        suggestions = await suggest_collections(db)
        assert [group["name"] for group in suggestions] == ["ACME 100", "RPT 24"]
        assert [group["doc_ids"] for group in suggestions] == [["d1", "d2"], ["d3"]]

        result = await apply_suggestions(db, suggestions)
        await db.commit()
        assert result == {"collections": 2, "created": 2, "assigned": 3}

        names = {row["name"] for row in await list_collections(db)}
        assert names == {"ACME 100", "RPT 24"}
        assert await unassigned_doc_count(db) == 1  # the ungrouped note
    await engine.dispose()
