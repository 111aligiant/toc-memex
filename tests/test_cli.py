from __future__ import annotations

import asyncio

from app.cli import main
from app.config import Settings
from app.db import init_db, make_engine, session_factory
from app.models import Chunk, Document, Namespace, TocNode


async def _seed(settings: Settings) -> None:
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(
            Document(
                id="d1",
                namespace_id="shared",
                title="测试标准",
                filename="test.pdf",
                file_path="test.pdf",
                content_hash="h",
                doc_type="standard",
                status="ready",
            )
        )
        await db.flush()
        db.add(
            TocNode(
                doc_id="d1",
                node_id="d1:1",
                parent_id=None,
                level=1,
                heading_num="第1章",
                title="第1章 范围",
                heading_path="第1章 范围",
                page_start=1,
                page_end=2,
                chunk_start=0,
                chunk_end=0,
                source="toc_page",
                confidence=0.9,
            )
        )
        db.add(
            Chunk(
                doc_id="d1",
                node_id="d1:1",
                chunk_index=0,
                content="本文件规定了范围。",
                heading_path="第1章 范围",
                page_start=1,
                page_end=1,
            )
        )
        await db.commit()
    await engine.dispose()


def test_cli_init_and_stats(tmp_path, monkeypatch, capsys):
    settings = Settings(
        tocmemex_db_path=tmp_path / "tocmemex.db",
        tocmemex_data_dir=tmp_path / "data",
    )
    monkeypatch.setattr("app.cli.get_settings", lambda: settings)

    assert main(["init"]) == 0
    assert main(["stats"]) == 0
    out = capsys.readouterr().out
    assert "documents" in out.lower() or "文档" in out


def test_cli_document_commands(tmp_path, monkeypatch, capsys):
    settings = Settings(
        tocmemex_db_path=tmp_path / "tocmemex.db",
        tocmemex_data_dir=tmp_path / "data",
    )
    monkeypatch.setattr("app.cli.get_settings", lambda: settings)
    asyncio.run(_seed(settings))

    assert main(["overview", "--json"]) == 0
    assert '"documents": 1' in capsys.readouterr().out

    assert main(["docs", "--json"]) == 0
    assert "测试标准" in capsys.readouterr().out

    assert main(["doc", "d1", "--json"]) == 0
    assert '"doc_id": "d1"' in capsys.readouterr().out

    assert main(["toc", "d1", "--json", "--depth", "1"]) == 0
    assert '"roots"' in capsys.readouterr().out


def test_cli_init_creates_default_namespaces(tmp_path, monkeypatch):
    from sqlalchemy import select

    from app.db import init_db, make_engine, session_factory
    from app.models import Namespace

    settings = Settings(
        tocmemex_db_path=tmp_path / "tocmemex.db",
        tocmemex_data_dir=tmp_path / "data",
    )
    monkeypatch.setattr("app.cli.get_settings", lambda: settings)
    assert main(["init"]) == 0
    assert (tmp_path / "tocmemex.db").exists()

    async def load():
        engine = make_engine(settings.tocmemex_db_path)
        await init_db(engine)
        async with session_factory(engine)() as db:
            return (await db.execute(select(Namespace.id))).scalars().all()

    assert "shared" in asyncio.run(load())
