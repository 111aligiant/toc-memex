from __future__ import annotations

from app.curation.issues import quality_groups
from app.db import init_db, make_engine, session_factory
from app.identifiers import document_key
from app.models import Document, Namespace


def test_document_key_normalizes_variants():
    assert document_key("ACME 100.56-2023 方法") == document_key(
        "ACME_100.56-2023 方法"
    )
    assert document_key("ACME/100.56-2023 方法") == document_key(
        "ACME 100.56-2023 方法"
    )
    assert document_key("ACME+38058-2019") == document_key("ACME 38058-2019")
    assert document_key("RPT-160G 设备") == "RPT160G"
    assert document_key("没有编号的文档") == ""


async def _seed(tmp_path, titles: list[tuple[str, str]]):
    db_path = tmp_path / "tocmemex.db"
    engine = make_engine(db_path)
    await init_db(engine)
    async with session_factory(engine)() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        for doc_id, title in titles:
            db.add(
                Document(
                    id=doc_id,
                    namespace_id="shared",
                    title=title,
                    filename=f"{doc_id}.pdf",
                    file_path=f"{doc_id}.pdf",
                    content_hash=doc_id,
                    page_count=10,
                    status="ready",
                )
            )
        await db.commit()
    await engine.dispose()
    return db_path


async def test_quality_groups_flags_duplicate_documents(tmp_path):
    db_path = await _seed(
        tmp_path,
        [
            ("doc1", "ACME 100.56-2023 方法"),
            ("doc2", "ACME_100.56-2023 方法"),
        ],
    )
    groups = quality_groups(db_path, None)
    duplicate = [group for group in groups if group["kind"] == "duplicate-doc"]
    assert len(duplicate) == 2
    assert {group["relatedDocId"] for group in duplicate} == {"doc1", "doc2"}
    assert all(group["title"] == "重复文档" for group in duplicate)
    assert all(group["severity"] == "warning" for group in duplicate)


async def test_quality_groups_ignores_single_documents(tmp_path):
    db_path = await _seed(tmp_path, [("doc1", "ACME 100.56-2023 方法")])
    groups = quality_groups(db_path, None)
    assert [group for group in groups if group["kind"] == "duplicate-doc"] == []
