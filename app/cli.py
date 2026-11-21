from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select, text

from .config import get_settings
from .db import init_db, make_engine, session_factory
from .index.ask import ask_question
from .index.documents import (
    get_document,
    get_toc,
    list_documents,
    memex_overview,
)
from .index.fts import search_chunks
from .index.sections import read_section
from .ingest.pipeline import ingest_path, set_engine
from .ingest.reindex import (
    backfill_all_images,
    backfill_document_images,
    reindex_all,
    reindex_document,
)
from .ingest.toc import validate_toc
from .models import Namespace, TocNode

DEFAULT_NAMESPACES = ["shared", "human"]


async def _init(args=None) -> int:
    settings = get_settings()
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    try:
        async with session_factory(engine)() as db:
            for name in DEFAULT_NAMESPACES:
                if await db.get(Namespace, name) is None:
                    db.add(
                        Namespace(
                            id=name,
                            kind="shared" if name == "shared" else "agent",
                        )
                    )
            await db.commit()
        print("initialized")
        return 0
    finally:
        await engine.dispose()


async def _ingest(args) -> int:
    settings = get_settings()
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    try:
        doc_id = await ingest_path(
            Path(args.path),
            namespace=args.namespace,
            doc_type=args.type,
            doc_date=args.date,
            settings=settings,
            image_mode=getattr(args, "image_mode", "structured"),
        )
        print(doc_id)
        return 0
    finally:
        await engine.dispose()


async def _stats(args=None) -> int:
    settings = get_settings()
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    try:
        async with session_factory(engine)() as db:
            docs = (await db.execute(text("select count(*) from documents"))).scalar()
            tocs = (await db.execute(text("select count(*) from toc_nodes"))).scalar()
            chunks = (await db.execute(text("select count(*) from chunks"))).scalar()
        print(f"documents: {docs}")
        print(f"toc_nodes: {tocs}")
        print(f"chunks: {chunks}")
        return 0
    finally:
        await engine.dispose()


async def _overview(args) -> int:
    settings = get_settings()
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    try:
        async with session_factory(engine)() as db:
            data = await memex_overview(db)
        if getattr(args, "json", False):
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(f"documents: {data['documents']}")
            print(f"toc_nodes: {data['toc_nodes']}")
            print(f"chunks: {data['chunks']}")
            print(f"types: {data['doc_types']}")
            for doc in data["recent"]:
                print(
                    f"  {doc['doc_id']}  {doc['status']}  {doc['title']}"
                )
        return 0
    finally:
        await engine.dispose()


async def _docs(args) -> int:
    settings = get_settings()
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    try:
        async with session_factory(engine)() as db:
            documents = await list_documents(
                db,
                query=args.query,
                doc_type=args.type,
                limit=args.limit,
            )
        if getattr(args, "json", False):
            print(json.dumps(documents, ensure_ascii=False, indent=2))
        else:
            for doc in documents:
                summary = (doc.get("summary") or "")[:80]
                print(
                    f"{doc['doc_id']}\t{doc['status']}\t"
                    f"{doc['doc_type'] or '-'}\t{doc['title']}\t{summary}"
                )
        return 0
    finally:
        await engine.dispose()


async def _doc(args) -> int:
    settings = get_settings()
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    try:
        async with session_factory(engine)() as db:
            document = await get_document(db, args.doc_id)
        if getattr(args, "json", False):
            print(json.dumps(document, ensure_ascii=False, indent=2))
            return 0
        print(f"{document['title']}  ({document['doc_id']})")
        print(
            f"status: {document['status']}; pages: {document['page_count']}; "
            f"toc: {document['toc_nodes']}; chunks: {document['chunks']}"
        )
        print(f"summary: {document['summary'] or ''}")
        print(f"keywords: {document['keywords'] or ''}")
        for root in document["toc_roots"]:
            pages = (
                f"{root['page_start']}-{root['page_end']}"
                if root["page_start"] is not None
                else "?"
            )
            print(
                f"  {root['heading_num'] or ''} {root['title']} "
                f"pages {pages}"
            )
        return 0
    finally:
        await engine.dispose()


async def _toc(args) -> int:
    settings = get_settings()
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    try:
        if getattr(args, "json", False):
            async with session_factory(engine)() as db:
                data = await get_toc(
                    db, args.doc_id, depth=getattr(args, "depth", 2)
                )
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0

        async with session_factory(engine)() as db:
            rows = (
                await db.execute(
                    select(TocNode)
                    .where(TocNode.doc_id == args.doc_id)
                    .order_by(TocNode.id)
                )
            ).scalars().all()
        if not rows:
            print("no TOC for this document")
            return 1

        by_parent: dict[str | None, list[TocNode]] = {}
        for node in rows:
            by_parent.setdefault(node.parent_id, []).append(node)
        known = {node.node_id for node in rows}
        roots = [
            node
            for node in rows
            if node.parent_id is None or node.parent_id not in known
        ]

        def emit(node: TocNode, depth: int) -> None:
            indent = "  " * depth
            pages = (
                f"{node.page_start}-{node.page_end}"
                if node.page_start is not None
                else "?"
            )
            print(
                f"{indent}{node.node_id} [{node.level}] {node.title} "
                f"pages {pages} ({node.source})"
            )
            # Stored order is the tree's own reading order (rows come ordered
            # by id); re-sorting by page here scrambles siblings whenever one
            # page mapping is off.
            for child in by_parent.get(node.node_id, []):
                emit(child, depth + 1)

        for root in roots:
            emit(root, 0)

        issues = validate_toc(rows)
        errors = sum(1 for issue in issues if issue.severity == "error")
        warnings = sum(1 for issue in issues if issue.severity == "warning")
        print(f"\nvalidation: errors={errors} warnings={warnings}")
        for issue in issues[:8]:
            print(f"  {issue.severity}: {issue.message}")
        return 0
    finally:
        await engine.dispose()


async def _search(args) -> int:
    settings = get_settings()
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    try:
        async with session_factory(engine)() as db:
            hits = await search_chunks(db, args.query, top_k=args.top)
        if getattr(args, "json", False):
            print(json.dumps(hits, ensure_ascii=False, indent=2))
            return 0
        for hit in hits:
            pages = (
                f"{hit['page_start']}-{hit['page_end']}"
                if hit.get("page_start") is not None
                else "?"
            )
            print(
                f"{hit.get('score', 0):.6f}\t"
                f"{hit.get('match_type', '')}\t"
                f"chunk={hit.get('chunk_id')}\tnode={hit.get('node_id')}\t"
                f"pages={pages}\t{hit.get('heading_path', '')}\t"
                f"{hit.get('snippet', '')}"
            )
        return 0
    finally:
        await engine.dispose()


async def _read(args) -> int:
    settings = get_settings()
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    try:
        async with session_factory(engine)() as db:
            result = await read_section(
                db,
                args.doc_id,
                node_id=args.node,
                max_chars=args.max_chars,
                cursor=args.cursor,
            )
        if getattr(args, "json", False):
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        pages = (
            f"{result['page_start']}-{result['page_end']}"
            if result.get("page_start") is not None
            else "?"
        )
        print(f"# {result['heading_path']}")
        print(f"pages: {pages}; chunks: {result['chunk_ids']}")
        print()
        print(result["content"])
        if result.get("next_cursor") is not None:
            print(f"\n[next_cursor={result['next_cursor']}]")
        return 0
    finally:
        await engine.dispose()


async def _ask(args) -> int:
    settings = get_settings()
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    try:
        async with session_factory(engine)() as db:
            result = await ask_question(
                db,
                args.question,
                doc_ids=args.doc_id,
                top_k=args.top,
                max_chars=args.max_chars,
            )
        if getattr(args, "json", False):
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["answer"] else 1
        if not result["answer"]:
            print(result["hint"])
            return 1
        print(result["answer"])
        print("\n来源：")
        for index, citation in enumerate(result["citations"], 1):
            start = citation.get("page_start")
            end = citation.get("page_end")
            pages = f"{start}-{end}" if start is not None else "?"
            print(
                f"[{index}] {citation['doc_title']} › "
                f"{citation['heading_path']}（PDF p.{pages}）"
            )
        return 0
    finally:
        await engine.dispose()


async def _reindex(args) -> int:
    settings = get_settings()
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    try:
        if args.all_docs:
            ids = await reindex_all(settings)
        elif args.doc_id:
            ids = [await reindex_document(args.doc_id, settings)]
        else:
            print("specify a doc_id or --all")
            return 2
        for doc_id in ids:
            print(doc_id)
        return 0
    finally:
        await engine.dispose()


async def _images(args) -> int:
    """Download figure images an earlier ingest failed to fetch."""
    settings = get_settings()
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    try:
        if args.all_docs:
            results = await backfill_all_images(settings)
        elif args.doc_id:
            results = [await backfill_document_images(args.doc_id, settings)]
        else:
            print("specify a doc_id or --all")
            return 2
        for result in results:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    finally:
        await engine.dispose()


async def _collections(args) -> int:
    """List, suggest or seed the library's topic collections.

    ``suggest``/``apply`` exist because a fresh library has no collections
    at all: every document is "unfiled", `collection=` filters match
    nothing, and the whole topic axis is dead weight. The grouping is by
    document designation family and is reversible from the Web UI.
    """
    from .index.collections import (
        apply_suggestions,
        list_collections,
        suggest_collections,
        unassigned_doc_count,
    )

    settings = get_settings()
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    try:
        async with session_factory(engine)() as db:
            if args.action == "list":
                rows = await list_collections(db)
                unassigned = await unassigned_doc_count(db)
                if args.json:
                    print(json.dumps(
                        {"collections": rows, "unassigned": unassigned},
                        ensure_ascii=False, indent=2,
                    ))
                else:
                    for row in rows:
                        print(
                            f"{row['doc_count']:4}  {row['name']}"
                            f"{'  — ' + row['description'] if row['description'] else ''}"
                        )
                    print(f"{unassigned} unfiled")
                return 0
            if args.action == "suggest":
                groups = await suggest_collections(db)
                if args.json:
                    print(json.dumps(groups, ensure_ascii=False, indent=2))
                else:
                    for group in groups:
                        print(f"{len(group['doc_ids']):4}  {group['name']}")
                    print(f"{len(groups)} proposed collection(s)")
                return 0
            if args.action == "apply":
                result = await apply_suggestions(db)
                await db.commit()
                print(json.dumps(result, ensure_ascii=False))
                return 0
        print(f"unknown action: {args.action}")
        return 2
    finally:
        await engine.dispose()


async def _provenance(args) -> int:
    """Backfill or print ingest provenance."""
    from .ingest.provenance import backfill_runs, run_out

    settings = get_settings()
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    try:
        async with session_factory(engine)() as db:
            if args.backfill:
                result = await backfill_runs(db, settings, overwrite=args.overwrite)
                print(json.dumps(result, ensure_ascii=False))
                return 0
            from sqlalchemy import desc

            from .models import IngestRun

            stmt = select(IngestRun).order_by(desc(IngestRun.id))
            if args.doc_id:
                stmt = stmt.where(IngestRun.doc_id == args.doc_id)
            rows = (await db.execute(stmt.limit(args.limit))).scalars().all()
            for row in rows:
                if args.json:
                    print(json.dumps(run_out(row), ensure_ascii=False))
                else:
                    print(
                        f"{row.doc_id[:8]} {row.recorded_by:8} "
                        f"ocr={row.ocr_provider or '-':7} "
                        f"llm={row.llm_outcome or '-':11} "
                        f"toc={row.toc_source or '-':14} "
                        f"nodes={row.node_count if row.node_count is not None else '-'}"
                    )
            return 0
    finally:
        await engine.dispose()


async def _toc_review(args) -> int:
    """纲目复审：locate nodes whose titles are off their declared pages.

    Dry run by default; ``--propose`` files the located ``set_page`` ops as
    one pending proposal (high-risk ops always wait for a human).
    """
    from .curation.dispatch import Dispatcher
    from .curation.ops import build_context
    from .ingest.toc_review import review_document
    from .models import Document

    settings = get_settings()
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    try:
        async with session_factory(engine)() as db:
            if args.task:
                from .ingest.toc_review import consume_review_task

                try:
                    result = await consume_review_task(
                        db,
                        settings.tocmemex_data_dir,
                        args.task,
                        limit=args.limit,
                        window=args.window,
                    )
                except (LookupError, ValueError) as exc:
                    await db.rollback()
                    print(str(exc))
                    return 1
                await db.commit()
                if args.json:
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                    return 0
                if result.get("skipped"):
                    print(f"task {args.task}: {result['skipped']}")
                    return 0
                report = result["report"]
                print(
                    f"工单 {args.task} → {result['title'][:50]}: "
                    f"checked {report['checked']}, proposed {len(report['proposed'])}, "
                    f"ambiguous {len(report['ambiguous'])}, "
                    f"unlocatable {len(report['unlocatable'])}"
                )
                if result.get("proposal"):
                    proposal = result["proposal"]
                    print(
                        f"proposal {proposal['proposal_id']} filed: "
                        f"pending {proposal['pending_ops']} / dropped {proposal['dropped_ops']}"
                    )
                return 0
            if not args.doc_id:
                print("需要 doc_id，或用 --task <id> 消费纲目复审工单")
                return 2
            doc = await db.get(Document, args.doc_id)
            if doc is None:
                print(f"document not found: {args.doc_id}")
                return 2
            ctx = await build_context(db, doc, settings.tocmemex_data_dir)
            report = await review_document(
                db, doc, ctx.pages, limit=args.limit, window=args.window
            )
            proposal = None
            if args.propose and report["proposed"]:
                dispatcher = Dispatcher(
                    db, settings.tocmemex_data_dir, doc.id
                )
                proposal = await dispatcher.propose_ops(
                    [entry["op"] for entry in report["proposed"]],
                    rationale="纲目复审：节点标题不在声明页，按页内定位修正",
                )
                await db.commit()
        if args.json:
            print(json.dumps({**report, "proposal": proposal}, ensure_ascii=False, indent=2))
            return 0
        if report["checked"] == 0:
            print("no cached pages to review against (nothing checked)")
            return 0
        print(
            f"{doc.title[:50]}: checked {report['checked']}, "
            f"ok {report['ok']}, proposed {len(report['proposed'])}, "
            f"ambiguous {len(report['ambiguous'])}, "
            f"unlocatable {len(report['unlocatable'])}, "
            f"skipped {report['skipped']}"
        )
        for entry in report["proposed"]:
            print(
                f"  FIX {entry['node_id']}: {entry['title'][:40]} "
                f"pages {entry['declared'][0]}-{entry['declared'][1]} → {entry['found']}"
            )
        for entry in report["ambiguous"]:
            print(f"  ?   {entry['node_id']}: {entry['title'][:40]} hits {entry['hits']}")
        if proposal:
            print(
                f"proposal {proposal['proposal_id']} filed: "
                f"pending {proposal['pending_ops']} / dropped {proposal['dropped_ops']}"
            )
        return 0
    finally:
        await engine.dispose()


async def _baseline(args) -> int:
    """Create, list or restore document baselines (ADR-0002 §4)."""
    from .curation import baselines as baselines_mod
    from .models import Document

    settings = get_settings()
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    try:
        async with session_factory(engine)() as db:
            if args.action == "create":
                if args.all_docs:
                    ids = (await db.execute(select(Document.id))).scalars().all()
                elif args.doc_id:
                    ids = [args.doc_id]
                else:
                    print("specify a doc_id or --all")
                    return 2
                created = skipped = 0
                for doc_id in ids:
                    try:
                        row = await baselines_mod.create_baseline(
                            db, doc_id, args.reason, overwrite=args.overwrite
                        )
                    except ValueError as exc:
                        print(f"{doc_id}: {exc}")
                        continue
                    if row is None:
                        skipped += 1
                    else:
                        created += 1
                        print(
                            f"{doc_id}\tbaseline {row.id}\t"
                            f"{row.node_count} nodes / {row.chunk_count} chunks"
                        )
                await db.commit()
                print(f"created {created}, already present {skipped}")
                return 0

            if args.action == "list":
                if args.doc_id:
                    rows = (
                        (
                            await db.execute(
                                select(baselines_mod.DocBaseline)
                                .where(baselines_mod.DocBaseline.doc_id == args.doc_id)
                                .order_by(baselines_mod.DocBaseline.id.desc())
                            )
                        )
                        .scalars()
                        .all()
                    )
                else:
                    rows = (
                        (
                            await db.execute(
                                select(baselines_mod.DocBaseline).order_by(
                                    baselines_mod.DocBaseline.id.desc()
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                for row in rows:
                    print(
                        f"{row.id}\t{row.doc_id[:8]}\t{row.reason}\t"
                        f"{row.node_count}/{row.chunk_count}\t{row.created_at}"
                    )
                print(f"{len(rows)} baseline(s)")
                return 0

            if args.action == "restore":
                if args.all_docs:
                    ids = (await db.execute(select(Document.id))).scalars().all()
                elif args.doc_id:
                    ids = [args.doc_id]
                else:
                    print("specify a doc_id or --all")
                    return 2
                failures = 0
                for doc_id in ids:
                    try:
                        result = await baselines_mod.restore_baseline(
                            db, doc_id, force=args.force
                        )
                    except (ValueError, LookupError) as exc:
                        await db.rollback()
                        print(f"{doc_id}: {exc}")
                        failures += 1
                        continue
                    await db.commit()
                    print(json.dumps(result, ensure_ascii=False))
                return 1 if failures else 0

        print(f"unknown action: {args.action}")
        return 2
    finally:
        await engine.dispose()


async def _anchor_ids(args) -> int:
    """Rewrite positional node ids to content anchors (ADR-0002 §6.3)."""
    from .curation.migrate import anchor_all, anchor_document

    settings = get_settings()
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    try:
        async with session_factory(engine)() as db:
            if args.all_docs:
                results = await anchor_all(db)
            elif args.doc_id:
                results = [await anchor_document(db, args.doc_id)]
                await db.commit()
            else:
                print("specify a doc_id or --all")
                return 2
        for result in results:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="toc-memex")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create database and default namespaces")

    ingest = sub.add_parser("ingest", help="ingest one file")
    ingest.add_argument("path")
    ingest.add_argument("--namespace", default="shared")
    ingest.add_argument("--type", default=None)
    ingest.add_argument("--date", default=None)
    # Images only: "structured" builds a TOC/figures, "text" keeps one flat
    # searchable text document. Non-image files ignore it.
    ingest.add_argument(
        "--image-mode", choices=["structured", "text"], default="structured"
    )

    sub.add_parser("stats", help="show document/toc/chunk counts")

    prov = sub.add_parser("provenance", help="show or backfill ingest provenance")
    prov.add_argument("doc_id", nargs="?", default=None)
    prov.add_argument("--backfill", action="store_true", help="write missing rows")
    prov.add_argument(
        "--overwrite", action="store_true", help="with --backfill: redo every document"
    )
    prov.add_argument("--limit", type=int, default=200)
    prov.add_argument("--json", action="store_true")

    overview = sub.add_parser("overview", help="show Toc Memex overview")
    overview.add_argument("--json", action="store_true")

    docs = sub.add_parser("docs", help="list documents")
    docs.add_argument("--query", default="")
    docs.add_argument("--type", default="")
    docs.add_argument("--limit", type=int, default=20)
    docs.add_argument("--json", action="store_true")

    doc = sub.add_parser("doc", help="show one document")
    doc.add_argument("doc_id")
    doc.add_argument("--json", action="store_true")

    toc = sub.add_parser("toc", help="print a document TOC")
    toc.add_argument("doc_id")
    toc.add_argument("--depth", type=int, default=2)
    toc.add_argument("--json", action="store_true")

    search = sub.add_parser("search", help="BM25 search")
    search.add_argument("query")
    search.add_argument("--top", type=int, default=8)
    search.add_argument("--json", action="store_true", help="print full hit metadata")

    read = sub.add_parser("read", help="read a section by node id")
    read.add_argument("doc_id")
    read.add_argument("--node", required=True, help="TOC node id, e.g. doc:0082")
    read.add_argument("--max-chars", type=int, default=8000)
    read.add_argument("--cursor", type=int, default=0)
    read.add_argument("--json", action="store_true", help="print citation metadata")

    ask = sub.add_parser("ask", help="extractive answer with section citations")
    ask.add_argument("question")
    ask.add_argument("--doc-id", action="append", default=None)
    ask.add_argument("--top", type=int, default=3)
    ask.add_argument("--max-chars", type=int, default=1200)
    ask.add_argument("--json", action="store_true", help="print full answer metadata")

    reindex = sub.add_parser(
        "reindex", help="rebuild TOC/chunks/FTS from cached OCR (no OCR call)"
    )
    reindex.add_argument("doc_id", nargs="?")
    reindex.add_argument("--all", action="store_true", dest="all_docs")

    images = sub.add_parser(
        "images", help="re-download figure images missing from resources"
    )
    images.add_argument("doc_id", nargs="?")
    images.add_argument("--all", action="store_true", dest="all_docs")

    baseline = sub.add_parser(
        "baseline", help="create / list / restore pre-curator baselines"
    )
    baseline.add_argument("action", choices=["create", "list", "restore"])
    baseline.add_argument("--doc", default=None, dest="doc_id")
    baseline.add_argument("--all", action="store_true", dest="all_docs")
    baseline.add_argument(
        "--reason",
        default="manual",
        choices=["adoption", "pre_curator_write", "manual"],
    )
    baseline.add_argument(
        "--overwrite", action="store_true", help="create: rewrite an existing baseline"
    )
    baseline.add_argument(
        "--force", action="store_true", help="restore: allow a content-hash mismatch"
    )

    anchor = sub.add_parser(
        "anchor-ids", help="rewrite positional node ids to content anchors"
    )
    anchor.add_argument("doc_id", nargs="?")
    anchor.add_argument("--all", action="store_true", dest="all_docs")

    collections = sub.add_parser(
        "collections", help="list / suggest / seed topic collections"
    )
    collections.add_argument("action", choices=["list", "suggest", "apply"])
    collections.add_argument("--json", action="store_true")

    review = sub.add_parser(
        "toc-review",
        help="纲目复审：locate nodes whose titles are off their declared pages",
    )
    review.add_argument("doc_id", nargs="?")
    review.add_argument("--task", default=None, help="consume a gangmu-review task id")
    review.add_argument("--limit", type=int, default=20)
    review.add_argument("--window", type=int, default=10)
    review.add_argument(
        "--propose",
        action="store_true",
        help="file the located fixes as one pending proposal (set_page waits for a human)",
    )
    review.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    handlers = {
        "init": _init,
        "ingest": _ingest,
        "stats": _stats,
        "overview": _overview,
        "docs": _docs,
        "doc": _doc,
        "toc": _toc,
        "search": _search,
        "read": _read,
        "ask": _ask,
        "reindex": _reindex,
        "images": _images,
        "provenance": _provenance,
        "baseline": _baseline,
        "anchor-ids": _anchor_ids,
        "collections": _collections,
        "toc-review": _toc_review,
    }
    return asyncio.run(handlers[args.command](args))


if __name__ == "__main__":
    raise SystemExit(main())