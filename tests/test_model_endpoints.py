from __future__ import annotations

import pytest
import pytest_asyncio

from app.config import Settings
from app.db import init_db, make_engine, session_factory
from app.llm.endpoints import active_endpoint, endpoint_out
from app.models import ModelEndpoint


@pytest_asyncio.fixture
async def db_engine():
    engine = make_engine(":memory:")
    await init_db(engine)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(db_engine):
    factory = session_factory(db_engine)
    async with factory() as s:
        yield s


def _settings(**overrides) -> Settings:
    base = dict(
        tocmemex_llm_api_key="env-key-9999",
        tocmemex_llm_model="env-model",
        tocmemex_llm_base_url="http://env/v1",
    )
    base.update(overrides)
    return Settings(**base)


def test_endpoint_out_redacts_key():
    row = ModelEndpoint(
        name="main",
        base_url="http://x/v1",
        api_key="sk-abcdef123456",
        model="gpt-test",
    )
    out = endpoint_out(row)
    assert "sk-abcdef123456" not in str(out)
    assert out["has_key"] is True
    assert out["key_tail"] == "3456"
    assert out["name"] == "main"
    assert out["base_url"] == "http://x/v1"
    assert out["model"] == "gpt-test"


def test_endpoint_out_without_key():
    row = ModelEndpoint(name="bare", base_url="http://x/v1", api_key="", model="m")
    out = endpoint_out(row)
    assert out["has_key"] is False
    assert out["key_tail"] == ""


async def test_active_endpoint_db_default_wins(session):
    session.add(
        ModelEndpoint(
            name="db",
            base_url="http://db/v1",
            api_key="db-key-0001",
            model="db-model",
            enabled=True,
            is_default=True,
        )
    )
    await session.commit()
    got = await active_endpoint(session, _settings())
    assert got == {
        "name": "db",
        "base_url": "http://db/v1",
        "api_key": "db-key-0001",
        "model": "db-model",
        "reasoning_effort": "",
    }


async def test_active_endpoint_falls_back_to_env(session):
    got = await active_endpoint(session, _settings())
    assert got == {
        "name": "env",
        "base_url": "http://env/v1",
        "api_key": "env-key-9999",
        "model": "env-model",
        "reasoning_effort": "",
    }


async def test_active_endpoint_none_without_any(session):
    got = await active_endpoint(
        session,
        _settings(
            tocmemex_llm_api_key="", tocmemex_llm_model="", tocmemex_llm_base_url=""
        ),
    )
    assert got is None


async def test_active_endpoint_ignores_disabled_or_non_default(session):
    session.add_all(
        [
            ModelEndpoint(
                name="off",
                base_url="http://off/v1",
                api_key="k-0001",
                model="m",
                enabled=False,
                is_default=True,
            ),
            ModelEndpoint(
                name="notdefault",
                base_url="http://nd/v1",
                api_key="k-0002",
                model="m",
                enabled=True,
                is_default=False,
            ),
        ]
    )
    await session.commit()
    got = await active_endpoint(session, _settings())
    # DB has no usable default -> falls back to .env
    assert got is not None
    assert got["base_url"] == "http://env/v1"


# ── reasoning_effort column ──


@pytest.mark.asyncio
async def test_ensure_schema_adds_reasoning_effort(tmp_path):
    """A pre-existing DB whose model_endpoints predates the column upgrades in place."""
    import sqlite3 as _sqlite3

    from app.db import ensure_schema

    db_path = tmp_path / "old.db"
    with _sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE model_endpoints (
                id VARCHAR(36) PRIMARY KEY,
                name VARCHAR(120) NOT NULL,
                base_url VARCHAR(500) NOT NULL,
                api_key VARCHAR(500) NOT NULL,
                model VARCHAR(120) NOT NULL,
                enabled INTEGER NOT NULL,
                is_default INTEGER NOT NULL,
                created_at VARCHAR(40) NOT NULL,
                updated_at VARCHAR(40) NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO model_endpoints VALUES ('e1','n','http://x/v1','k-1234','m',1,0,'t','t')"
        )

    engine = make_engine(db_path)
    await ensure_schema(engine)
    await engine.dispose()

    with _sqlite3.connect(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(model_endpoints)")}
        assert "reasoning_effort" in cols
        conn.execute("UPDATE model_endpoints SET reasoning_effort='low' WHERE id='e1'")
        val = conn.execute(
            "SELECT reasoning_effort FROM model_endpoints WHERE id='e1'"
        ).fetchone()[0]
        assert val == "low"


def test_endpoint_out_reasoning_effort_roundtrip():
    row = ModelEndpoint(
        name="n", base_url="http://x/v1", api_key="k-1234", model="m", reasoning_effort="low"
    )
    assert endpoint_out(row)["reasoning_effort"] == "low"
    bare = ModelEndpoint(name="n2", base_url="http://x/v1", api_key="", model="m")
    assert endpoint_out(bare)["reasoning_effort"] == ""


async def test_active_endpoint_carries_effort(session):
    session.add(
        ModelEndpoint(
            name="db",
            base_url="http://db/v1",
            api_key="k-0001",
            model="m",
            reasoning_effort="low",
            enabled=True,
            is_default=True,
        )
    )
    await session.commit()
    got = await active_endpoint(session, _settings())
    assert got["reasoning_effort"] == "low"


async def test_active_endpoint_db_empty_uses_env_effort(session):
    session.add(
        ModelEndpoint(
            name="db",
            base_url="http://db/v1",
            api_key="k-0001",
            model="m",
            reasoning_effort=None,
            enabled=True,
            is_default=True,
        )
    )
    await session.commit()
    got = await active_endpoint(session, _settings(tocmemex_llm_reasoning_effort="high"))
    # DB row has no effort -> empty string; env fallback is a separate branch
    assert got["reasoning_effort"] == ""


async def test_active_endpoint_env_fallback_carries_effort(session):
    got = await active_endpoint(session, _settings(tocmemex_llm_reasoning_effort="medium"))
    assert got["base_url"] == "http://env/v1"
    assert got["reasoning_effort"] == "medium"
