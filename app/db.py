from __future__ import annotations

from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base

# Same coarse values ingest writes. Kept local to avoid importing the
# pipeline (OCR/httpx stack) just to run a startup backfill.
_DOC_TYPE_BY_SUFFIX = {
    ".pdf": "pdf",
    ".doc": "doc",
    ".docx": "doc",
    ".md": "md",
    ".markdown": "md",
    ".txt": "txt",
    ".xlsx": "xlsx",
    ".xls": "xlsx",
    ".csv": "csv",
    ".tsv": "csv",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".bmp": "image",
    ".tif": "image",
    ".tiff": "image",
    ".gif": "image",
}

FTS_DDL = (
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
        title, heading, body,
        doc_id UNINDEXED, node_id UNINDEXED, chunk_id UNINDEXED,
        tokenize = 'unicode61 remove_diacritics 2'
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts USING fts5(
        title, summary, keywords,
        doc_id UNINDEXED,
        tokenize = 'unicode61 remove_diacritics 2'
    )
    """,
)


def make_engine(db_path: Path) -> AsyncEngine:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path.as_posix()}", future=True
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


async def init_db(engine: AsyncEngine) -> None:
    # The activity-layer models (tasks / activities / errata / usage_log /
    # lessons / scores) live in app.index.activity and register themselves on
    # the shared Base only when that module is imported. Import it here so a
    # fresh database always gets those tables, whichever entry point created
    # it (CLI init, web_api startup, MCP ensure_schema).
    from .index import activity as _activity  # noqa: F401
    # Console run/step tables; imported here so every entry point that calls
    # init_db (CLI, web_api, MCP) creates them on a fresh database.
    from .agent import models as _agent_models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for ddl in FTS_DDL:
            await conn.execute(text(ddl))
        await _patch_document_collection(conn)
        await _patch_collection_name_key(conn)
        await _backfill_doc_types(conn)
        await _patch_legacy_columns(conn)


async def _patch_legacy_columns(conn) -> None:  # noqa: ANN001
    """Add columns that postdate a database created by older code.

    This lives in ``init_db`` rather than only ``ensure_schema`` because the
    CLI, the curator and the eval scripts all call ``init_db`` directly: an
    old database opened by ``python -m app.curator`` must come out of the
    first command already migrated, not fail with "no such column" on the
    first ORM query. Every patch is idempotent, so running on every start is
    cheap, and ``_add_column_if_missing`` absorbs the concurrent-start race.
    """
    # agent_notes: replies, issue links, sessions, task timeline
    rows = await conn.execute(text("PRAGMA table_info(agent_notes)"))
    names = {row[1] for row in rows.fetchall()}
    if names:
        await _add_column_if_missing(conn, "agent_notes", "reply_to", "INTEGER")
        if await _add_column_if_missing(conn, "agent_notes", "issue_id", "VARCHAR(120)"):
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_agent_notes_issue_id "
                    "ON agent_notes (issue_id)"
                )
            )
        if await _add_column_if_missing(conn, "agent_notes", "session_id", "VARCHAR(64)"):
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_agent_notes_session_id "
                    "ON agent_notes (session_id)"
                )
            )
        if await _add_column_if_missing(conn, "agent_notes", "task_id", "VARCHAR(36)"):
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_agent_notes_task_id "
                    "ON agent_notes (task_id)"
                )
            )

    # documents: live ingest progress (stage / percent / parse scheme) shown
    # by the 解析队列 page. Written before parsing starts so the queue can
    # list a document while OCR is still running.
    for column, ddl in (
        ("stage", "VARCHAR(40)"),
        ("progress", "INTEGER"),
        ("parse_plan", "VARCHAR(80)"),
    ):
        await _add_column_if_missing(conn, "documents", column, ddl)

    # errata scope/supersedes columns (legal supersede + retraction)
    rows = await conn.execute(text("PRAGMA table_info(errata)"))
    names = {row[1] for row in rows.fetchall()}
    if names:
        await _add_column_if_missing(
            conn, "errata", "scope", "VARCHAR(20) NOT NULL DEFAULT 'error'"
        )
        await _add_column_if_missing(conn, "errata", "supersedes_doc_id", "VARCHAR(36)")
        await _add_column_if_missing(conn, "errata", "supersedes_node_id", "VARCHAR(120)")

    # per-endpoint reasoning effort (optional; NULL omits the field)
    await _add_column_if_missing(
        conn, "model_endpoints", "reasoning_effort", "VARCHAR(20)"
    )

    # bench scenario OCR dimension + per-run OCR columns
    await _add_column_if_missing(conn, "toc_bench_scenarios", "ocr_endpoint_ids", "TEXT")
    for column, ddl in (
        ("ocr_endpoint_id", "VARCHAR(36)"),
        ("ocr_label", "VARCHAR(120)"),
        ("ocr_status", "VARCHAR(10)"),
        ("ocr_elapsed_ms", "INTEGER"),
        ("ocr_error", "TEXT"),
    ):
        await _add_column_if_missing(conn, "toc_bench_runs", column, ddl)

    # ADR-0002 §8.2: the curator scores every document by usage heat each
    # run, which filters on (doc_id, node_id, used). Three single-column
    # indexes cannot answer that in one seek; this composite can.
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_usage_log_doc_node_used "
            "ON usage_log (doc_id, node_id, used)"
        )
    )

    # agent_runs: runs started outside the console. Old rows predate the
    # column set and belong to the built-in loop, hence the "console" default.
    await _add_column_if_missing(
        conn, "agent_runs", "source", "VARCHAR(20) NOT NULL DEFAULT 'console'"
    )
    await _add_column_if_missing(conn, "agent_runs", "session_id", "VARCHAR(80)")
    await _add_column_if_missing(conn, "agent_runs", "client", "VARCHAR(120)")
    await _add_column_if_missing(conn, "agent_runs", "last_seen_at", "VARCHAR(40)")
    # Per-run token ceiling; 0 = the built-in default in app.agent.runner.
    await _add_column_if_missing(conn, "agent_runs", "max_tokens", "INTEGER NOT NULL DEFAULT 0")
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_agent_runs_source "
            "ON agent_runs (source)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_agent_runs_session_id "
            "ON agent_runs (session_id)"
        )
    )

    # lessons.support_count / updated_at: consolidation state for
    # lesson dedup (ADR-0002 §11.3).
    await _add_column_if_missing(
        conn, "lessons", "support_count", "INTEGER NOT NULL DEFAULT 1"
    )
    if await _add_column_if_missing(conn, "lessons", "updated_at", "VARCHAR(40)"):
        await conn.execute(
            text("UPDATE lessons SET updated_at = created_at WHERE updated_at IS NULL")
        )


async def _add_column_if_missing(conn, table: str, column: str, ddl: str) -> bool:  # noqa: ANN001
    """Add a column only if the table exists and lacks it.

    Two processes can start at the same instant during a migration window
    and both see the column missing; the loser's ALTER then raises
    "duplicate column name". Treat that as success rather than failing
    startup.
    """
    rows = await conn.execute(text(f"PRAGMA table_info({table})"))
    names = {row[1] for row in rows.fetchall()}
    if not names or column in names:
        return False
    try:
        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
    except Exception as exc:  # duplicate column from a concurrent migrator
        if "duplicate column name" not in str(exc).lower():
            raise
        return False
    return True


async def _patch_document_collection(conn) -> None:  # noqa: ANN001
    """Add documents.collection_id to databases that predate collections.

    This lives in ``init_db`` rather than only ``ensure_schema`` because
    the CLI and the eval/smoke scripts call ``init_db`` directly. The
    column is selected by every ORM query on Document, so without this a
    pre-collections database fails with "no such column" on any command.

    The index is created unconditionally: a run that died between the
    ALTER and the CREATE INDEX must heal on the next start.
    """
    await _add_column_if_missing(conn, "documents", "collection_id", "VARCHAR(36)")
    rows = await conn.execute(text("PRAGMA table_info(documents)"))
    if not {row[1] for row in rows.fetchall()}:
        return  # documents table does not exist yet; create_all made it
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_documents_collection_id "
            "ON documents (collection_id)"
        )
    )


async def _patch_collection_name_key(conn) -> None:  # noqa: ANN001
    """Add collections.name_key (casefolded, UNIQUE) to older databases.

    Uniqueness used to be UNIQUE(name) (BINARY) plus a lower() pre-check,
    both ASCII-only. The key column is what enforces case-insensitive
    uniqueness for any script, including "Ärger"/"ärger".

    Backfill is computed in Python because SQLite's lower() cannot casefold.
    Existing duplicates would make the UNIQUE index fail, so the column is
    added first and indexed only once it is duplicate-free.
    """
    rows = await conn.execute(text("PRAGMA table_info(collections)"))
    if not {row[1] for row in rows.fetchall()}:
        return  # collections table does not exist yet; create_all made it
    await _add_column_if_missing(conn, "collections", "name_key", "VARCHAR(80)")

    existing = (
        await conn.execute(
            text("SELECT id, name FROM collections WHERE name_key IS NULL OR name_key = ''")
        )
    ).all()
    for collection_id, name in existing:
        await conn.execute(
            text("UPDATE collections SET name_key = :key WHERE id = :id"),
            {"key": (name or "").strip().casefold(), "id": collection_id},
        )

    duplicates = (
        await conn.execute(
            text(
                "SELECT name_key FROM collections "
                "GROUP BY name_key HAVING COUNT(*) > 1"
            )
        )
    ).all()
    if duplicates:
        # A database written by the old code can hold "Job"/"job". Leave the
        # index off rather than failing every startup; the pre-check still
        # refuses new duplicates and the user can rename one.
        return
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_collections_name_key "
            "ON collections (name_key)"
        )
    )


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def _backfill_doc_types(conn) -> int:  # noqa: ANN001
    """Fill ``documents.doc_type`` from the filename for older rows.

    Every document ingested before the ingest path inferred a type has
    ``doc_type = NULL``, so ``list_documents(doc_type=...)`` matched nothing
    and the overview reported 74 documents under "unknown". Only NULL/empty
    values are touched, so an explicitly set type is never overwritten.
    """
    rows = await conn.execute(
        text("SELECT id, filename FROM documents WHERE doc_type IS NULL OR doc_type = ''")
    )
    updated = 0
    for doc_id, filename in rows.all():
        doc_type = _DOC_TYPE_BY_SUFFIX.get(Path(filename or "").suffix.lower())
        if doc_type is None:
            continue
        await conn.execute(
            text("UPDATE documents SET doc_type = :doc_type WHERE id = :id"),
            {"doc_type": doc_type, "id": doc_id},
        )
        updated += 1
    return updated


async def ensure_schema(engine: AsyncEngine) -> None:
    """Create missing tables and patch columns onto tables that predate them.

    Every column patch now lives in ``init_db`` (``_patch_legacy_columns``),
    so the CLI and the curator get the same migration on first command. This
    wrapper remains for the web_api / MCP entry points that call it by name.
    """
    await init_db(engine)