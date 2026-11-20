"""DB-backed OCR endpoints: resolution, env fallback, and ingest wiring."""

from __future__ import annotations

import pytest
import pytest_asyncio

from app.config import Settings
from app.db import init_db, make_engine, session_factory
from app.models import OcrEndpoint
from app.ocr.endpoints import (
    _endpoint_settings,
    active_ocr_endpoint,
    ocr_endpoint_out,
)


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
        tocmemex_ocr_provider="paddle",
        paddleocr_api_token="env-token-4321",
        paddleocr_api_url="https://env.test/jobs",
        paddleocr_api_model="PaddleOCR-VL-1.6",
    )
    base.update(overrides)
    return Settings(**base)


def test_ocr_endpoint_out_redacts_key():
    row = OcrEndpoint(
        name="main",
        provider="mistral",
        base_url="https://api.mistral.ai/v1/ocr",
        api_key="sk-abcdef123456",
        model="mistral-ocr-latest",
    )
    out = ocr_endpoint_out(row)
    assert "sk-abcdef123456" not in str(out)
    assert out["has_key"] is True
    assert out["key_tail"] == "3456"
    assert out["provider"] == "mistral"


def test_endpoint_settings_keeps_data_paths_and_provider_credentials():
    settings = Settings(
        tocmemex_data_dir=__import__("pathlib").Path("D:/custom/data"),
        tocmemex_ocr_poll_timeout=99,
        paddleocr_api_token="env-paddle",
    )
    row = OcrEndpoint(
        name="m",
        provider="mistral",
        base_url="https://api.mistral.ai/v1/ocr",
        api_key="sk-mistral",
        model="mistral-ocr-latest",
    )
    resolved = _endpoint_settings(settings, row)
    assert resolved.tocmemex_ocr_provider == "mistral"
    assert resolved.mistral_api_key == "sk-mistral"
    assert resolved.mistral_api_url == "https://api.mistral.ai/v1/ocr"
    assert resolved.mistral_ocr_model == "mistral-ocr-latest"
    # Data paths and polling must not silently move to defaults.
    assert resolved.tocmemex_data_dir == settings.tocmemex_data_dir
    assert resolved.tocmemex_ocr_poll_timeout == 99
    # Paddle image-download fallback survives a non-paddle endpoint.
    assert resolved.paddleocr_api_token == "env-paddle"


def test_endpoint_settings_paddle_uses_row_key():
    settings = _settings()
    row = OcrEndpoint(
        name="p",
        provider="paddle",
        base_url="https://row.test/jobs",
        api_key="row-token",
        model="PaddleOCR-VL-2.0",
    )
    resolved = _endpoint_settings(settings, row)
    assert resolved.paddleocr_api_token == "row-token"
    assert resolved.paddleocr_api_url == "https://row.test/jobs"
    assert resolved.paddleocr_api_model == "PaddleOCR-VL-2.0"


async def test_active_ocr_endpoint_db_default_wins(session):
    session.add(
        OcrEndpoint(
            name="db",
            provider="mistral",
            base_url="https://api.mistral.ai/v1/ocr",
            api_key="db-key-0001",
            model="mistral-ocr-latest",
            enabled=True,
            is_default=True,
        )
    )
    await session.commit()
    got = await active_ocr_endpoint(session, _settings())
    assert got is not None
    assert got["name"] == "db"
    assert got["provider"] == "mistral"
    assert got["settings"].mistral_api_key == "db-key-0001"


async def test_active_ocr_endpoint_falls_back_to_env(session):
    got = await active_ocr_endpoint(session, _settings())
    assert got is not None
    assert got["name"] == "env"
    assert got["provider"] == "paddle"
    assert got["settings"].paddleocr_api_token == "env-token-4321"


async def test_active_ocr_endpoint_none_without_any(session):
    got = await active_ocr_endpoint(
        session, _settings(paddleocr_api_token="")
    )
    assert got is None


async def test_active_ocr_endpoint_ignores_disabled_or_non_default(session):
    session.add_all(
        [
            OcrEndpoint(
                name="off",
                provider="mistral",
                base_url="https://x/v1/ocr",
                api_key="k-0001",
                model="m",
                enabled=False,
                is_default=True,
            ),
            OcrEndpoint(
                name="notdefault",
                provider="mistral",
                base_url="https://nd/v1/ocr",
                api_key="k-0002",
                model="m",
                enabled=True,
                is_default=False,
            ),
        ]
    )
    await session.commit()
    got = await active_ocr_endpoint(session, _settings())
    # DB has no usable default -> .env
    assert got is not None
    assert got["name"] == "env"


async def test_ingest_uses_db_default_endpoint(tmp_path, monkeypatch):
    """ingest_path resolves the DB default before falling back to .env."""
    from app.ingest import pipeline
    from app.ingest.pipeline import ingest_path, set_engine
    from app.ocr.providers.base import BaseOcrProvider
    from app.ocr.models import OcrBlock, OcrPage, ParsedDocument

    settings = Settings(
        tocmemex_db_path=tmp_path / "archive.db",
        tocmemex_data_dir=tmp_path / "data",
        paddleocr_api_token="env-token",
    )
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)

    seen: dict = {}

    class SpyProvider(BaseOcrProvider):
        name = "mistral"

        def check_config(self) -> None:
            pass

        async def parse_pdf(self, pdf_path, out_dir):
            seen["provider"] = self.name
            seen["key"] = self.settings.mistral_api_key
            return ParsedDocument(
                pages=[
                    OcrPage(
                        0,
                        100,
                        200,
                        "",
                        [OcrBlock("text", "正文", (0, 0, 100, 10), 1)],
                    )
                ]
            )

    monkeypatch.setattr(
        pipeline, "build_ocr_provider", lambda resolved: SpyProvider(resolved)
    )
    monkeypatch.setattr(pipeline, "parse_pdf_text", lambda path: None)
    monkeypatch.setattr(pipeline, "read_pdf_bookmarks", lambda path: [])

    async with session_factory(engine)() as db:
        db.add(
            OcrEndpoint(
                name="mistral-main",
                provider="mistral",
                base_url="https://api.mistral.ai/v1/ocr",
                api_key="db-key-7777",
                model="mistral-ocr-latest",
                enabled=True,
                is_default=True,
            )
        )
        await db.commit()

    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    await ingest_path(pdf, settings=settings)

    assert seen == {"provider": "mistral", "key": "db-key-7777"}
    await engine.dispose()
