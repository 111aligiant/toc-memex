"""OCR provider base: shared HTTP plumbing and cache writing.

A provider's only job is ``parse_pdf(pdf_path, out_dir) -> ParsedDocument``
whose pages use the project's ``OcrPage``/``OcrBlock`` model. Everything
after that (TOC, chunking, resources, FTS) is provider-agnostic.

Each provider writes two files into ``out_dir``:

- ``raw.jsonl`` — the vendor response verbatim (audit / replay);
- ``pages.json`` — the normalized pages (what downstream reads).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from ...config import Settings
from ..cache import save_pages
from ..models import ParsedDocument
from ..netguard import assert_public_url


class OcrProviderError(RuntimeError):
    """A provider call failed after retries."""


class BaseOcrProvider:
    name = "base"

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._transport = transport

    # ── HTTP helpers ──────────────────────────────────────────────────
    def _client(self, timeout: float = 180.0) -> httpx.AsyncClient:
        # follow_redirects is off by default in httpx; keep it explicit so a
        # 302 cannot bounce a validated URL to an internal address.
        return httpx.AsyncClient(
            timeout=timeout,
            transport=self._transport,
            follow_redirects=False,
        )

    async def _guard_url(self, url: str) -> None:
        """Reject outbound URLs that are not safe to request.

        The endpoint ``base_url`` is writable through the unauthenticated
        local API, so every request (including URLs a vendor returns) is
        checked here rather than only at configuration time.
        """
        await assert_public_url(
            url,
            allow_private=self.settings.tocmemex_ocr_allow_private_hosts,
            # A custom transport means the caller owns the network layer
            # (tests, in-process proxies): no real DNS lookup to validate.
            resolve=self._transport is None,
        )

    @property
    def poll_timeout(self) -> int:
        return self.settings.tocmemex_ocr_poll_timeout

    @property
    def poll_interval(self) -> int:
        return self.settings.tocmemex_ocr_poll_interval

    def check_config(self) -> None:
        """Raise OcrProviderError when required settings are missing."""
        raise NotImplementedError

    # ── connectivity probe (frontend "测试" button) ───────────────────
    def _probe_target(self) -> tuple[str, dict[str, str]]:
        """A cheap GET that proves reachability + credentials.

        A 404 on a jobs URL still proves DNS, TLS and the gateway are fine;
        only 401/403/5xx count as failures. Providers override this when a
        real catalog endpoint exists.
        """
        raise NotImplementedError

    async def probe(self) -> dict:
        """Return {ok, elapsed_ms} or {ok: False, error, elapsed_ms}."""
        import time as _time

        started = _time.monotonic()
        try:
            self.check_config()
            url, headers = self._probe_target()
            await self._guard_url(url)
            async with self._client(timeout=20.0) as client:
                response = await client.get(url, headers=headers)
            elapsed = int((_time.monotonic() - started) * 1000)
            if response.status_code in (401, 403):
                return {
                    "ok": False,
                    "error": f"认证失败 HTTP {response.status_code}",
                    "elapsed_ms": elapsed,
                }
            if response.status_code >= 500:
                return {
                    "ok": False,
                    "error": f"服务端错误 HTTP {response.status_code}",
                    "elapsed_ms": elapsed,
                }
            return {"ok": True, "elapsed_ms": elapsed}
        except Exception as exc:  # network, config, or bad URL
            # Some httpx/OS errors carry an empty message; always report a
            # non-empty reason so the UI has something to show.
            message = str(exc).strip() or type(exc).__name__
            return {
                "ok": False,
                "error": message,
                "elapsed_ms": int((_time.monotonic() - started) * 1000),
            }

    # ── main entry point ──────────────────────────────────────────────
    async def parse_pdf(self, pdf_path: Path, out_dir: Path) -> ParsedDocument:
        self.check_config()
        out_dir = Path(out_dir)
        async with self._client() as client:
            parsed, raw = await self._run(client, Path(pdf_path), out_dir)
        self._write_cache(out_dir, parsed, raw)
        return parsed

    async def _run(
        self, client: httpx.AsyncClient, pdf_path: Path, out_dir: Path
    ) -> tuple[ParsedDocument, str]:
        """Call the vendor and return (normalized pages, raw payload)."""
        raise NotImplementedError

    def _write_cache(self, out_dir: Path, parsed: ParsedDocument, raw: str) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "raw.jsonl").write_text(raw, encoding="utf-8")
        parsed.metadata.setdefault("ocr_provider", self.name)
        save_pages(out_dir, parsed)

    # ── polling helper shared by async-job providers ─────────────────
    async def _sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
