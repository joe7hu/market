"""Request boundary for the local and private-network API."""

from __future__ import annotations

from ipaddress import ip_address, ip_network

from fastapi import HTTPException, Request


TAILSCALE_CGNAT = ip_network("100.64.0.0/10")


def _normalized_ip(value: str):
    address = ip_address(value)
    return getattr(address, "ipv4_mapped", None) or address


def require_local_request(request: Request) -> None:
    """Allow API access only from loopback, private LAN, link-local, or Tailscale."""

    host = request.client.host if request.client else ""
    if host in {"localhost", "testclient"}:
        return
    try:
        address = _normalized_ip(host)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="API access is available only from the local network.") from exc
    if address.is_loopback:
        forwarded = str(request.headers.get("x-forwarded-for") or "")
        if forwarded:
            try:
                chain = [_normalized_ip(item.strip()) for item in forwarded.split(",")]
            except ValueError as exc:
                raise HTTPException(status_code=403, detail="API access is available only from the local network.") from exc
            address = chain[-1]
    if not (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or (address.version == 4 and address in TAILSCALE_CGNAT)
    ):
        raise HTTPException(status_code=403, detail="API access is available only from the local network.")


__all__ = ["TAILSCALE_CGNAT", "require_local_request"]
