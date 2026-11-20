"""TOC bench runner: documents × OCR choices × LLM endpoints.

A scenario compares TOC builds across two independent axes:

- **OCR axis** — ``cached`` (the parse output already on disk), ``text``
  (the PDF text layer, no OCR), or an ``ocr_endpoints`` row id (that
  provider is actually called). Each document is parsed once per OCR
  choice, into an isolated ``data/bench/ocr/<scenario>/<doc>/`` directory,
  so a bench OCR run never overwrites the document's real parse cache.
- **LLM axis** — zero or more ``model_endpoints`` rows. For every
  (document, OCR choice, endpoint) the rule TOC and that endpoint's LLM
  TOC are built and scored side by side. The rule baseline is one run per
  (document, OCR choice).

A scenario is a bypass comparison — it reads documents and parse output,
builds TOCs, scores them, and writes only the bench tables. It never
touches documents / toc_nodes / chunks / FTS, and it never reads or writes
the ingest-time LLM cache (``data/llm``): every bench LLM run must actually
exercise its endpoint.

The LLM side calls ``build_llm_toc_proposal`` directly — not ``build_toc``,
which swallows LLM failures for production safety. Here a dead endpoint
raises and is recorded as an error run. The blocking (sync) build work runs
in a thread so the event loop keeps serving; an ``asyncio.Semaphore(1)``
keeps concurrent scenarios serial.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ..config import Settings
from ..models import (
    Document,
    ModelEndpoint,
    OcrEndpoint,
    TocBenchRun,
    TocBenchScenario,
    utcnow_iso,
)
from ..ocr.models import ParsedDocument
from .llm_toc import build_llm_toc_proposal
from .reindex import load_parsed
from .toc import TocNodeData, build_toc, score_nodes

rebuild_parsed = load_parsed  # indirection so tests can monkeypatch the loader

_scenario_lock = asyncio.Semaphore(1)

# Pseudo OCR choices (stored in ocr_endpoint_ids alongside real endpoint ids).
OCR_CACHED = "cached"
OCR_TEXT = "text"


def _endpoint_settings(settings: Settings, row: ModelEndpoint) -> Settings:
    """A Settings that routes LLM calls at this endpoint. The endpoint's
    explicit reasoning_effort wins; empty falls back to the .env value."""
    return Settings(
        tocmemex_llm_api_key=row.api_key,
        tocmemex_llm_base_url=row.base_url,
        tocmemex_llm_model=row.model,
        tocmemex_llm_temperature=settings.tocmemex_llm_temperature,
        tocmemex_llm_reasoning_effort=row.reasoning_effort
        or settings.tocmemex_llm_reasoning_effort,
    )


def _no_key_settings(settings: Settings) -> Settings:
    """A Settings with LLM disabled: build_toc then runs pure rule path."""
    return Settings(
        tocmemex_llm_api_key="",
        tocmemex_llm_base_url="",
        tocmemex_llm_model="",
        tocmemex_llm_temperature=settings.tocmemex_llm_temperature,
    )


def _sync_build(
    doc_id: str,
    parsed,
    rule_settings: Settings,
    llm_settings: Settings | None,
) -> tuple[list[TocNodeData], int, list[TocNodeData] | None, str | None, int]:
    """One document's rule TOC and (optionally) one endpoint's LLM TOC.

    Returns (rule_nodes, rule_elapsed, llm_nodes | None, llm_error | None,
    llm_elapsed). Runs in a worker thread; all calls inside are synchronous.
    """
    started = time.monotonic()
    rule_nodes = build_toc(doc_id, parsed, rule_settings)
    rule_elapsed = int((time.monotonic() - started) * 1000)

    llm_nodes: list[TocNodeData] | None = None
    llm_error: str | None = None
    llm_elapsed = 0
    if llm_settings is not None:
        started = time.monotonic()
        try:
            llm_nodes = build_llm_toc_proposal(doc_id, parsed, llm_settings)
        except Exception as exc:
            llm_error = str(exc)
        llm_elapsed = int((time.monotonic() - started) * 1000)
    return rule_nodes, rule_elapsed, llm_nodes, llm_error, llm_elapsed


async def _parse_with(
    doc: Document,
    choice: str,
    settings: Settings,
    ocr_row: OcrEndpoint | None,
    out_dir: Path,
) -> ParsedDocument:
    """Parse one document through one OCR choice.

    ``cached`` reuses the document's real parse output; ``text`` reads the
    PDF text layer; an endpoint id calls that provider into ``out_dir``.
    """
    if choice == OCR_CACHED:
        return rebuild_parsed(doc, settings)
    if choice == OCR_TEXT:
        if Path(doc.file_path).suffix.lower() != ".pdf":
            raise ValueError("文本层对比仅适用于 PDF")
        from .pdf_text import parse_pdf_text

        # parse_pdf_text returns None when the PDF has no usable text layer;
        # for a text-layer comparison that is a real, reportable outcome.
        parsed = parse_pdf_text(Path(doc.file_path))
        if parsed is None:
            raise ValueError("该 PDF 没有可用文本层（需要 OCR）")
        if not parsed.bookmarks:
            from .pipeline import read_pdf_bookmarks

            parsed.bookmarks = read_pdf_bookmarks(Path(doc.file_path))
        return parsed
    if ocr_row is None:
        raise ValueError(f"OCR endpoint not found: {choice}")

    from ..ocr.endpoints import _endpoint_settings as ocr_endpoint_settings
    from ..ocr.providers import build_ocr_provider

    provider = build_ocr_provider(ocr_endpoint_settings(settings, ocr_row))
    return await provider.parse_pdf(Path(doc.file_path), out_dir)


def _ocr_label(choice: str, ocr_row: OcrEndpoint | None) -> str:
    if choice == OCR_CACHED:
        return "已有解析缓存"
    if choice == OCR_TEXT:
        return "PDF 文本层"
    if ocr_row is not None:
        label = ocr_row.name or ocr_row.provider
        if ocr_row.model:
            label = f"{label} · {ocr_row.model}"
        return label
    return choice


async def _add_run(
    db: AsyncSession,
    *,
    scenario_id: str,
    doc_id: str,
    endpoint: ModelEndpoint | None,
    side: str,
    status: str,
    error: str | None,
    nodes: list[TocNodeData],
    page_count: int | None,
    elapsed_ms: int,
    ocr_choice: str,
    ocr_label: str,
    ocr_status: str,
    ocr_elapsed_ms: int | None,
    ocr_error: str | None,
) -> None:
    score = score_nodes(nodes, page_count) if status == "ok" else None
    db.add(
        TocBenchRun(
            scenario_id=scenario_id,
            doc_id=doc_id,
            endpoint_id=endpoint.id if endpoint else None,
            side=side,
            status=status,
            error=error,
            node_count=len(nodes),
            validate_errors=score[0] * -1 if score else None,
            validate_warnings=score[1] * -1 if score else None,
            coverage=score[3] if score else None,
            score_json=json.dumps(list(score)) if score else None,
            elapsed_ms=elapsed_ms,
            endpoint_name=endpoint.name if endpoint else None,
            base_url=endpoint.base_url if endpoint else None,
            model=endpoint.model if endpoint else None,
            ocr_endpoint_id=ocr_choice,
            ocr_label=ocr_label,
            ocr_status=ocr_status,
            ocr_elapsed_ms=ocr_elapsed_ms,
            ocr_error=ocr_error,
        )
    )


async def run_scenario(
    scenario_id: str,
    doc_ids: list[str],
    endpoint_ids: list[str],
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    engine: AsyncEngine | None = None,
    ocr_endpoint_ids: list[str] | None = None,
) -> None:
    """Execute a scenario to completion; never raises (status records it).

    ``session_factory`` / ``engine`` are injectable for tests; production
    callers omit both and the module-level engine is used. Scenarios are
    serialized by a module-level semaphore. ``ocr_endpoint_ids`` defaults
    to ``["cached"]`` (the pre-OCR-dimension behaviour).
    """
    if engine is not None:
        from ..db import session_factory as _default_factory

        session_factory = _default_factory(engine)

    ocr_choices = list(ocr_endpoint_ids) if ocr_endpoint_ids else [OCR_CACHED]
    async with _scenario_lock:
        await _run_scenario_locked(
            scenario_id, doc_ids, endpoint_ids, settings, session_factory, ocr_choices
        )


async def _run_scenario_locked(
    scenario_id: str,
    doc_ids: list[str],
    endpoint_ids: list[str],
    settings: Settings,
    session_factory,
    ocr_choices: list[str],
) -> None:
    try:
        async with session_factory() as db:
            scenario = await db.get(TocBenchScenario, scenario_id)
            if scenario is None:
                scenario = TocBenchScenario(
                    id=scenario_id,
                    doc_ids=json.dumps(doc_ids),
                    endpoint_ids=json.dumps(endpoint_ids),
                    ocr_endpoint_ids=json.dumps(ocr_choices),
                )
                db.add(scenario)
                await db.commit()

            docs: dict[str, Document] = {}
            for doc_id in doc_ids:
                doc = await db.get(Document, doc_id)
                if doc is None:
                    raise ValueError(f"document not found: {doc_id}")
                docs[doc_id] = doc

            endpoints: dict[str, ModelEndpoint] = {}
            for endpoint_id in endpoint_ids:
                row = await db.get(ModelEndpoint, endpoint_id)
                if row is None:
                    raise ValueError(f"endpoint not found: {endpoint_id}")
                endpoints[endpoint_id] = row

            ocr_rows: dict[str, OcrEndpoint | None] = {}
            for choice in ocr_choices:
                if choice in {OCR_CACHED, OCR_TEXT}:
                    ocr_rows[choice] = None
                    continue
                row = await db.get(OcrEndpoint, choice)
                if row is None:
                    raise ValueError(f"OCR endpoint not found: {choice}")
                ocr_rows[choice] = row

            rule_settings = _no_key_settings(settings)

            for ocr_choice in ocr_choices:
                ocr_row = ocr_rows[ocr_choice]
                ocr_label = _ocr_label(ocr_choice, ocr_row)
                for doc_id, doc in docs.items():
                    # Parse once per (document, OCR choice); every endpoint
                    # then builds from the same pages.
                    out_dir = (
                        settings.tocmemex_data_dir
                        / "bench"
                        / "ocr"
                        / scenario_id
                        / doc_id
                        / ocr_choice
                    )
                    started = time.monotonic()
                    ocr_error: str | None = None
                    ocr_status = "ok"
                    parsed: ParsedDocument | None = None
                    try:
                        parsed = await _parse_with(
                            doc, ocr_choice, settings, ocr_row, out_dir
                        )
                    except Exception as exc:
                        ocr_status = "error"
                        ocr_error = str(exc) or type(exc).__name__
                    ocr_elapsed = int((time.monotonic() - started) * 1000)

                    if parsed is None:
                        # The OCR choice failed: record it against every
                        # endpoint so the failure is visible per cell, and
                        # still record the rule side.
                        for endpoint in endpoints.values():
                            await _add_run(
                                db,
                                scenario_id=scenario_id,
                                doc_id=doc_id,
                                endpoint=endpoint,
                                side="llm",
                                status="error",
                                error=f"OCR 失败：{ocr_error}",
                                nodes=[],
                                page_count=None,
                                elapsed_ms=0,
                                ocr_choice=ocr_choice,
                                ocr_label=ocr_label,
                                ocr_status=ocr_status,
                                ocr_elapsed_ms=ocr_elapsed,
                                ocr_error=ocr_error,
                            )
                        await _add_run(
                            db,
                            scenario_id=scenario_id,
                            doc_id=doc_id,
                            endpoint=None,
                            side="rule",
                            status="error",
                            error=f"OCR 失败：{ocr_error}",
                            nodes=[],
                            page_count=None,
                            elapsed_ms=0,
                            ocr_choice=ocr_choice,
                            ocr_label=ocr_label,
                            ocr_status=ocr_status,
                            ocr_elapsed_ms=ocr_elapsed,
                            ocr_error=ocr_error,
                        )
                        await db.commit()
                        continue

                    for endpoint in endpoints.values():
                        llm_settings = _endpoint_settings(settings, endpoint)
                        (
                            _rule_nodes,
                            _rule_elapsed,
                            llm_nodes,
                            llm_error,
                            llm_elapsed,
                        ) = await asyncio.to_thread(
                            _sync_build, doc_id, parsed, rule_settings, llm_settings
                        )
                        await _add_run(
                            db,
                            scenario_id=scenario_id,
                            doc_id=doc_id,
                            endpoint=endpoint,
                            side="llm",
                            status="error" if llm_error is not None else "ok",
                            error=llm_error,
                            nodes=llm_nodes or [],
                            page_count=parsed.page_count,
                            elapsed_ms=llm_elapsed,
                            ocr_choice=ocr_choice,
                            ocr_label=ocr_label,
                            ocr_status=ocr_status,
                            ocr_elapsed_ms=ocr_elapsed,
                            ocr_error=ocr_error,
                        )
                        await db.commit()

                    # one rule baseline per (document, OCR choice)
                    (
                        rule_nodes,
                        rule_elapsed,
                        _,
                        _,
                        _,
                    ) = await asyncio.to_thread(
                        _sync_build, doc_id, parsed, rule_settings, None
                    )
                    await _add_run(
                        db,
                        scenario_id=scenario_id,
                        doc_id=doc_id,
                        endpoint=None,
                        side="rule",
                        status="ok",
                        error=None,
                        nodes=rule_nodes,
                        page_count=parsed.page_count,
                        elapsed_ms=rule_elapsed,
                        ocr_choice=ocr_choice,
                        ocr_label=ocr_label,
                        ocr_status=ocr_status,
                        ocr_elapsed_ms=ocr_elapsed,
                        ocr_error=ocr_error,
                    )
                    await db.commit()

            scenario = await db.get(TocBenchScenario, scenario_id)
            if scenario is not None:
                scenario.status = "done"
                scenario.finished_at = utcnow_iso()
            await db.commit()
    except Exception as exc:
        async with session_factory() as db:
            scenario = await db.get(TocBenchScenario, scenario_id)
            if scenario is not None:
                scenario.status = "failed"
                scenario.error = str(exc)
                scenario.finished_at = utcnow_iso()
                await db.commit()


async def fail_stale_processing_scenarios(session_factory) -> int:
    """Mark scenarios stuck in ``processing`` (e.g. a killed process) failed.

    Called at startup: nothing else will ever finish them, and the frontend
    polls processing scenarios forever otherwise. Returns the count marked.
    """
    async with session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(TocBenchScenario).where(TocBenchScenario.status == "processing")
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.status = "failed"
            row.error = row.error or "interrupted: server restarted mid-run"
            row.finished_at = utcnow_iso()
        await db.commit()
        return len(rows)
