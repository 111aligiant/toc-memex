from __future__ import annotations

from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..db import session_factory
from ..models import Document, utcnow_iso
from ..ocr.cache import load_pages
from ..ocr.models import ParsedDocument
from ..ocr.parser import page_text, parse_jsonl
from .pipeline import (
    _attach_gangmu_lessons,
    _resolve_llm_settings,
    _resolve_ocr_settings,
    backfill_images,
    get_engine,
    load_bookmarks,
    parse_markdown,
    parse_xlsx,
    persist_parsed,
    persist_plain_text,
    read_pdf_bookmarks,
    save_bookmarks,
)
from .provenance import build_run, record_run


def load_parsed(doc: Document, settings: Settings) -> ParsedDocument:
    """Rebuild a ParsedDocument without calling OCR.

    PDF/DOCX prefer the provider-neutral ``pages.json`` cache, then a legacy
    Paddle ``raw.jsonl``; Markdown/TXT/XLSX are re-read from the original
    file. Bookmarks prefer the saved JSON, then the PDF outline.
    """
    path = Path(doc.file_path)
    suffix = path.suffix.lower()

    if suffix in {".pdf", ".docx", ".doc"}:
        cache_dir = settings.ocr_dir / doc.id
        raw_path = cache_dir / "raw.jsonl"
        cached = load_pages(cache_dir)
        if cached is not None:
            pages = cached.pages
        elif raw_path.exists():
            # Legacy Paddle cache written before pages.json existed.
            pages = parse_jsonl(raw_path.read_text(encoding="utf-8"))
        elif suffix == ".pdf":
            from .pdf_text import parse_pdf_text

            parsed = parse_pdf_text(path) if path.exists() else None
            if parsed is None:
                raise FileNotFoundError(
                    f"OCR cache missing for {doc.id}: {raw_path}; "
                    "re-ingest required"
                )
            from .pdf_media import attach_pdf_text_media

            attach_pdf_text_media(path, parsed, settings.ocr_dir / doc.id)
            pages = parsed.pages
        elif suffix in {".docx", ".doc"}:
            from .docx_direct import parse_docx_direct
            from .pipeline import _docx_direct_has_text

            parsed = parse_docx_direct(path) if path.exists() else None
            if parsed is None or not _docx_direct_has_text(parsed):
                raise FileNotFoundError(
                    f"OCR cache missing for {doc.id}: {raw_path}; "
                    "re-ingest required"
                )
            pages = parsed.pages
        else:
            raise FileNotFoundError(
                f"OCR cache missing for {doc.id}: {raw_path}; re-ingest required"
            )
        bookmarks = load_bookmarks(settings, doc.id)
        if not bookmarks and cached is not None:
            bookmarks = cached.bookmarks
        if not bookmarks:
            if suffix == ".pdf" and path.exists():
                bookmarks = read_pdf_bookmarks(path)
            else:
                converted = settings.tocmemex_data_dir / "converted" / f"{doc.id}.pdf"
                if converted.exists():
                    bookmarks = read_pdf_bookmarks(converted)
        return ParsedDocument(
            pages=pages, bookmarks=bookmarks, metadata={"reindex": True}
        )

    if suffix in {".md", ".markdown", ".txt"}:
        return parse_markdown(path)
    if suffix in {".xlsx", ".xls"}:
        return parse_xlsx(path)
    raise ValueError(f"unsupported file type: {suffix}")


async def _clear_derived_rows(db: AsyncSession, doc_id: str) -> None:
    for statement in (
        "DELETE FROM chunk_fts WHERE doc_id = :doc_id",
        "DELETE FROM doc_fts WHERE doc_id = :doc_id",
        "DELETE FROM resources WHERE doc_id = :doc_id",
        "DELETE FROM chunks WHERE doc_id = :doc_id",
        "DELETE FROM toc_nodes WHERE doc_id = :doc_id",
    ):
        await db.execute(text(statement), {"doc_id": doc_id})


async def reindex_document(doc_id: str, settings: Settings | None = None) -> str:
    """Rebuild TOC/chunks/resources/FTS from cached parse output.

    The build uses the same LLM configuration an ingest would (DB default
    endpoint, else .env), and appends a ``reindex`` provenance row.
    """
    settings = settings or get_settings()
    llm_settings, llm_endpoint_name = await _resolve_llm_settings(settings)
    llm_settings = await _attach_gangmu_lessons(get_engine(), doc_id, llm_settings)
    Session = session_factory(get_engine())
    async with Session() as db:
        doc = await db.get(Document, doc_id)
        if doc is None:
            raise ValueError(f"document not found: {doc_id}")

        parsed = load_parsed(doc, settings)
        save_bookmarks(settings, doc_id, parsed.bookmarks)

        await _clear_derived_rows(db, doc_id)
        doc.page_count = parsed.page_count
        doc.char_count = sum(len(page_text(page)) for page in parsed.pages)
        doc.status = "indexing"
        doc.stage = "整理结构"
        doc.progress = 40
        doc.error = None

        toc_trace: dict = {}
        plain_text = "纯文本" in (doc.parse_plan or "")
        if plain_text:
            toc = await persist_plain_text(db, doc, parsed, settings)
        else:
            toc = await persist_parsed(
                db, doc, parsed, llm_settings, toc_trace=toc_trace
            )
        if toc_trace.get("llm_used"):
            toc_trace["llm_endpoint_name"] = llm_endpoint_name
        doc.status = "ready"
        doc.stage = "完成"
        doc.progress = 100
        doc.updated_at = utcnow_iso()
        provenance = _reindex_parse_provenance(settings, doc_id)
        if plain_text and provenance.get("parse_channel") == "ocr":
            provenance["parse_channel"] = "ocr-text"
        await record_run(
            db,
            build_run(
                doc,
                recorded_by="reindex",
                parse=provenance,
                toc_trace=toc_trace,
                nodes=toc,
            ),
        )
        await db.commit()
        return doc_id


def _reindex_parse_provenance(settings: Settings, doc_id: str) -> dict:
    """How the cached pages were produced, as far as the cache can prove.

    Reindex never calls OCR, so ``ocr_used`` describes the *original* parse
    that produced the cache: ``pages.json`` records its provider/model;
    a legacy Paddle ``raw.jsonl`` proves Paddle ran but not its model.
    """
    cache_dir = settings.ocr_dir / doc_id
    cached = load_pages(cache_dir)
    if cached is not None:
        meta = cached.metadata or {}
        return {
            "parse_channel": "ocr",
            "ocr_used": True,
            "ocr_provider": meta.get("ocr_provider"),
            "ocr_model": meta.get("ocr_model"),
            "ocr_job_id": meta.get("job_id"),
        }
    if (cache_dir / "raw.jsonl").exists():
        return {
            "parse_channel": "ocr",
            "ocr_used": True,
            "ocr_provider": "paddle",
            "ocr_model": None,
            "ocr_job_id": None,
        }
    return {"parse_channel": "pdf-text", "ocr_used": False}


async def reindex_all(settings: Settings | None = None) -> list[str]:
    settings = settings or get_settings()
    Session = session_factory(get_engine())
    async with Session() as db:
        ids = (await db.execute(select(Document.id))).scalars().all()
    return [await reindex_document(doc_id, settings) for doc_id in ids]


async def backfill_document_images(
    doc_id: str, settings: Settings | None = None
) -> dict:
    """Download missing figure images for one document without reindexing."""
    settings = settings or get_settings()
    ocr_settings = await _resolve_ocr_settings(settings)
    Session = session_factory(get_engine())
    async with Session() as db:
        doc = await db.get(Document, doc_id)
        if doc is None:
            raise ValueError(f"document not found: {doc_id}")
        parsed = load_parsed(doc, settings)
        result = await backfill_images(
            db, doc, parsed, settings, ocr_settings=ocr_settings
        )
        await db.commit()
        return result


async def backfill_all_images(settings: Settings | None = None) -> list[dict]:
    settings = settings or get_settings()
    Session = session_factory(get_engine())
    async with Session() as db:
        ids = (await db.execute(select(Document.id))).scalars().all()
    return [await backfill_document_images(doc_id, settings) for doc_id in ids]