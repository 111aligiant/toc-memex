from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import BeforeValidator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _blank_to_none(value):
    """Treat an empty env var (``PADDLEOCR_POLL_TIMEOUT=``) as "unset".

    ``.env.example`` ships these keys empty; without this a copied file
    would fail validation for an ``int | None`` field.
    """
    if isinstance(value, str) and not value.strip():
        return None
    return value


OptionalInt = Annotated[int | None, BeforeValidator(_blank_to_none)]


class Settings(BaseSettings):
    """Runtime configuration. Env vars are upper-case field names (TOCMEMEX_DB_PATH, ...)."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    tocmemex_db_path: Path = Path("data/tocmemex.db")
    tocmemex_data_dir: Path = Path("data")

    # Bind policy for the unauthenticated web / inspector / MCP HTTP entry
    # points. False = loopback only (see ``app.bindguard``); True is the
    # explicit opt-in for a LAN or reverse-proxied deployment.
    tocmemex_allow_nonlocal: bool = False

    # OCR provider: "paddle" (default), "mistral", "mineru" or "azure".
    # Selecting a provider only affects the OCR fallback path; PDFs with a
    # usable text layer are still read directly. See docs/ocr-providers.md.
    tocmemex_ocr_provider: str = "paddle"
    tocmemex_ocr_poll_timeout: int = 600
    tocmemex_ocr_poll_interval: int = 5
    # Opt-in for self-hosted OCR (PaddleX / MinerU) on loopback or a LAN:
    # permits non-public addresses and plain http for outbound OCR calls.
    # Default off, so a writable base_url cannot reach internal services.
    tocmemex_ocr_allow_private_hosts: bool = False

    paddleocr_api_token: str = ""
    paddleocr_api_url: str = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
    paddleocr_api_model: str = "PaddleOCR-VL-1.6"
    # Optional per-provider overrides for the Paddle jobs API. None means
    # "use the TOCMEMEX_OCR_POLL_* knobs above", so the documented global
    # settings work for every provider unless one is pinned explicitly.
    paddleocr_poll_timeout: OptionalInt = None
    paddleocr_poll_interval: OptionalInt = None

    mistral_api_key: str = ""
    mistral_api_url: str = "https://api.mistral.ai/v1/ocr"
    mistral_ocr_model: str = "mistral-ocr-latest"

    mineru_api_token: str = ""
    mineru_api_url: str = "https://mineru.net/api/v4"
    mineru_model_version: str = "pipeline"

    azure_di_endpoint: str = ""
    azure_di_key: str = ""
    azure_di_model: str = "prebuilt-layout"
    azure_di_api_version: str = "2024-11-30"

    tocmemex_max_chunk_chars: int = 1200
    tocmemex_chunk_overlap: int = 100

    # Optional query-term aliases, as JSON: {"term": ["alternative", ...]}.
    # Empty by default; the search layer is generic and knows no domain.
    tocmemex_search_synonyms: dict[str, list[str]] = {}

    # LLM for ingest-time TOC parsing. Empty key/base_url/model disables
    # every LLM feature; the pipeline then behaves exactly as before.
    tocmemex_llm_api_key: str = ""
    tocmemex_llm_base_url: str = ""
    tocmemex_llm_model: str = ""
    tocmemex_llm_temperature: float = 0.0
    # Optional OpenAI reasoning_effort sent with every completion; empty
    # means the field is omitted entirely (vendor default applies).
    tocmemex_llm_reasoning_effort: str = ""
    # Request knobs. Per-agent values are resolved into these fields by
    # ``app.llm.purpose.resolve_runtime``; direct callers get the defaults.
    tocmemex_llm_max_tokens: int = 4096
    tocmemex_llm_retries: int = 1
    tocmemex_llm_timeout: float = 120.0

    # 纲目 agent's effective config (prompts / tuning), attached by
    # ``resolve_runtime`` and read by ``app/ingest/llm_toc.py``. None means
    # "shipped defaults" (bench, tests and plain CLI calls take that path).
    tocmemex_gangmu: dict | None = None

    # LLM call log: full (800-char previews) | meta (lengths only) | off.
    # ADR-0002 §10.3. Full never stores whole texts, only the head.
    tocmemex_llm_log: str = "full"

    @property
    def ocr_dir(self) -> Path:
        return self.tocmemex_data_dir / "ocr"

    @property
    def resources_dir(self) -> Path:
        return self.tocmemex_data_dir / "resources"


@lru_cache
def get_settings() -> Settings:
    return Settings()