"""Outbound URL guard for OCR provider traffic.

The OCR endpoint ``base_url`` is writable through the local web API, and the
service has no login (see README): anyone who can reach the port could point
an endpoint at an internal address and make the server fetch it — with the
stored ``api_key`` attached. Every outbound OCR request therefore goes
through :func:`assert_public_url` first.

Local / private deployments (self-hosted PaddleX, MinerU) are still
supported by opting in with ``TOCMEMEX_OCR_ALLOW_PRIVATE_HOSTS=1``; that
also permits plain ``http`` so a LAN box needs no certificate.

Limitation: the hostname is resolved here and again by httpx, so a DNS
rebinding attacker can win that race. Pinning the socket to the resolved
address would close it but breaks proxies and keep-alive; this guard stops
the straightforward "point the endpoint at 127.0.0.1 / 169.254.169.254"
attack, which is the one reachable from the unauthenticated API.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse


class OutboundUrlError(ValueError):
    """An outbound URL is malformed or points at a non-public address."""


def _normalize_host(host: str) -> str:
    return (host or "").strip().rstrip(".").lower()


def _parse_ip(host: str) -> ipaddress._BaseAddress | None:
    """The literal IP in ``host``, or None when it is a DNS name."""
    # IPv6 zone ids ("fe80::1%eth0") are not accepted by ipaddress.
    candidate = host.split("%", 1)[0]
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def _is_non_public(ip: ipaddress._BaseAddress) -> bool:
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
        or ip.is_multicast
    )


def is_obviously_private_url(url: str) -> bool:
    """True when the URL names a loopback/private address *literally*.

    Synchronous and DNS-free, for call sites that must not pay a lookup
    (image downloads from vendor responses). It catches
    ``http://127.0.0.1:8080`` and ``http://169.254.169.254/...`` but not an
    internal hostname; :func:`assert_public_url` is the stronger check.
    """
    try:
        host = _normalize_host(urlparse(url).hostname or "")
    except ValueError:
        return True
    if not host:
        return True
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        return True
    ip = _parse_ip(host)
    return ip is not None and _is_non_public(ip)


async def assert_public_url(
    url: str, *, allow_private: bool = False, resolve: bool = True
) -> None:
    """Raise OutboundUrlError unless ``url`` is safe to request.

    Requires https for public traffic; ``allow_private`` relaxes both the
    address class and the scheme so LAN deployments keep working.
    ``resolve=False`` skips the DNS check (used when a custom httpx
    transport handles the request, e.g. tests, so no real lookup happens).
    """
    try:
        parsed = urlparse(url)
        host = _normalize_host(parsed.hostname or "")
    except ValueError as exc:
        raise OutboundUrlError(f"invalid URL {url!r}: {exc}") from exc

    if parsed.scheme not in {"http", "https"}:
        raise OutboundUrlError(
            f"unsupported URL scheme {parsed.scheme!r}; use http or https"
        )
    if parsed.username or parsed.password:
        raise OutboundUrlError("credentials in the URL are not allowed")
    if not host:
        raise OutboundUrlError(f"URL has no host: {url!r}")
    if not allow_private and parsed.scheme != "https":
        raise OutboundUrlError(
            f"refusing plain-http request to {host!r}; https is required"
        )

    literal = _parse_ip(host)
    if literal is not None:
        if not allow_private and _is_non_public(literal):
            raise OutboundUrlError(f"refusing request to non-public address {host}")
        return

    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        if not allow_private:
            raise OutboundUrlError(f"refusing request to non-public host {host!r}")
        return

    if not resolve:
        return

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OutboundUrlError(f"cannot resolve {host!r}: {exc}") from exc
    if not infos:
        raise OutboundUrlError(f"cannot resolve {host!r}")
    for info in infos:
        address = str(info[4][0]).split("%", 1)[0]
        try:
            resolved = ipaddress.ip_address(address)
        except ValueError:
            raise OutboundUrlError(f"{host!r} resolved to unparseable address {address}")
        if not allow_private and _is_non_public(resolved):
            raise OutboundUrlError(
                f"{host!r} resolves to non-public address {address}"
            )


__all__ = [
    "OutboundUrlError",
    "assert_public_url",
    "is_obviously_private_url",
]
