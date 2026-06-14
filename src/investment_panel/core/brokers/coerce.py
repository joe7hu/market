"""JSON, datetime, network, and id helpers."""

from __future__ import annotations
import socket

from investment_panel.core.coercion import parse_dt_utc as parse_dt
from investment_panel.core.coercion import parse_json, stable_id

__all__ = ["parse_json", "parse_dt", "tcp_open", "stable_id"]


def tcp_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
