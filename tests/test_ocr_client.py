from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.ocr.client import PaddleOcrClient

FIXTURE = Path(__file__).parent / "fixtures" / "paddleocr_sample.jsonl"
pytestmark = pytest.mark.skipif(
    not FIXTURE.exists(),
    reason="OCR fixtures are not tracked; see tests/fixtures/README.md",
)
RAW = FIXTURE.read_text(encoding="utf-8") if FIXTURE.exists() else ""


@pytest.mark.asyncio
async def test_parse_pdf_with_mock_transport(tmp_path):
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if request.method == "POST":
            return httpx.Response(200, json={"data": {"jobId": "job-1"}})
        if request.url.path.endswith("/job-1"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "state": "done",
                        "resultUrl": {"jsonUrl": "https://example.test/result.jsonl"},
                    }
                },
            )
        if str(request.url) == "https://example.test/result.jsonl":
            return httpx.Response(200, text=RAW)
        return httpx.Response(404)

    settings = Settings(
        paddleocr_api_token="t",
        paddleocr_api_url="https://example.test/jobs",
    )
    client = PaddleOcrClient(settings, transport=httpx.MockTransport(handler))
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    parsed = await client.parse_pdf(pdf, tmp_path / "ocr")

    assert len(parsed.pages) == 4
    assert (tmp_path / "ocr" / "raw.jsonl").exists()
    assert any(method == "POST" for method, _ in calls)


@pytest.mark.asyncio
async def test_submit_retries_transient_500(tmp_path, monkeypatch):
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(500, text="server error")
        return httpx.Response(200, json={"data": {"jobId": "job-1"}})

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("app.ocr.providers.base.asyncio.sleep", no_sleep)
    settings = Settings(
        paddleocr_api_token="t",
        paddleocr_api_url="https://example.test/jobs",
    )
    client = PaddleOcrClient(settings, transport=httpx.MockTransport(handler))
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    assert await client.submit(pdf) == "job-1"
    assert attempts == 2