"""PaddleOCR-VL cloud jobs (the original provider).

Kept byte-compatible with the previous ``app.ocr.client.PaddleOcrClient``
API (``submit`` / ``poll`` / ``fetch`` / ``parse_pdf``) so existing tests
and callers keep working; only the cache write is new (``pages.json``).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from ...config import Settings
from ..models import ParsedDocument
from ..parser import parse_jsonl
from .base import BaseOcrProvider, OcrProviderError

_OPTIONAL_PAYLOAD = json.dumps(
    {
        "useDocOrientationClassify": False,
        "useDocUnwarping": False,
        "useChartRecognition": False,
    }
)


class PaddleOcrError(OcrProviderError):
    pass


def _retry_after_seconds(response: httpx.Response, default: float = 60.0) -> float:
    """The server's Retry-After, in seconds, or a conservative default."""
    raw = response.headers.get("retry-after")
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


class PaddleOcrProvider(BaseOcrProvider):
    name = "paddle"

    def check_config(self) -> None:
        if not self.settings.paddleocr_api_token:
            raise PaddleOcrError("PADDLEOCR_API_TOKEN is not configured")

    @property
    def poll_timeout(self) -> int:
        override = self.settings.paddleocr_poll_timeout
        return override if override is not None else self.settings.tocmemex_ocr_poll_timeout

    @property
    def poll_interval(self) -> int:
        override = self.settings.paddleocr_poll_interval
        return override if override is not None else self.settings.tocmemex_ocr_poll_interval

    def _headers(self) -> dict[str, str]:
        self.check_config()
        return {"Authorization": f"bearer {self.settings.paddleocr_api_token}"}

    def _probe_target(self) -> tuple[str, dict[str, str]]:
        # No public catalog: the jobs URL itself answers (404 = reachable).
        return self.settings.paddleocr_api_url, self._headers()

    async def _submit(self, client: httpx.AsyncClient, pdf_path: Path) -> str:
        data = {
            "model": self.settings.paddleocr_api_model,
            "optionalPayload": _OPTIONAL_PAYLOAD,
        }
        # The vendor's submit endpoint returns transient 5xx errors under
        # load. The old budget (3 tries, 2s + 4s) gave up after ~6 seconds,
        # so a 20-second outage always failed and the user paid for a full
        # OCR run again. Back off up to 60s between tries instead; 429 gets
        # its own, longer treatment because it means "slow down", not "retry
        # immediately".
        backoff = (2, 4, 8, 16, 32)
        max_delay = 60
        last: Exception | None = None
        attempt = 0
        while True:
            response: httpx.Response | None = None
            try:
                await self._guard_url(self.settings.paddleocr_api_url)
                with pdf_path.open("rb") as handle:
                    files = {"file": (pdf_path.name, handle, "application/pdf")}
                    response = await client.post(
                        self.settings.paddleocr_api_url,
                        headers=self._headers(),
                        data=data,
                        files=files,
                    )
            except httpx.HTTPError as exc:
                last = exc
            else:
                if response.status_code == 200:
                    job_id = (response.json().get("data") or {}).get("jobId")
                    if not job_id:
                        raise PaddleOcrError("submit response missing data.jobId")
                    return str(job_id)
                if response.status_code == 429:
                    # Rate limited: honour Retry-After when the vendor sends
                    # one, otherwise wait a full minute before trying again.
                    retry_after = _retry_after_seconds(response)
                    last = PaddleOcrError(
                        f"submit failed HTTP 429: {response.text[:200]}"
                    )
                    if attempt >= len(backoff):
                        raise PaddleOcrError(
                            f"submit failed after retries: {last}"
                        )
                    await self._sleep(min(retry_after, max_delay))
                    attempt += 1
                    continue
                if response.status_code < 500:
                    # A 4xx other than 429 will not fix itself on retry.
                    raise PaddleOcrError(
                        f"submit failed HTTP {response.status_code}: "
                        f"{response.text[:200]}"
                    )
                last = PaddleOcrError(
                    f"submit failed HTTP {response.status_code}: "
                    f"{response.text[:200]}"
                )
            if attempt >= len(backoff):
                raise PaddleOcrError(f"submit failed after retries: {last}")
            await self._sleep(min(backoff[attempt], max_delay))
            attempt += 1

    async def _poll(self, client: httpx.AsyncClient, job_id: str) -> str:
        url = f"{self.settings.paddleocr_api_url.rstrip('/')}/{job_id}"
        await self._guard_url(url)
        deadline = time.monotonic() + self.poll_timeout
        while True:
            try:
                resp = await client.get(url, headers=self._headers())
            except httpx.HTTPError as exc:
                if time.monotonic() >= deadline:
                    raise PaddleOcrError(f"poll failed: {exc}") from exc
                await self._sleep(self.poll_interval)
                continue
            if resp.status_code >= 500:
                # Transient server error: keep polling until the deadline.
                if time.monotonic() >= deadline:
                    raise PaddleOcrError(f"poll failed HTTP {resp.status_code}")
                await self._sleep(self.poll_interval)
                continue
            if resp.status_code != 200:
                raise PaddleOcrError(f"poll failed HTTP {resp.status_code}")
            data = resp.json().get("data") or {}
            state = data.get("state")
            if state == "done":
                json_url = (data.get("resultUrl") or {}).get("jsonUrl")
                if not json_url:
                    raise PaddleOcrError("job done but missing resultUrl.jsonUrl")
                return str(json_url)
            if state == "failed":
                detail = data.get("errorMsg") or "unknown"
                raise PaddleOcrError(f"job failed (job {job_id}): {detail}")
            if time.monotonic() >= deadline:
                raise PaddleOcrError("poll timeout")
            await self._sleep(self.poll_interval)

    async def _fetch(self, client: httpx.AsyncClient, json_url: str) -> str:
        # json_url comes from the vendor response, not our config: guard it.
        await self._guard_url(json_url)
        last: Exception | None = None
        for attempt in range(3):
            try:
                resp = await client.get(json_url)
                if resp.status_code == 200:
                    return resp.text
                last = PaddleOcrError(f"fetch failed HTTP {resp.status_code}")
            except httpx.HTTPError as exc:
                last = exc
            if attempt < 2:
                await self._sleep(attempt + 1)
        raise PaddleOcrError(f"fetch failed: {last}")

    # Public single-step API (kept for existing callers/tests).
    async def submit(self, pdf_path: Path) -> str:
        async with self._client() as client:
            return await self._submit(client, pdf_path)

    async def poll(self, job_id: str) -> str:
        async with self._client() as client:
            return await self._poll(client, job_id)

    async def fetch(self, json_url: str) -> str:
        async with self._client() as client:
            return await self._fetch(client, json_url)

    async def _run(
        self, client: httpx.AsyncClient, pdf_path: Path, out_dir: Path
    ) -> tuple[ParsedDocument, str]:
        job_id = await self._submit(client, pdf_path)
        json_url = await self._poll(client, job_id)
        raw = await self._fetch(client, json_url)
        parsed = ParsedDocument(
            pages=parse_jsonl(raw),
            bookmarks=[],
            metadata={
                "ocr_provider": self.name,
                "ocr_model": self.settings.paddleocr_api_model,
                "job_id": job_id,
            },
        )
        return parsed, raw
