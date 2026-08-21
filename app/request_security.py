"""Request boundary for local and private-network mutations."""

from __future__ import annotations

from ipaddress import ip_address, ip_network

from fastapi import HTTPException, Request


TAILSCALE_CGNAT = ip_network("100.64.0.0/10")


def require_local_request(request: Request) -> None:
    """Allow writes only from loopback, private LAN, link-local, or Tailscale."""

    host = request.client.host if request.client else ""
    if host in {"localhost", "testclient"}:
        return
    try:
        address = ip_address(host)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Write actions are available only from the local network.") from exc
    if not (address.is_loopback or address.is_private or address.is_link_local or address in TAILSCALE_CGNAT):
        raise HTTPException(status_code=403, detail="Write actions are available only from the local network.")


__all__ = ["TAILSCALE_CGNAT", "require_local_request"]
