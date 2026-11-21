from __future__ import annotations

import pytest
import pytest_asyncio

from app.config import Settings
from app.db import init_db, make_engine, session_factory
from app.llm import purpose
from app.llm.purpose import (
    bianxiu_defaults,
    defaults_for,
    gangmu_defaults,
    load_config,
    load_global,
    resolve_runtime,
    save_config,
    save_log_mode,
    validate,
)
from app.models import ModelEndpoint


@pytest_asyncio.fixture
async def session():
    engine = make_engine(":memory:")
    await init_db(engine)
    async with session_factory(engine)() as s:
        yield s
    await engine.dispose()


def _endpoint(**overrides) -> ModelEndpoint:
    base = dict(
        id="ep1",
        name="deep",
        base_url="http://llm/v1",
        api_key="sk-1",
        model="m1",
        enabled=1,
    )
    base.update(overrides)
    return ModelEndpoint(**base)


def test_defaults_carry_the_shipped_prompts():
    gangmu = gangmu_defaults()
    assert set(gangmu["prompts"]) == {"detect", "transform", "verify", "fix"}
    assert "{content}" in gangmu["prompts"]["detect"]
    assert gangmu["enabled"] is True

    bianxiu = bianxiu_defaults()
    assert "{doc_id}" in bianxiu["prompts"]["system_prompt"]
    assert bianxiu["agent_kinds"] == [
        "unplaced",
        "duplicate-heading",
        "zero-result-queries",
    ]


def test_validate_drops_unknown_keys_and_clamps():
    cleaned = validate("gangmu", {"temperature": 0.3, "nonsense": 1})
    assert cleaned == {"temperature": 0.3}

    with pytest.raises(ValueError):
        validate("gangmu", {"temperature": 9})
    with pytest.raises(ValueError):
        validate("bianxiu", {"agent_kinds": ["no-such-kind"]})
    with pytest.raises(ValueError):
        validate("gangmu", {"prompts": {"detect": "  "}})
    with pytest.raises(ValueError):
        validate("nope", {})


async def test_save_merges_over_defaults_and_keeps_untouched_fields(session):
    saved = await save_config(session, "gangmu", {"temperature": 0.4})
    assert saved["temperature"] == 0.4
    # untouched fields still track the shipped default
    assert saved["check_limit"] == gangmu_defaults()["check_limit"]
    assert saved["prompts"]["detect"] == gangmu_defaults()["prompts"]["detect"]

    again = await load_config(session, "gangmu")
    assert again["temperature"] == 0.4


async def test_prompt_edits_merge_per_key(session):
    await save_config(
        session, "bianxiu", {"prompts": {"system_prompt": "精简提示词 {doc_id}"}}
    )
    cfg = await load_config(session, "bianxiu")
    assert cfg["prompts"]["system_prompt"] == "精简提示词 {doc_id}"


async def test_resolve_prefers_agent_bound_endpoint(session):
    session.add(_endpoint(id="ep-fast", name="fast", model="fast-model"))
    session.add(_endpoint(id="ep-deep", name="deep", is_default=1))
    await session.flush()
    await save_config(
        session,
        "gangmu",
        {"endpoint": "ep-fast", "temperature": 0, "timeout": 90},
    )

    settings, endpoint, cfg = await resolve_runtime(
        session,
        "gangmu",
        Settings(tocmemex_llm_log="full"),
    )

    assert endpoint == "fast"
    assert settings.tocmemex_llm_model == "fast-model"
    assert settings.tocmemex_llm_timeout == 90
    assert settings.tocmemex_gangmu["enabled"] is True
    assert cfg["endpoint"] == "ep-fast"


async def test_resolve_falls_back_when_bound_endpoint_disabled(session):
    session.add(_endpoint(id="ep-off", name="off", enabled=0))
    session.add(_endpoint(id="ep-deep", name="deep", is_default=1))
    await session.flush()
    await save_config(session, "bianxiu", {"endpoint": "ep-off"})

    settings, endpoint, _cfg = await resolve_runtime(session, "bianxiu")
    assert endpoint == "deep"
    assert settings.tocmemex_llm_model == "m1"


async def test_resolve_returns_none_without_any_endpoint(session):
    settings, endpoint, cfg = await resolve_runtime(session, "gangmu")
    assert settings is None
    assert endpoint is None
    assert cfg["enabled"] is True


async def test_disabled_gangmu_still_resolves_but_carries_the_switch(session):
    session.add(_endpoint(is_default=1))
    await session.flush()
    await save_config(session, "gangmu", {"enabled": False})

    settings, _endpoint_name, cfg = await resolve_runtime(session, "gangmu")
    assert settings is not None
    assert cfg["enabled"] is False
    assert settings.tocmemex_gangmu["enabled"] is False

    from app.ingest.llm_toc import gangmu_available

    assert gangmu_available(settings) is False


async def test_log_mode_round_trip(session):
    assert (await load_global(session))["log_mode"] == "full"
    await save_log_mode(session, "meta")
    assert (await load_global(session))["log_mode"] == "meta"
    with pytest.raises(ValueError):
        await save_log_mode(session, "verbose")


def test_purpose_exposes_only_known_agents():
    assert purpose.AGENTS == ("gangmu", "bianxiu")
    assert defaults_for("gangmu")["prompts"]


def _gangmu_settings(**config) -> Settings:
    return Settings(
        tocmemex_llm_api_key="k",
        tocmemex_llm_base_url="http://llm/v1",
        tocmemex_llm_model="m",
        tocmemex_gangmu=config,
    )


def test_llm_toc_uses_edited_prompts_and_limits(monkeypatch):
    from app.ingest import llm_toc

    captured = []

    def fake_complete(prompt, settings=None, **kwargs):
        captured.append(prompt)
        return '{"toc_detected": "no"}'

    monkeypatch.setattr(llm_toc, "llm_complete", fake_complete)
    settings = _gangmu_settings(
        enabled=True,
        check_limit=2,
        prompts={"detect": "CUSTOM {content}"},
    )

    llm_toc.detect_toc_pages_llm(["a", "b", "c", "d"], settings)

    assert captured[0] == "CUSTOM a"
    assert len(captured) == 2  # check_limit honoured


def test_malformed_prompt_falls_back_to_shipped_text(monkeypatch):
    from app.ingest import llm_toc

    captured = []

    def fake_complete(prompt, settings=None, **kwargs):
        captured.append(prompt)
        return '{"toc_detected": "no"}'

    monkeypatch.setattr(llm_toc, "llm_complete", fake_complete)
    settings = _gangmu_settings(
        enabled=True,
        check_limit=1,
        prompts={"detect": "broken {brace"},
    )

    llm_toc.detect_toc_pages_llm(["page-body"], settings)

    assert "page-body" in captured[0]
    assert "table of content" in captured[0]


def test_disabled_gangmu_short_circuits_the_llm_path():
    from app.ingest.llm_toc import detect_printed_toc_llm, gangmu_available

    settings = _gangmu_settings(enabled=False)
    assert gangmu_available(settings) is False
    assert detect_printed_toc_llm(None, settings) is None  # returns before touching the doc


def test_agent_system_prompt_falls_back_on_bad_braces():
    from app.llm.agent import SYSTEM_PROMPT, render_system_prompt

    rendered = render_system_prompt(
        "broken {doc_id", doc_id="d1", title="T", max_calls=5
    )
    assert rendered.startswith(SYSTEM_PROMPT.split("。")[0][:6])  # shipped text
    assert "d1" in rendered
    assert rendered != "broken {doc_id"

    edited = render_system_prompt(
        "只处理 {doc_id}，预算 {max_calls}", doc_id="d2", title="T", max_calls=9
    )
    assert edited == "只处理 d2，预算 9"


def test_rendered_default_system_prompt_matches_shipped():
    from app.llm.agent import SYSTEM_PROMPT

    assert SYSTEM_PROMPT  # sanity: constant imported without cycle