"""MCP tool profiles: ``TOCMEMEX_TOOLS=default|full``.

The module-level ``server`` is the complete surface the console and tests
use in-process. Only ``build_server(profile)`` decides what an outside agent
sees, so these tests never rely on the ambient env or on import order.
"""

from __future__ import annotations

import re

import pytest

from app.mcp_server import (
    DEFAULT_TOOLS,
    PROFILE_DEFAULT,
    PROFILE_FULL,
    _REGISTRY,
    _active_profile,
    build_server,
    server,
)


def _names(srv_tools) -> set[str]:
    return {tool.name for tool in srv_tools}


async def _tool_names(profile: str) -> set[str]:
    return _names(await build_server(profile).list_tools())


@pytest.fixture
def clean_settings_cache():
    from app.config import get_settings

    get_settings.cache_clear()
    yield get_settings
    get_settings.cache_clear()


async def test_full_profile_and_module_server_expose_all_40():
    assert len(await _tool_names(PROFILE_FULL)) == 40
    # The console builds its schemas from this module-level instance.
    assert len(await server.list_tools()) == 40


async def test_default_profile_is_exactly_eight_tools():
    assert await _tool_names(PROFILE_DEFAULT) == DEFAULT_TOOLS
    assert len(DEFAULT_TOOLS) == 8


async def test_default_tools_are_a_subset_of_full():
    assert DEFAULT_TOOLS <= await _tool_names(PROFILE_FULL)


def test_registry_profiles_hide_exactly_the_other_32():
    by_profile = {name: profile for name, _, profile in _REGISTRY}
    assert len(by_profile) == 40
    assert {n for n, p in by_profile.items() if p == PROFILE_DEFAULT} == DEFAULT_TOOLS
    assert {
        n for n, p in by_profile.items() if p == PROFILE_FULL
    } == set(by_profile) - DEFAULT_TOOLS


async def test_unknown_profile_falls_back_to_default():
    assert await _tool_names("nonsense") == DEFAULT_TOOLS


def test_active_profile_reads_env_and_falls_back(monkeypatch, clean_settings_cache):
    get_settings = clean_settings_cache

    monkeypatch.setenv("TOCMEMEX_TOOLS", " full ")
    get_settings.cache_clear()
    assert _active_profile() == PROFILE_FULL

    monkeypatch.setenv("TOCMEMEX_TOOLS", "bogus")
    get_settings.cache_clear()
    assert _active_profile() == PROFILE_DEFAULT

    monkeypatch.setenv("TOCMEMEX_TOOLS", "")
    get_settings.cache_clear()
    assert _active_profile() == PROFILE_DEFAULT


def test_default_instructions_only_name_default_tools():
    from app.agent.instructions import DEFAULT_INSTRUCTIONS

    mentioned = set(re.findall(r"([a-z][a-z_]+)\(", DEFAULT_INSTRUCTIONS))
    # Words a guard regex can pick up that are not tool calls.
    non_tools = {"int", "str"}
    assert mentioned - non_tools
    assert mentioned - non_tools <= DEFAULT_TOOLS


def test_instructions_for_picks_matching_text():
    from app.agent.instructions import (
        DEFAULT_INSTRUCTIONS,
        FULL_INSTRUCTIONS,
        instructions_for,
    )

    assert instructions_for(PROFILE_DEFAULT) == DEFAULT_INSTRUCTIONS
    assert instructions_for(PROFILE_FULL) == FULL_INSTRUCTIONS
    assert instructions_for("bogus") == DEFAULT_INSTRUCTIONS


async def test_memex_overview_reports_collection_descriptions(tmp_path):
    from app.index.documents import memex_overview
    from test_collections import _seed_two_collections

    engine, Session = await _seed_two_collections(tmp_path)
    async with Session() as db:
        overview = await memex_overview(db)
    await engine.dispose()

    descriptions = {
        row["name"]: row["description"] for row in overview["collections"]
    }
    assert descriptions["电子标准"] == "国标行标"
    assert descriptions["求职"] == "JD 与个人经历"
