"""Refuse to expose an unauthenticated local service by accident.

Both web entry points serve the archive without authentication. Binding
them to a LAN or public interface is only safe behind a reverse proxy or
on a trusted network, so the default is loopback-only and the override has
to be explicit (``TOCMEMEX_ALLOW_NONLOCAL=1``).
"""

from __future__ import annotations

import ipaddress
import os


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
    new entry point cannot silently forget the check. Reads
    ``TOCMEMEX_ALLOW_NONLOCAL`` itself.
    """
    allow_nonlocal = os.environ.get("TOCMEMEX_ALLOW_NONLOCAL") == "1"
    try:
        check_bind_host(host, allow_nonlocal=allow_nonlocal)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if allow_nonlocal and not is_loopback_host(host):
        print(
            f"警告：TOCMEMEX_ALLOW_NONLOCAL=1，服务将绑定到 {host}。"
            "该服务没有鉴权，请确认网络边界。"
        )
