"""Fail-closed hydration for fundamental history stored on the NAS."""

from __future__ import annotations

import gzip
from hashlib import sha256
import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


def hydrate_history(
    values: dict[str, Any], *, archive_uri: str | None, archive_sha256: str | None,
) -> dict[str, Any]:
    """Return values with verified history, or explicit degraded health.

    This reader never invents a history series.  It accepts only local NAS
    file URIs recorded in ``ingest.payload`` and verifies the compressed
    artifact before decoding it.
    """

    hydrated = dict(values)
    if isinstance(hydrated.get("history"), list):
        hydrated["history_data_health"] = {"status": "live"}
        return hydrated
    if not archive_uri or not archive_sha256:
        hydrated["history_data_health"] = {"status": "missing_archive_reference"}
        return hydrated
    parsed = urlparse(archive_uri)
    if parsed.scheme != "file":
        hydrated["history_data_health"] = {"status": "degraded", "detail": "unsupported_archive_uri"}
        return hydrated
    path = Path(unquote(parsed.path))
    try:
        digest = sha256(path.read_bytes()).hexdigest()
        if digest != archive_sha256:
            raise ValueError("sha256_mismatch")
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            history = json.load(handle)
        if not isinstance(history, list):
            raise ValueError("history_not_array")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        hydrated["history_data_health"] = {"status": "degraded", "detail": str(exc)}
        return hydrated
    hydrated["history"] = history
    hydrated["history_data_health"] = {"status": "archived_verified"}
    return hydrated
