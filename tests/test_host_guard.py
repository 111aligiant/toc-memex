"""S2: Host / Origin request guard (DNS rebinding + CSRF defense)."""

from __future__ import annotations

import pytest

from app.bindguard import is_allowed_origin, is_allowed_request_host
from app.web_api import create_app
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _loopback_only(monkeypatch):
    # Force the default (non-opted-in) policy so the guard is active in tests.
    from app import bindguard

    monkeypatch.setattr(
        bindguard.get_settings(), "tocmemex_allow_nonlocal", False, raising=False
    )


# ── unit: Host ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.0.0.1:8791", "localhost", "localhost:8791",
     "::1", "[::1]:8791", "testserver"],
)
def test_loopback_hosts_allowed(host):
    assert is_allowed_request_host(host) is True


@pytest.mark.parametrize(
    "host",
    ["evil.example", "evil.example:80", "attacker.test:8791", "", None,
     "10.0.0.5", "192.168.1.10:8791"],
)
def test_foreign_hosts_rejected(host):
    assert is_allowed_request_host(host) is False


# ── unit: Origin ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "origin",
    [None, "", "http://127.0.0.1:8791", "http://localhost:8791", "http://[::1]:8791"],
)
def test_loopback_or_absent_origins_allowed(origin):
    assert is_allowed_origin(origin) is True


@pytest.mark.parametrize(
    "origin",
    ["http://evil.example", "https://attacker.test", "null"],
)
def test_foreign_origins_rejected(origin):
    assert is_allowed_origin(origin) is False


# ── HTTP level: DNS-rebinding Host is refused ────────────────────────────


def _client(tmp_path):
    app = create_app(tmp_path / "tocmemex.db", tmp_path)
    return TestClient(app)


def test_guard_blocks_rebinding_host_header(tmp_path):
    with _client(tmp_path) as client:
        # A rebound page would send its own name as Host.
        response = client.get("/api/namespaces", headers={"host": "evil.example"})
        assert response.status_code == 400
        # The normal loopback host still works.
        ok = client.get("/api/namespaces", headers={"host": "127.0.0.1:8791"})
        assert ok.status_code == 200


def test_guard_blocks_cross_origin_write(tmp_path):
    with _client(tmp_path) as client:
        # Cross-site POST (CSRF) from a foreign origin is refused before routing.
        blocked = client.post(
            "/api/llm-endpoints",
            json={"name": "x", "base_url": "http://x/v1", "model": "m"},
            headers={"origin": "http://evil.example"},
        )
        assert blocked.status_code == 400
        # Same-origin write is still allowed (loopback origin).
        allowed = client.post(
            "/api/llm-endpoints",
            json={"name": "x", "base_url": "http://x/v1", "model": "m"},
            headers={"origin": "http://127.0.0.1:8791"},
        )
        assert allowed.status_code != 400
