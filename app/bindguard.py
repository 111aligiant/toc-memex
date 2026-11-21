"""Refuse to expose an unauthenticated local service by accident.

Both web entry points serve the archive without authentication. Binding
them to a LAN or public interface is only safe behind a reverse proxy or
on a trusted network, so the default is loopback-only and the override has
to be explicit (``TOCMEMEX_ALLOW_NONLOCAL=1``).
"""

from __future__ import annotations

import ipaddress

from .config import get_settings


def is_loopback_host(host: str) -> bool:
    """True for literal loopback addresses and the ``localhost`` name."""
    candidate = (host or "").strip()
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    if candidate.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def check_bind_host(host: str, *, allow_nonlocal: bool) -> None:
    """Raise ``ValueError`` unless ``host`` is loopback or explicitly allowed."""
    if allow_nonlocal or is_loopback_host(host):
        return
    raise ValueError(
        f"拒绝绑定非本机地址 {host!r}。"
        "如需在局域网访问，请显式设置 TOCMEMEX_ALLOW_NONLOCAL=1；"
        "默认只允许 127.0.0.1 / localhost / ::1。"
    )


def enforce_bind_host(host: str) -> None:
    """Apply the bind policy and exit the CLI when it is violated.

    Shared by the web API, the inspector, and the MCP HTTP transport so a
    new entry point cannot silently forget the check. Reads the setting
    through ``Settings`` so ``TOCMEMEX_ALLOW_NONLOCAL`` works from the
    environment *and* from ``.env``, like every other knob.
    """
    allow_nonlocal = get_settings().tocmemex_allow_nonlocal
    try:
        check_bind_host(host, allow_nonlocal=allow_nonlocal)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if allow_nonlocal and not is_loopback_host(host):
        print(
            f"警告：TOCMEMEX_ALLOW_NONLOCAL=1，服务将绑定到 {host}。"
            "该服务没有鉴权，请确认网络边界。"
        )


# ── Host / Origin request guards (S2) ────────────────────────────────────
#
# Binding to loopback stops a *remote* host from connecting, but it does not
# stop DNS rebinding: a malicious page served from evil.example can re-resolve
# its own name to 127.0.0.1 and have the browser POST to the local service,
# because the browser still believes it is talking to evil.example (and sends
# ``Host: evil.example``). Checking the incoming ``Host`` header against the
# loopback names closes that hole. ``testserver`` is the FastAPI TestClient's
# synthetic host; it never leaves the test process, so it is safe to allow.

# Host header names that are allowed by default. A DNS-rebinding attacker can
# only supply a name they control, and that name is matched here by exact
# string after the port is stripped, so it is rejected.
_DEFAULT_ALLOWED_HOSTS = frozenset(
    {"localhost", "127.0.0.1", "::1", "testserver"}
)


def _host_only(value: str) -> str:
    """``[::1]:8791`` -> ``::1``; ``127.0.0.1:8791`` -> ``127.0.0.1``."""
    candidate = (value or "").strip()
    if candidate.startswith("["):
        end = candidate.find("]")
        if end != -1:
            return candidate[1:end].strip().lower()
    if candidate.count(":") == 1:
        return candidate.split(":", 1)[0].strip().lower()
    return candidate.lower()


def is_allowed_request_host(host_header: str | None) -> bool:
    """True when the ``Host`` header names a loopback/test host.

    When the operator has explicitly opted into a non-local deployment
    (``TOCMEMEX_ALLOW_NONLOCAL=1``) the DNS-rebinding guard cannot pin a
    loopback name, so any host is accepted there and the operator is
    responsible for the network boundary.
    """
    if get_settings().tocmemex_allow_nonlocal:
        return True
    name = _host_only(host_header or "")
    if not name:
        return False
    if name in _DEFAULT_ALLOWED_HOSTS:
        return True
    return is_loopback_host(name)


def is_allowed_origin(origin: str | None) -> bool:
    """True when ``origin`` is absent, ``null``, or a loopback origin.

    A cross-origin browser request carries the attacker's ``Origin``; rejecting
    it stops a hostile page from driving state-changing endpoints (CSRF) even
    if it somehow reaches the service. A non-browser client sends no Origin and
    is allowed (the service is local and unauthenticated by design).
    """
    if not origin:
        return True
    if get_settings().tocmemex_allow_nonlocal:
        return True
    candidate = origin.strip()
    if candidate.lower() == "null":  # sandboxed iframe / file://
        return False
    # origin is scheme://host[:port]; parse the host out.
    for scheme in ("http://", "https://"):
        if candidate.lower().startswith(scheme):
            host = _host_only(candidate[len(scheme):])
            return bool(host) and (host in _DEFAULT_ALLOWED_HOSTS or is_loopback_host(host))
    return False
