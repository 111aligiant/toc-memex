"""Outbound URL guard: SSRF and local-file-read defenses."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.ocr.netguard import (
    OutboundUrlError,
    assert_public_url,
    is_obviously_private_url,
)
from app.ocr.providers import MistralOcrProvider


# ── static checks (no DNS) ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8791/api",
        "https://127.0.0.1/x",
        "http://localhost:8080/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/jobs",
        "http://192.168.1.1/jobs",
        "https://[::1]/x",
        "https://[fe80::1]/x",
        "http://0.0.0.0/x",
        "file:///etc/passwd",
        "ftp://example.com/x",
        "https://user:pw@example.com/x",
    ],
)
async def test_assert_public_url_rejects_non_public_targets(url):
    with pytest.raises(OutboundUrlError):
        await assert_public_url(url, resolve=False)


async def test_assert_public_url_allows_public_https():
    # No DNS: only the static shape is checked.
    await assert_public_url("https://api.mistral.ai/v1/ocr", resolve=False)


async def test_assert_public_url_requires_https_for_public_hosts():
    with pytest.raises(OutboundUrlError, match="https is required"):
        await assert_public_url("http://api.mistral.ai/v1/ocr", resolve=False)


async def test_allow_private_permits_lan_http():
    """Self-hosted PaddleX / MinerU on a LAN still work when opted in."""
    await assert_public_url(
        "http://192.168.1.20:8080/api/v2/ocr/jobs",
        allow_private=True,
        resolve=False,
    )
    await assert_public_url(
        "http://127.0.0.1:8080/jobs", allow_private=True, resolve=False
    )


async def test_allow_private_still_rejects_bad_scheme_and_credentials():
    with pytest.raises(OutboundUrlError):
        await assert_public_url("file:///etc/passwd", allow_private=True)
    with pytest.raises(OutboundUrlError):
        await assert_public_url(
            "http://u:p@127.0.0.1/x", allow_private=True, resolve=False
        )


def test_is_obviously_private_url_is_dns_free():
    assert is_obviously_private_url("http://127.0.0.1:9/img.jpg")
    assert is_obviously_private_url("http://169.254.169.254/x.jpg")
    assert is_obviously_private_url("http://localhost/x.jpg")
    assert is_obviously_private_url("http://[::1]/x.jpg")
    assert not is_obviously_private_url("https://cdn.example.com/x.jpg")


# ── provider wiring ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_reports_ssrf_refusal_instead_of_requesting():
    """The '测试' button must not reach an internal address."""
    provider = MistralOcrProvider(
        Settings(
            tocmemex_ocr_provider="mistral",
            mistral_api_key="k",
            mistral_api_url="http://127.0.0.1:8791/v1/ocr",
            # Pin the default so a developer's .env opt-in does not turn this
            # guard test into a no-op.
            tocmemex_ocr_allow_private_hosts=False,
        )
    )
    result = await provider.probe()
    assert result["ok"] is False
    assert "non-public" in result["error"] or "https" in result["error"]


@pytest.mark.asyncio
async def test_provider_guard_honours_allow_private_setting():
    provider = MistralOcrProvider(
        Settings(
            tocmemex_ocr_provider="mistral",
            mistral_api_key="k",
            mistral_api_url="http://127.0.0.1:9/v1/ocr",
            tocmemex_ocr_allow_private_hosts=True,
        )
    )
    # Guard passes; the probe then fails on connection refused, not on SSRF.
    result = await provider.probe()
    assert result["ok"] is False
    assert "non-public" not in result["error"]
