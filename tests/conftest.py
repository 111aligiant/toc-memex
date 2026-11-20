from __future__ import annotations

import pytest

from app.db import init_db, make_engine, session_factory


@pytest.fixture(autouse=True, scope="session")
def _no_llm_call_log():
    """Keep tests from appending to the real data/llm/calls.jsonl.

    Every Settings() reads the environment, so turning the log off here
    covers tests that construct their own settings. Tests that exercise the
    log opt back in by passing ``tocmemex_llm_log`` and a tmp data dir.
    """
    import os

    previous = os.environ.get("TOCMEMEX_LLM_LOG")
    os.environ["TOCMEMEX_LLM_LOG"] = "off"
    yield
    if previous is None:
        os.environ.pop("TOCMEMEX_LLM_LOG", None)
    else:
        os.environ["TOCMEMEX_LLM_LOG"] = previous


@pytest.fixture
async def db_engine(tmp_path):
    engine = make_engine(tmp_path / "tocmemex.db")
    await init_db(engine)
    yield engine
    await engine.dispose()


@pytest.fixture
def db_session_factory(db_engine):
    return session_factory(db_engine)