"""Request boundary for the local and private-network API."""

from __future__ import annotations

from ipaddress import ip_address, ip_network
from urllib.parse import urlsplit

from fastapi import HTTPException, Request


TAILSCALE_CGNAT = ip_network("100.64.0.0/10")
TAILSCALE_HOST_SUFFIX = ".tail46d3fb.ts.net"
ALLOWED_HOSTNAMES = {"localhost", "mini1.local"}


def _normalized_ip(value: str):
    address = ip_address(value)
    return getattr(address, "ipv4_mapped", None) or address


def _allowed_address(address) -> bool:
    return bool(
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or (address.version == 4 and address in TAILSCALE_CGNAT)
    )


def _valid_dns_labels(value: str) -> bool:
    return bool(value) and all(
        label.isascii()
        and 0 < len(label) <= 63
        and label[0].isalnum()
        and label[-1].isalnum()
        and all(char.isalnum() or char == "-" for char in label)
        for label in value.split(".")
    )


def _require_allowed_host(request: Request) -> None:
    host_values = request.headers.getlist("host")
    raw_host = str(host_values[0] if len(host_values) == 1 else "")
    try:
        if (
            not raw_host
            or raw_host != raw_host.strip()
            or raw_host.endswith(":")
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in raw_host)
        ):
            raise ValueError
        parsed = urlsplit(f"//{raw_host}")
        _ = parsed.port
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="API access is available only from an authorized host.") from exc

    hostname = parsed.hostname.rstrip(".").lower()
    if request.client and request.client.host == "testclient" and hostname == "testserver":
        return
    tailscale_name = hostname[: -len(TAILSCALE_HOST_SUFFIX)] if hostname.endswith(TAILSCALE_HOST_SUFFIX) else ""
    if hostname in ALLOWED_HOSTNAMES or _valid_dns_labels(tailscale_name):
        return
    try:
        address = _normalized_ip(hostname)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="API access is available only from an authorized host.") from exc
    if not _allowed_address(address):
        raise HTTPException(status_code=403, detail="API access is available only from an authorized host.")


def require_local_request(request: Request) -> None:
    """Allow API access only from loopback, private LAN, link-local, or Tailscale."""

    _require_allowed_host(request)
    host = request.client.host if request.client else ""
    if host == "testclient":
        return
    try:
        address = _normalized_ip("127.0.0.1" if host == "localhost" else host)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="API access is available only from the local network.") from exc
    if address.is_loopback:
        forwarded = str(request.headers.get("x-forwarded-for") or "")
        if forwarded:
            try:
                chain = [_normalized_ip(item.strip()) for item in forwarded.split(",")]
            except ValueError as exc:
                raise HTTPException(status_code=403, detail="API access is available only from the local network.") from exc
            if any(not _allowed_address(item) for item in chain):
                raise HTTPException(
                    status_code=403,
                    detail="API access is available only from the local network.",
                )
            address = chain[0]
    if not _allowed_address(address):
        raise HTTPException(status_code=403, detail="API access is available only from the local network.")


__all__ = ["TAILSCALE_CGNAT", "require_local_request"]
