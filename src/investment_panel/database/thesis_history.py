"""Revision history helpers for thesis records."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def with_revision_diffs(revisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    previous: dict[str, Any] | None = None
    output: list[dict[str, Any]] = []
    for row in reversed(revisions):
        current = dict(row.get("thesis_json") or {})
        diff = {
            "from_revision": previous.get("revision") if previous else None,
            "changed_keys": sorted(
                key for key in set(current) | set(previous or {}) if current.get(key) != (previous or {}).get(key)
            ),
            "hash": hashlib.sha256(json.dumps(current, sort_keys=True, default=str).encode()).hexdigest()[:16],
        }
        next_row = dict(row)
        next_row["diff"] = diff
        output.append(next_row)
        previous = current | {"revision": row.get("revision")}
    return list(reversed(output))
