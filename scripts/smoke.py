"""Real OCR + Word COM smoke test.

Run from the project root with the project .env present:

    PYTHONPATH=. python scripts/smoke.py path/to/sample.pdf path/to/sample.docx

It ingests the two sample files (deduped if already ingested), then prints
BM25 hits for a few queries.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from app.config import get_settings
from app.db import init_db, make_engine, session_factory
from app.index.fts import search_chunks
from app.ingest.pipeline import ingest_path, set_engine

# Pass sample paths explicitly; defaults are placeholders.
SAMPLE_PDF = Path(sys.argv[1] if len(sys.argv) > 1 else "samples/sample.pdf")
SAMPLE_DOCX = Path(sys.argv[2] if len(sys.argv) > 2 else "samples/sample.docx")


async def main() -> None:
    settings = get_settings()
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    try:
        if SAMPLE_PDF.exists():
            doc_id = await ingest_path(
                SAMPLE_PDF, doc_type="document", settings=settings
            )
            print("pdf", doc_id)
        if SAMPLE_DOCX.exists():
            doc_id = await ingest_path(
                SAMPLE_DOCX,
                doc_type="document",
                doc_date="2026-11-03",
                settings=settings,
            )
            print("docx", doc_id)

        async with session_factory(engine)() as db:
            for query in ["概述", "方法", "结论"]:
                hits = await search_chunks(db, query, top_k=3)
                print(query, len(hits), [hit["chunk_id"] for hit in hits])
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())