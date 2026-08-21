"""Mechanical checks for the generated backend/frontend HTTP contract."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.generate_openapi import rendered_schema


ROOT = Path(__file__).resolve().parent.parent
OPENAPI_PATH = ROOT / "frontend" / "src" / "generated" / "openapi.json"
TYPESCRIPT_PATH = ROOT / "frontend" / "src" / "generated" / "apiSchema.ts"


def _contract() -> dict:
    return json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))


def test_generated_openapi_is_current() -> None:
    assert OPENAPI_PATH.read_text(encoding="utf-8") == rendered_schema()
    generated_types = TYPESCRIPT_PATH.read_text(encoding="utf-8")
    assert "export interface paths" in generated_types
    assert "export interface components" in generated_types


def test_openapi_baseline_and_json_success_schemas() -> None:
    contract = _contract()
    paths = contract["paths"]
    operations = [
        operation
        for path in paths.values()
        for method, operation in path.items()
        if method in {"get", "post", "put", "patch", "delete"}
    ]
    assert len(paths) <= 90
    assert operations
    assert contract["components"]["schemas"]

    missing: list[str] = []
    for path, path_item in paths.items():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            for status, response in operation.get("responses", {}).items():
                if not status.startswith("2"):
                    continue
                for media_type, payload in response.get("content", {}).items():
                    if media_type == "application/json" and "schema" not in payload:
                        missing.append(f"{method.upper()} {path} {status}")
    assert not missing, "JSON success responses need an OpenAPI schema:\n  " + "\n  ".join(missing)


def test_json_success_responses_use_named_backend_models() -> None:
    contract = _contract()
    unnamed: list[str] = []
    for path, path_item in contract["paths"].items():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            for status, response in operation.get("responses", {}).items():
                if not status.startswith("2"):
                    continue
                schema = response.get("content", {}).get("application/json", {}).get("schema")
                if schema and "$ref" not in schema:
                    unnamed.append(f"{method.upper()} {path} {status}")
    assert not unnamed, "JSON success responses must reference named schemas:\n  " + "\n  ".join(unnamed)
