"""DB-backed OCR endpoint resolution (mirrors ``app.llm.endpoints``).

``active_ocr_endpoint`` picks the OCR configuration an ingest should use:
the enabled default endpoint in ``ocr_endpoints`` wins; when the DB has
none the ``.env`` settings are the fallback; when neither exists, ingest
fails the way it always has (a missing-token error from the provider).

Serialization for anything that leaves the server goes through
``ocr_endpoint_out`` — the plaintext api_key never leaves the process.

The returned dict is turned into a ``Settings``-shaped object by
``ocr_endpoint_settings``; every provider already reads its config from a
``Settings``, so endpoints reuse the exact same code path as .env.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..models import OcrEndpoint

# .env field each provider's credentials live in, for the env fallback.
_PROVIDER_DEFAULTS = {
    "paddle": {
        "provider": "paddle",
        "api_key_field": "paddleocr_api_token",
        "base_url_field": "paddleocr_api_url",
        "model_field": "paddleocr_api_model",
    },
    "mistral": {
        "provider": "mistral",
        "api_key_field": "mistral_api_key",
        "base_url_field": "mistral_api_url",
        "model_field": "mistral_ocr_model",
    },
    "mineru": {
        "provider": "mineru",
        "api_key_field": "mineru_api_token",
        "base_url_field": "mineru_api_url",
        "model_field": "mineru_model_version",
    },
    "azure": {
        "provider": "azure",
        "api_key_field": "azure_di_key",
        "base_url_field": "azure_di_endpoint",
        "model_field": "azure_di_model",
    },
}


def ocr_endpoint_out(row: OcrEndpoint) -> dict:
    """Redacted serialization: no plaintext key, only has_key + tail."""
    key = row.api_key or ""
    return {
        "id": row.id,
        "name": row.name,
        "provider": row.provider,
        "base_url": row.base_url,
        "model": row.model,
        "enabled": bool(row.enabled),
        "is_default": bool(row.is_default),
        "has_key": bool(key),
        "key_tail": key[-4:] if key else "",
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _endpoint_settings(settings: Settings, row: OcrEndpoint) -> Settings:
    """A Settings carrying this endpoint's credentials for its provider.

    Data paths and polling knobs are inherited from the caller's settings:
    the endpoint only overrides credentials, so cache/resource locations
    never silently move to a default ``data/`` directory.
    """
    defaults = _PROVIDER_DEFAULTS.get(row.provider)
    if defaults is None:
        raise ValueError(f"unknown OCR provider {row.provider!r}")
    return Settings(
        tocmemex_db_path=settings.tocmemex_db_path,
        tocmemex_data_dir=settings.tocmemex_data_dir,
        tocmemex_ocr_provider=row.provider,
        tocmemex_ocr_poll_timeout=settings.tocmemex_ocr_poll_timeout,
        tocmemex_ocr_poll_interval=settings.tocmemex_ocr_poll_interval,
        **{
            defaults["api_key_field"]: row.api_key,
            defaults["base_url_field"]: row.base_url,
            defaults["model_field"]: row.model,
            # Keep the .env paddle token as the image-download fallback for
            # signed URLs when this row is not itself a paddle endpoint.
            "paddleocr_api_token": row.api_key
            if row.provider == "paddle"
            else settings.paddleocr_api_token,
        },
    )


async def active_ocr_endpoint(
    session: AsyncSession, settings: Settings | None = None
) -> dict | None:
    """Resolve the OCR endpoint to use now: DB default first, then .env, else None.

    The dict carries ``settings`` (a Settings the provider reads) plus the
    display fields for logging / metadata.
    """
    row = (
        await session.execute(
            select(OcrEndpoint)
            .where(OcrEndpoint.enabled.is_(True), OcrEndpoint.is_default.is_(True))
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is not None:
        settings = settings or Settings()
        return {
            "name": row.name,
            "provider": row.provider,
            "settings": _endpoint_settings(settings, row),
        }
    settings = settings or Settings()
    defaults = _PROVIDER_DEFAULTS.get(settings.tocmemex_ocr_provider or "paddle")
    if defaults is None:
        return None
    api_key = getattr(settings, defaults["api_key_field"])
    if not api_key:
        return None
    return {
        "name": "env",
        "provider": settings.tocmemex_ocr_provider or "paddle",
        "settings": settings,
    }


__all__ = [
    "_endpoint_settings",
    "active_ocr_endpoint",
    "ocr_endpoint_out",
]
