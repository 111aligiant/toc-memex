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


async def test_quality_groups_does_not_open_a_connection_per_document(
    tmp_path, monkeypatch
):
    """quality_groups must not re-query doc_row per document (N+1).

    It gets every document from doc_summaries, whose per-document stats are
    already memoized; rebuilding a row with doc_row() for each one opens a
    fresh read-only connection per document. Count connect_ro() calls to make
    sure the per-document ``doc_row`` lookup is gone: the connection count must
    not scale with the number of documents.
    """
    import app.curation.issues as issues_mod

    titles = [(f"doc{i}", f"ACME {i:03d}.11-2020 方法") for i in range(12)]
    db_path = await _seed(tmp_path, titles)

    real_connect_ro = issues_mod.connect_ro
    calls = {"n": 0}

    def counting_connect_ro(path):
        calls["n"] += 1
        return real_connect_ro(path)

    monkeypatch.setattr(issues_mod, "connect_ro", counting_connect_ro)
    issues_mod.invalidate_stats_cache()
    groups = quality_groups(db_path, None)
    assert groups  # sanity: something was produced

    # Doc_summaries opens one connection to fetch all rows; latest_runs opens
    # one; compute_doc_stats opens one per *uncached* document (the first pass
    # through doc_summaries). There must be NO extra per-document doc_row
    # connection on top of that. With stats memoized from doc_summaries, the
    # quality_groups loop adds zero connections of its own.
    #
    # Concretely: latest_runs=1, doc_summaries main query=1,
    # compute_doc_stats first-pass = len(docs). Anything beyond that is the
    # N+1 we removed. Allow a small constant slack, never O(docs) extra.
    allowed = 2 + len(titles)  # +2 for the two list-level connections
    assert calls["n"] <= allowed, (
        f"quality_groups opened {calls['n']} connections for {len(titles)} "
        f"docs (allowed <= {allowed}); the per-document doc_row N+1 is back"
    )
