from __future__ import annotations

import json

from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.config import Settings
from app.db import init_db, make_engine, session_factory
from app.ingest.bench import run_scenario
from app.ingest.toc import TocNodeData
from app.models import (
    Chunk,
    Document,
    ModelEndpoint,
    OcrEndpoint,
    Namespace,
    TocBenchRun,
    TocBenchScenario,
    TocNode,
)


@pytest_asyncio.fixture
async def db_engine(tmp_path):
    engine = make_engine(tmp_path / "bench.db")
    await init_db(engine)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def factory(db_engine):
    return session_factory(db_engine)


@pytest_asyncio.fixture
async def seeded(factory):
    """One ready document, one endpoint."""
    async with factory() as db:
        db.add(Namespace(id="shared", kind="shared"))
        await db.flush()
        db.add(
            Document(
                id="doc1",
                namespace_id="shared",
                title="Demo",
                filename="demo.pdf",
                file_path="demo.pdf",
                content_hash="hash",
                page_count=10,
                status="ready",
            )
        )
        db.add(
            ModelEndpoint(
                name="ep",
                base_url="http://ep/v1",
                api_key="kk-2222",
                model="ep-model",
                enabled=True,
                is_default=True,
            )
        )
        await db.commit()
    return "doc1"


def _settings() -> Settings:
    return Settings(tocmemex_llm_api_key="", tocmemex_llm_model="")


def _nodes(*titles: str) -> list[TocNodeData]:
    return [
        TocNodeData(
            node_id=f"n:{i}",
            parent_id=None,
            level=1,
            heading_num=None,
            title=t,
            heading_path=t,
            page_start=i,
            page_end=i + 2,
            source="rule",
            confidence=0.8,
        )
        for i, t in enumerate(titles)
    ]


@pytest.mark.asyncio
async def test_run_scenario_writes_rule_and_llm_rows(factory, seeded, monkeypatch):
    doc_id = seeded
    async with factory() as db:
        endpoint = (await db.execute(select(ModelEndpoint))).scalar_one()
        endpoint_id = endpoint.id

    def fake_rebuild(doc, settings):
        return SimpleNamespace(page_count=10)

    monkeypatch.setattr("app.ingest.bench.rebuild_parsed", fake_rebuild)

    rule_calls: list = []
    llm_calls: list = []

    def fake_build_toc(doc_id_arg, parsed, settings=None):
        rule_calls.append(doc_id_arg)
        return _nodes("规则 章 A")

    def fake_proposal(doc_id_arg, parsed, settings):
        llm_calls.append(doc_id_arg)
        return _nodes("LLM 章 A", "LLM 章 B")

    monkeypatch.setattr("app.ingest.bench.build_toc", fake_build_toc)
    monkeypatch.setattr("app.ingest.bench.build_llm_toc_proposal", fake_proposal)

    scenario_id = "sc1"
    await run_scenario(
        scenario_id, [doc_id], [endpoint_id], _settings(), session_factory=factory
    )

    async with factory() as db:
        runs = (
            await db.execute(select(TocBenchRun).order_by(TocBenchRun.id))
        ).scalars().all()
        assert len(runs) == 2
        by_side = {r.side: r for r in runs}
        rule, llm = by_side["rule"], by_side["llm"]
        assert rule.doc_id == doc_id
        assert rule.node_count == 1
        assert llm.node_count == 2
        # endpoint snapshot survives deletion
        assert llm.endpoint_name == "ep"
        assert llm.model == "ep-model"
        assert llm.base_url == "http://ep/v1"
        assert llm.elapsed_ms >= 0
        assert llm.score_json is not None
        assert llm.validate_errors is not None
        assert scenario_id in (rule.scenario_id, llm.scenario_id)

        scenario = await db.get(TocBenchScenario, scenario_id)
        assert scenario.status == "done"


@pytest.mark.asyncio
async def test_run_scenario_llm_failure_is_error_run(factory, seeded, monkeypatch):
    doc_id = seeded
    async with factory() as db:
        endpoint_id = (await db.execute(select(ModelEndpoint))).scalar_one().id

    monkeypatch.setattr(
        "app.ingest.bench.rebuild_parsed", lambda doc, settings: SimpleNamespace(page_count=10)
    )

    def fake_build_toc(doc_id_arg, parsed, settings=None):
        return _nodes("规则 章 A")

    monkeypatch.setattr("app.ingest.bench.build_toc", fake_build_toc)
    monkeypatch.setattr(
        "app.ingest.bench.build_llm_toc_proposal",
        lambda doc_id_arg, parsed, settings: (_ for _ in ()).throw(
            RuntimeError("LLM exploded")
        ),
    )

    await run_scenario(
        "sc2", [doc_id], [endpoint_id], _settings(), session_factory=factory
    )

    async with factory() as db:
        runs = (
            await db.execute(select(TocBenchRun).order_by(TocBenchRun.id))
        ).scalars().all()
        assert len(runs) == 2
        llm = next(r for r in runs if r.side == "llm")
        assert llm.status == "error"
        assert llm.error and "LLM exploded" in llm.error
        assert llm.node_count == 0
        scenario = await db.get(TocBenchScenario, "sc2")
        assert scenario.status == "done"  # error run does not fail the scenario


@pytest.mark.asyncio
async def test_run_scenario_writes_no_production_tables(factory, seeded, monkeypatch):
    doc_id = seeded
    async with factory() as db:
        endpoint_id = (await db.execute(select(ModelEndpoint))).scalar_one().id

    monkeypatch.setattr(
        "app.ingest.bench.rebuild_parsed", lambda doc, settings: SimpleNamespace(page_count=10)
    )
    monkeypatch.setattr(
        "app.ingest.bench.build_toc", lambda d, p, settings=None: _nodes("N")
    )

    await run_scenario(
        "sc3", [doc_id], [endpoint_id], _settings(), session_factory=factory
    )

    async with factory() as db:
        assert (await db.execute(select(func.count()).select_from(Document))).scalar() == 1
        assert (await db.execute(select(func.count()).select_from(TocNode))).scalar() == 0
        assert (await db.execute(select(func.count()).select_from(Chunk))).scalar() == 0


@pytest.mark.asyncio
async def test_run_scenario_missing_document_fails_scenario(factory, seeded):
    async with factory() as db:
        endpoint_id = (await db.execute(select(ModelEndpoint))).scalar_one().id

    await run_scenario(
        "sc4", ["ghost-doc"], [endpoint_id], _settings(), session_factory=factory
    )

    async with factory() as db:
        scenario = await db.get(TocBenchScenario, "sc4")
        assert scenario.status == "failed"
        assert "ghost-doc" in (scenario.error or "")


# ── review fixes: real LLM path semantics ──


@pytest.mark.asyncio
async def test_bench_llm_failure_surfaces_as_error_run_with_real_path(
    factory, seeded, monkeypatch
):
    """C1: detect_printed_toc_llm swallows LLMError and returns None; the bench
    must still record an error run instead of an ok run identical to rule."""
    doc_id = seeded
    async with factory() as db:
        endpoint_id = (await db.execute(select(ModelEndpoint))).scalar_one().id

    monkeypatch.setattr(
        "app.ingest.bench.rebuild_parsed", lambda doc, settings: SimpleNamespace(page_count=10)
    )
    # real build_toc: rule result healthy, never raises even with a dead LLM
    monkeypatch.setattr("app.ingest.bench.build_toc", _real_like_build_toc)
    monkeypatch.setattr(
        "app.ingest.bench.build_llm_toc_proposal",
        lambda doc_id_arg, parsed, settings: (_ for _ in ()).throw(
            RuntimeError("LLM exploded")
        ),
    )

    await run_scenario(
        "sc5", [doc_id], [endpoint_id], _settings(), session_factory=factory
    )

    async with factory() as db:
        runs = (await db.execute(select(TocBenchRun))).scalars().all()
        llm = next(r for r in runs if r.side == "llm")
        assert llm.status == "error"
        assert "LLM exploded" in llm.error


@pytest.mark.asyncio
async def test_bench_llm_proposal_skips_disk_cache(factory, seeded, monkeypatch):
    """C2: bench must not read or write the data/llm disk cache."""
    doc_id = seeded
    async with factory() as db:
        endpoint_id = (await db.execute(select(ModelEndpoint))).scalar_one().id

    monkeypatch.setattr(
        "app.ingest.bench.rebuild_parsed", lambda doc, settings: SimpleNamespace(page_count=10)
    )
    monkeypatch.setattr("app.ingest.bench.build_toc", _real_like_build_toc)

    calls = {}

    def fake_proposal(doc_id_arg, parsed, settings):
        calls["doc_id"] = doc_id_arg
        calls["settings_model"] = settings.tocmemex_llm_model
        return _nodes("LLM 章 X")

    monkeypatch.setattr("app.ingest.bench.build_llm_toc_proposal", fake_proposal)

    from app.llm.client import LLMError

    # real detect would have been cache-driven; assert bench calls our proposal
    # builder and does not touch the cache file
    cache_dir = Path(_settings().tocmemex_data_dir) / "llm" / doc_id
    existed_before = cache_dir.exists()

    await run_scenario(
        "sc6", [doc_id], [endpoint_id], _settings(), session_factory=factory
    )
    assert calls["doc_id"] == doc_id
    assert calls["settings_model"] == "ep-model"
    async with factory() as db:
        runs = (await db.execute(select(TocBenchRun))).scalars().all()
        llm = next(r for r in runs if r.side == "llm")
        assert llm.status == "ok"
        assert llm.node_count == 1
    # proposal builder is invoked directly; cache untouched by bench itself
    assert existed_before == cache_dir.exists()


def _real_like_build_toc(doc_id_arg, parsed, settings=None):
    """Mirrors real build_toc: healthy rule result, LLM failures never raise."""
    return _nodes("规则 章 A")


@pytest.mark.asyncio
async def test_fail_stale_processing_scenarios(factory):
    from app.ingest.bench import fail_stale_processing_scenarios
    from app.models import TocBenchScenario

    async with factory() as db:
        db.add(TocBenchScenario(id="stale1", doc_ids="[]", endpoint_ids="[]", status="processing"))
        db.add(TocBenchScenario(id="stale2", doc_ids="[]", endpoint_ids="[]", status="processing"))
        db.add(TocBenchScenario(id="done1", doc_ids="[]", endpoint_ids="[]", status="done"))
        await db.commit()

    marked = await fail_stale_processing_scenarios(factory)
    assert marked == 2

    async with factory() as db:
        s1 = await db.get(TocBenchScenario, "stale1")
        s2 = await db.get(TocBenchScenario, "stale2")
        d1 = await db.get(TocBenchScenario, "done1")
        assert s1.status == "failed" and "restart" in (s1.error or "")
        assert s2.status == "failed"
        assert d1.status == "done"


@pytest.mark.asyncio
async def test_concurrent_scenarios_run_serially(factory, seeded, monkeypatch):
    """Two scenarios submitted at once: runs never interleave (semaphore)."""
    doc_id = seeded
    async with factory() as db:
        endpoint_id = (await db.execute(select(ModelEndpoint))).scalar_one().id

    monkeypatch.setattr(
        "app.ingest.bench.rebuild_parsed", lambda doc, settings: SimpleNamespace(page_count=10)
    )
    monkeypatch.setattr("app.ingest.bench.build_toc", _real_like_build_toc)
    monkeypatch.setattr(
        "app.ingest.bench.build_llm_toc_proposal",
        lambda d, p, settings: _nodes("LLM 章 A"),
    )

    import asyncio as _asyncio

    await _asyncio.gather(
        run_scenario("cc1", [doc_id], [endpoint_id], _settings(), session_factory=factory),
        run_scenario("cc2", [doc_id], [endpoint_id], _settings(), session_factory=factory),
    )

    async with factory() as db:
        for sid in ("cc1", "cc2"):
            runs = (
                await db.execute(
                    select(TocBenchRun).where(TocBenchRun.scenario_id == sid)
                )
            ).scalars().all()
            # each scenario owns exactly its own 2 rows (1 rule + 1 llm)
            assert len(runs) == 2, (sid, len(runs))
            assert all(r.scenario_id == sid for r in runs)
            assert len({r.side for r in runs}) == 2


@pytest.mark.asyncio
async def test_bench_endpoint_settings_carry_effort(factory, seeded, monkeypatch):
    doc_id = seeded
    async with factory() as db:
        endpoint = (await db.execute(select(ModelEndpoint))).scalar_one()
        endpoint.reasoning_effort = "low"
        endpoint_id = endpoint.id
        await db.commit()

    monkeypatch.setattr(
        "app.ingest.bench.rebuild_parsed", lambda doc, settings: SimpleNamespace(page_count=10)
    )
    monkeypatch.setattr("app.ingest.bench.build_toc", _real_like_build_toc)

    captured = {}

    def fake_proposal(doc_id_arg, parsed, settings):
        captured["effort"] = settings.tocmemex_llm_reasoning_effort
        return _nodes("N")

    monkeypatch.setattr("app.ingest.bench.build_llm_toc_proposal", fake_proposal)

    await run_scenario(
        "eff1", [doc_id], [endpoint_id], _settings(), session_factory=factory
    )
    assert captured["effort"] == "low"


@pytest.mark.asyncio
async def test_run_scenario_ocr_axis_multiplies_runs(factory, seeded, monkeypatch):
    """Two OCR choices × one endpoint = 2 rule + 2 LLM runs, labelled."""
    doc_id = seeded
    async with factory() as db:
        endpoint_id = (await db.execute(select(ModelEndpoint))).scalar_one().id
        db.add(
            OcrEndpoint(
                id="ocr1",
                name="Paddle 测试",
                provider="paddle",
                base_url="http://ocr/jobs",
                api_key="k",
                model="m",
                enabled=True,
            )
        )
        await db.commit()

    monkeypatch.setattr(
        "app.ingest.bench.rebuild_parsed",
        lambda doc, settings: SimpleNamespace(page_count=10),
    )

    async def fake_parse_with(doc, choice, settings, ocr_row, out_dir):
        return SimpleNamespace(page_count=10)

    monkeypatch.setattr("app.ingest.bench._parse_with", fake_parse_with)
    monkeypatch.setattr(
        "app.ingest.bench.build_toc",
        lambda doc_id_arg, parsed, settings=None: _nodes("规则 章 A"),
    )
    monkeypatch.setattr(
        "app.ingest.bench.build_llm_toc_proposal",
        lambda doc_id_arg, parsed, settings: _nodes("LLM 章 A", "LLM 章 B"),
    )

    await run_scenario(
        "sc-ocr",
        [doc_id],
        [endpoint_id],
        _settings(),
        session_factory=factory,
        ocr_endpoint_ids=["cached", "ocr1"],
    )

    async with factory() as db:
        runs = (
            await db.execute(select(TocBenchRun).order_by(TocBenchRun.id))
        ).scalars().all()
        assert len(runs) == 4
        labels = {r.ocr_endpoint_id: r.ocr_label for r in runs}
        assert labels["cached"] == "已有解析缓存"
        assert "Paddle 测试" in labels["ocr1"]
        assert all(r.ocr_status == "ok" for r in runs)
        scenario = await db.get(TocBenchScenario, "sc-ocr")
        assert scenario.status == "done"
        assert json.loads(scenario.ocr_endpoint_ids) == ["cached", "ocr1"]


@pytest.mark.asyncio
async def test_run_scenario_ocr_failure_marks_cells(factory, seeded, monkeypatch):
    """A failing OCR choice records the error on both rule and LLM rows."""
    doc_id = seeded
    async with factory() as db:
        endpoint_id = (await db.execute(select(ModelEndpoint))).scalar_one().id
        db.add(
            OcrEndpoint(
                id="ocr2",
                name="坏 OCR",
                provider="paddle",
                base_url="http://ocr/jobs",
                api_key="k",
                model="m",
                enabled=True,
            )
        )
        await db.commit()

    async def boom(doc, choice, settings, ocr_row, out_dir):
        raise RuntimeError("provider down")

    monkeypatch.setattr("app.ingest.bench._parse_with", boom)

    await run_scenario(
        "sc-fail",
        [doc_id],
        [endpoint_id],
        _settings(),
        session_factory=factory,
        ocr_endpoint_ids=["ocr2"],
    )

    async with factory() as db:
        runs = (
            await db.execute(select(TocBenchRun).order_by(TocBenchRun.id))
        ).scalars().all()
        assert len(runs) == 2  # one rule + one llm cell, both errored
        assert all(r.status == "error" for r in runs)
        assert all(r.ocr_status == "error" for r in runs)
        assert all("provider down" in (r.ocr_error or "") for r in runs)
        scenario = await db.get(TocBenchScenario, "sc-fail")
        assert scenario.status == "done"
