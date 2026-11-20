"""Model endpoint registry: DB-backed LLM endpoint resolution.

``active_endpoint`` picks the LLM configuration a call should use: the
enabled default endpoint in ``model_endpoints`` wins; when the DB has none
the ``.env`` settings are the fallback; when neither exists, LLM features
are off.

Serialization for anything that leaves the server goes through
``endpoint_out`` — the plaintext api_key never leaves the process.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..llm.client import llm_available
from ..models import ModelEndpoint


def endpoint_out(row: ModelEndpoint) -> dict:
    """Redacted serialization: no plaintext key, only has_key + tail."""
    key = row.api_key or ""
    return {
        "id": row.id,
        "name": row.name,
        "base_url": row.base_url,
        "model": row.model,
        "reasoning_effort": row.reasoning_effort or "",
        "enabled": bool(row.enabled),
        "is_default": bool(row.is_default),
        "has_key": bool(key),
        "key_tail": key[-4:] if key else "",
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


async def active_endpoint(session: AsyncSession, settings: Settings | None = None) -> dict | None:
    """Resolve the endpoint to call now: DB default first, then .env, else None."""
    row = (
        await session.execute(
            select(ModelEndpoint)
            .where(ModelEndpoint.enabled.is_(True), ModelEndpoint.is_default.is_(True))
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is not None:
        return {
            "name": row.name,
            "base_url": row.base_url,
            "api_key": row.api_key,
            "model": row.model,
            "reasoning_effort": row.reasoning_effort or "",
        }
    settings = settings or Settings()
    if llm_available(settings):
        return {
            "name": "env",
            "base_url": settings.tocmemex_llm_base_url,
            "api_key": settings.tocmemex_llm_api_key,
            "model": settings.tocmemex_llm_model,
            "reasoning_effort": settings.tocmemex_llm_reasoning_effort,
        }
    return None


async def active_settings(
    session: AsyncSession, settings: Settings | None = None
) -> tuple[Settings | None, str | None]:
    """The LLM settings a call should use now, plus the endpoint's name.

    ``llm_available()`` only looks at ``.env``. The curator's agent loop used
    it and therefore always fell back to the deterministic path whenever the
    configured endpoint lived in ``model_endpoints`` (the normal case for the
    Web UI and for ingest). Resolving through ``active_endpoint`` makes the
    same DB-first rule apply everywhere.
    """
    from ..config import get_settings

    settings = settings or get_settings()
    resolved = await active_endpoint(session, settings)
    if resolved is None:
        return None, None
    if not (resolved["api_key"] and resolved["base_url"] and resolved["model"]):
        return None, None
    if resolved["name"] == "env":
        return settings, "env"
    return (
        settings.model_copy(
            update={
                "tocmemex_llm_api_key": resolved["api_key"],
                "tocmemex_llm_base_url": resolved["base_url"],
                "tocmemex_llm_model": resolved["model"],
                "tocmemex_llm_reasoning_effort": resolved["reasoning_effort"]
                or settings.tocmemex_llm_reasoning_effort,
            }
        ),
        resolved["name"],
    )
