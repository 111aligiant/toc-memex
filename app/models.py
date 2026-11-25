from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Float,
    ForeignKey,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, validates


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Namespace(Base):
    __tablename__ = "namespaces"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="shared")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utcnow_iso)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    namespace_id: Mapped[str] = mapped_column(
        ForeignKey("namespaces.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    collection_id: Mapped[str | None] = mapped_column(
        ForeignKey("collections.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    doc_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    doc_date: Mapped[str | None] = mapped_column(String(20), nullable=True)
    source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    keywords: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Live ingest state for the 解析队列. The row is written before parsing
    # starts, so these move while OCR runs; a ready document keeps the last
    # values for the record.
    stage: Mapped[str | None] = mapped_column(String(40), nullable=True)
    progress: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parse_plan: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(
        String(40), nullable=False, default=utcnow_iso, onupdate=utcnow_iso
    )

    __table_args__ = (
        UniqueConstraint("namespace_id", "content_hash", name="uq_doc_ns_hash"),
    )


class TocNode(Base):
    __tablename__ = "toc_nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_id: Mapped[str] = mapped_column(String(120), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    heading_num: Mapped[str | None] = mapped_column(String(50), nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    heading_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="fallback")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.3)

    __table_args__ = (
        UniqueConstraint("doc_id", "node_id", name="uq_toc_doc_node"),
    )


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    heading_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ref_ids: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utcnow_iso)

    __table_args__ = (
        UniqueConstraint("doc_id", "chunk_index", name="uq_chunk_doc_index"),
    )


class Resource(Base):
    __tablename__ = "resources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    resource_id: Mapped[str] = mapped_column(String(80), nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    page_idx: Mapped[int | None] = mapped_column(Integer, nullable=True)
    node_id: Mapped[str | None] = mapped_column(String(120), nullable=True)

    __table_args__ = (
        UniqueConstraint("doc_id", "resource_id", name="uq_resource_doc_id"),
    )


class AgentSetting(Base):
    """One JSON row of runtime settings per agent (纲目 / 编修 / global).

    Rows store only what the user changed; reads merge over the shipped
    defaults in ``app.llm.purpose``. Keeping this as JSON means new knobs do
    not need a schema migration, and an old row never hides a new default.
    """

    __tablename__ = "agent_settings"

    name: Mapped[str] = mapped_column(String(20), primary_key=True)
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[str] = mapped_column(
        String(40), nullable=False, default=utcnow_iso, onupdate=utcnow_iso
    )


class ModelEndpoint(Base):
    """An OpenAI-compatible LLM endpoint configured at runtime.

    API keys are stored server-side only; serialization goes through
    ``app.llm.endpoints.endpoint_out`` which never returns the plaintext.
    """

    __tablename__ = "model_endpoints"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    api_key: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    # Optional OpenAI reasoning_effort for this endpoint; NULL/empty omits
    # the field from completions (vendor default applies).
    reasoning_effort: Mapped[str | None] = mapped_column(String(20), nullable=True)
    enabled: Mapped[bool] = mapped_column(Integer, nullable=False, default=1)
    is_default: Mapped[bool] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(
        String(40), nullable=False, default=utcnow_iso, onupdate=utcnow_iso
    )


class OcrEndpoint(Base):
    """An OCR provider endpoint configured at runtime (mirrors ModelEndpoint).

    Which fields matter depends on ``provider``:

    - paddle: ``api_key`` = PADDLEOCR_API_TOKEN; ``base_url``/``model`` are
      the jobs endpoint / model name;
    - mistral: ``api_key`` = MISTRAL_API_KEY; ``model`` = mistral-ocr-latest;
      ``base_url`` is the /v1/ocr URL;
    - mineru: ``api_key`` = MINERU_API_TOKEN; ``base_url`` = API root;
    - azure: ``api_key`` = AZURE_DI_KEY; ``base_url`` = endpoint; ``model``
      = prebuilt-layout.

    Keys stay server-side; serialization goes through
    ``app.ocr.endpoints.ocr_endpoint_out`` which never returns the plaintext.
    """

    __tablename__ = "ocr_endpoints"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    api_key: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    model: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Integer, nullable=False, default=1)
    is_default: Mapped[bool] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(
        String(40), nullable=False, default=utcnow_iso, onupdate=utcnow_iso
    )


class IngestRun(Base):
    """One provenance record per ingest / reindex of a document.

    Answers, for every document, what it was built from and by which
    components: the original file, whether OCR ran (and which provider /
    model / endpoint), whether an LLM TOC proposal ran (and which endpoint,
    and whether it won the arbitration), and which channel produced the
    stored TOC. Rows are append-only history; the newest row is current.

    ``recorded_by`` is ``ingest`` / ``reindex`` for live runs and
    ``backfill`` for rows reconstructed from the filesystem for documents
    ingested before this table existed. Unknown values stay NULL — a
    backfilled row never invents a provider or model it cannot prove.
    """

    __tablename__ = "ingest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recorded_by: Mapped[str] = mapped_column(String(20), nullable=False, default="ingest")

    # ── original file ──
    file_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_exists: Mapped[bool] = mapped_column(Integer, nullable=False, default=1)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ── parse channel / OCR ──
    parse_channel: Mapped[str | None] = mapped_column(String(30), nullable=True)
    ocr_used: Mapped[bool] = mapped_column(Integer, nullable=False, default=0)
    ocr_provider: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ocr_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    ocr_endpoint_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    ocr_job_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    ocr_elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ocr_cache: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # ── LLM TOC proposal ──
    llm_used: Mapped[bool] = mapped_column(Integer, nullable=False, default=0)
    llm_endpoint_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # accepted | rejected | no_proposal | error | not_needed | unavailable
    llm_outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)
    llm_elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    llm_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── result ──
    toc_source: Mapped[str | None] = mapped_column(String(30), nullable=True)
    node_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Bookmarks the parser offered the TOC builder. A larger number than
    # node_count means entries were dropped on the way into the tree — the
    # quality page shows both so a silently lost section is visible.
    bookmark_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # JSON list of 0-based pages whose text layer decoded to mojibake and
    # whose blocks were therefore not indexed. ``force_ocr`` re-reads them.
    garbled_pages: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ok")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utcnow_iso)


class TocBenchScenario(Base):
    """One bench submission: documents × OCR choices × LLM endpoints.

    ``ocr_endpoint_ids`` holds pseudo-ids too: ``cached`` (the parse output
    already on disk) and ``text`` (the PDF text layer). Empty/NULL means
    ``["cached"]`` — the pre-OCR-dimension behaviour.
    """

    __tablename__ = "toc_bench_scenarios"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    doc_ids: Mapped[str] = mapped_column(Text, nullable=False)  # JSON list
    endpoint_ids: Mapped[str] = mapped_column(Text, nullable=False)  # JSON list
    ocr_endpoint_ids: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="processing")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utcnow_iso)
    finished_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class TocBenchRun(Base):
    """One scored TOC build inside a scenario (side = rule | llm).

    Endpoint fields are a snapshot: the endpoint row can be deleted later
    while historical runs stay readable.
    """

    __tablename__ = "toc_bench_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("toc_bench_scenarios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    doc_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    endpoint_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    side: Mapped[str] = mapped_column(String(10), nullable=False)  # rule | llm
    # OCR dimension: cached | text | <ocr_endpoint_id>. Null on old rows.
    ocr_endpoint_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    ocr_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    ocr_status: Mapped[str | None] = mapped_column(String(10), nullable=True)  # ok | error | skipped
    ocr_elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ocr_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="ok")  # ok | error
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    node_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    validate_errors: Mapped[int | None] = mapped_column(Integer, nullable=True)
    validate_warnings: Mapped[int | None] = mapped_column(Integer, nullable=True)
    coverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    elapsed_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    endpoint_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utcnow_iso)


class AgentNote(Base):
    """Hand-off notes agents leave for each other in the shared Toc Memex library.

    A finishes a task using some material, drops a note (what was done,
    what went wrong), and B reads it before continuing. Notes may anchor
    to a document / TOC node / chunk or float free in the common area.
    """

    __tablename__ = "agent_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    author: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="note")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    doc_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    node_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    chunk_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Which activity-layer task this note belongs to. A hand-off note written
    # with task_id is the same unit of work as the read/cite/erratum rows
    # carrying the same task_id, so the task page can show one timeline
    # instead of two disconnected streams. NULL means "not tied to a task".
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # Which quality issue this note answers (stable id from /api/quality).
    # A note anchored to a doc/node/chunk answers a place; issue_id answers
    # a specific complaint about that place, so the overview can tell
    # "someone judged this problem" apart from "someone left a note nearby".
    issue_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    # Who said it, and which connection said it. `author` is self-declared and
    # therefore unverified; `session_id` is assigned by the server per MCP
    # connection, so two notes with the same session came from one client run.
    # Neither proves which model wrote the note — see docs/agent-identity.md.
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    reply_to: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utcnow_iso)


class DocBaseline(Base):
    """A document's derived state as it was *before* the curator first wrote.

    ADR-0002 §4: the baseline is a product promise, not a backup. It is
    created for every document up front, never overwritten, never reclaimed,
    and it travels with the database. ``toc_json`` / ``chunks_json`` are
    gzip-compressed JSON arrays; ``doc_fields`` is plain JSON because it is
    small and worth reading by eye.

    ``UNIQUE(doc_id, content_hash, reason)`` lets a re-ingested document get
    a new baseline row (a version chain) without duplicating the same one.
    """

    __tablename__ = "doc_baselines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # adoption | pre_curator_write | manual
    reason: Mapped[str] = mapped_column(String(30), nullable=False)
    toc_json: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    chunks_json: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    doc_fields: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    node_count: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)
    doc_updated_at: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utcnow_iso)

    __table_args__ = (
        UniqueConstraint(
            "doc_id", "content_hash", "reason", name="uq_baseline_doc_hash_reason"
        ),
    )


class NodeIdAlias(Base):
    """Old ``node_id`` → new ``node_id``, so activity rows keep resolving.

    P0 rewrites positional ids (``doc:0007``) into content-anchored ones
    (``doc:h:3.2.1``). Notes, activities, errata and usage rows written
    against the old id must keep pointing at the same section, so the old
    id is recorded here and every reader resolves through it.
    """

    __tablename__ = "node_id_aliases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    old_id: Mapped[str] = mapped_column(String(120), nullable=False)
    new_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(40), nullable=False, default="anchored")
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utcnow_iso)

    __table_args__ = (
        UniqueConstraint("doc_id", "old_id", name="uq_node_alias_doc_old"),
    )


class CurationSnapshot(Base):
    """The derived rows as they were immediately before one write.

    Kept to the most recent few per document (see ``app.curation.snapshots``),
    so "undo the last thing the curator did" is cheap and does not have to
    re-run reindex.
    """

    __tablename__ = "curation_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    proposal_id: Mapped[str] = mapped_column(String(80), nullable=False)
    toc_json: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    chunks_json: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utcnow_iso)


class CurationOutcome(Base):
    """One ledger row per curation action (ADR-0002 §8.1).

    The strategy-ranking input: what was done, by whom, and whether it made
    things better. Written by the curator for every applied proposal and by
    the restore endpoint for every human rollback.
    """

    __tablename__ = "curation_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    issue_id: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    doc_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    strategy: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    # applied | partial | rejected | auto_rollback | restored_by_human
    # | restored_forced | llm_unavailable
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    # better | worse | neutral
    verdict: Mapped[str] = mapped_column(String(10), nullable=False, default="neutral")
    delta_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Before/after verifier metrics for this write (docs/self-evolution.md §5.1);
    # null for rows written without verification (--no-verify, legacy rows).
    verify_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # pending | survived | regressed — observation-window state (§5.1.2).
    survival: Mapped[str] = mapped_column(String(10), nullable=False, default="pending")
    actor: Mapped[str] = mapped_column(String(80), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utcnow_iso)


class CurationJob(Base):
    """One unit of curator work: fix one issue in one document.

    ``fingerprint`` folds in the inputs the work depends on, so a document
    whose data or curator version changed gets a new job while an unchanged
    one stays settled (ADR-0002 §8.1, §9.2).
    """

    __tablename__ = "curation_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    issue_id: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    doc_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # failed | parsing | unplaced | missing-pages | duplicate-heading
    # | short-chunks | agent
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # queued | running | proposed | applied | rolled_back | rejected
    # | skipped | failed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    next_run_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(
        String(40), nullable=False, default=utcnow_iso, onupdate=utcnow_iso
    )

    __table_args__ = (
        UniqueConstraint("issue_id", "fingerprint", name="uq_job_issue_fingerprint"),
    )


class CurationProposal(Base):
    """A submitted fix program and what happened to each op.

    ``payload_json`` holds ``{"ops": [...]}``; ``results_json`` the per-op
    verdicts (accepted / dropped + reason), so an agent can read why an op
    was refused and the quality page can list what is pending approval.
    """

    __tablename__ = "curation_proposals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("curation_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # agent_toc | agent_chunk | rule_merge | rule_page_ranges
    strategy: Mapped[str] = mapped_column(String(40), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    results_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    score_before: Mapped[str | None] = mapped_column(Text, nullable=True)
    score_after: Mapped[str | None] = mapped_column(Text, nullable=True)
    # pending | accepted | partial | rejected | disputed
    verdict: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    applied_ops: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dropped_ops: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    llm_endpoint: Mapped[str | None] = mapped_column(String(120), nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    llm_elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utcnow_iso)


class ChunkRemap(Base):
    """Old chunk id → the chunk that now contains it (ADR-0002 §6.3).

    ``chunk_id`` is an autoincrement integer, so any rewrite renumbers it.
    Merging keeps the mapping permanent so a note written against a merged
    slice still resolves to the slice that now holds that text.
    """

    __tablename__ = "chunk_remap"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    old_id: Mapped[int] = mapped_column(Integer, nullable=False)
    new_id: Mapped[int] = mapped_column(Integer, nullable=False)
    proposal_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utcnow_iso)

    __table_args__ = (
        UniqueConstraint("doc_id", "old_id", name="uq_chunk_remap_doc_old"),
    )


class CuratorRun(Base):
    """One curator run's liveness row (ADR-0002 §9.1 step 3).

    The file lock is what excludes a second process; this row exists so the
    Web UI and a debugging human can see that a run is in progress, which
    process owns it, and when it last heartbeated.
    """

    __tablename__ = "curator_runs"

    run_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utcnow_iso)


class Collection(Base):
    """A topic area inside the library (项目资料 / 个人资料 …).

    Distinct from ``Namespace``, which answers "whose library is this".
    A collection answers "which topic", and its ``description`` is written
    for agents: what the collection holds and when a request belongs here.
    Documents belong to at most one collection; NULL means unfiled.
    """

    __tablename__ = "collections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    # Casefolded name, unique. SQLite's UNIQUE(name) is BINARY and its
    # lower()/NOCASE only fold ASCII, so "Ärger"/"ärger" would both be
    # accepted. This column is computed in Python and is the authority.
    name_key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(
        String(40), nullable=False, default=utcnow_iso, onupdate=utcnow_iso
    )

    @validates("name")
    def _sync_name_key(self, key: str, value: str) -> str:
        """Keep the uniqueness key in step with the display name.

        SQLite cannot casefold, so the key is computed here; deriving it on
        assignment means a directly constructed Collection(name=...) is
        still valid instead of tripping the NOT NULL constraint.
        """
        self.name_key = (value or "").strip().casefold()
        return value