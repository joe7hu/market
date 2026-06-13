"""JSON, datetime, network, and id helpers."""

from __future__ import annotations
from datetime import UTC, datetime, timedelta
import hashlib
import json
import socket
from typing import Any, Protocol



def parse_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return {}
    try:
        return json.loads(value)
    except Exception:
        return {}




def parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.astimezone(UTC)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.astimezone(UTC)




def tcp_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False




def stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
