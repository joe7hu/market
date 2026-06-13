"""Auto-split from raw_sources.py — see ARCHITECTURE.md."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    if not path.exists():
        return [], []
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(f"{path}:{line_number} malformed JSONL: {exc.msg}")
                    continue
                if isinstance(value, dict):
                    rows.append(value)
                else:
                    errors.append(f"{path}:{line_number} JSONL row is not an object")
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"{path} read failed: {type(exc).__name__}: {exc}")
    return rows, errors


def read_json_snapshot(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return {}, f"{path} is not available"
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except json.JSONDecodeError as exc:
        return {}, f"{path} is malformed JSON: {exc.msg}"
    except (OSError, UnicodeDecodeError) as exc:
        return {}, f"{path} read failed: {type(exc).__name__}: {exc}"
    if not isinstance(value, dict):
        return {}, f"{path} is not a JSON object"
    return value, ""


def snapshot_capture_items(snapshot: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    for collection in ("canonicalItems", "observedItems", "items"):
        if collection not in snapshot:
            continue
        values = snapshot.get(collection)
        if not isinstance(values, list):
            errors.append(f"{collection} is not a list")
            continue
        for index, item in enumerate(values):
            if isinstance(item, dict):
                items.append(item)
            else:
                errors.append(f"{collection}[{index}] is not an object")
    return items, errors


def load_profiles(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    for row in rows:
        profile_id = str(row.get("id") or "")
        handle = str(row.get("handle") or "")
        if profile_id:
            profiles[profile_id] = row
        if handle:
            profiles[handle.lower()] = row
    return profiles
