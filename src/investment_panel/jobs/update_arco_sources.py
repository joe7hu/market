"""Import Arco evidence files into compact PostgreSQL content facts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Any

from investment_panel.core.arco import flatten_arco_items, load_arco_context
from investment_panel.core.config import load_config
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.source_facts import SourceFactRepository


SOURCE_ID = "arco"
TOKEN_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9.]{0,9})|\b([A-Z][A-Z0-9.]{0,9})\b")


def run(config_path: str | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    runtime = runtime_for_config(config)
    repository = IngestionRepository(runtime)
    repository.register_source(
        SOURCE_ID,
        name="Arco evidence",
        family="research",
        kind="private_evidence",
        origin=str(config.arco.raw_dir),
        capabilities={"beliefs": True, "bookmarks": True, "source_evidence": True},
    )
    run_id = repository.start_run(SOURCE_ID, "content")
    try:
        context = load_arco_context(config.arco)
        payload_ids = _record_context_payloads(repository, run_id, context)
        known = _known_symbols(runtime)
        items = [_normalize(item, known) for item in flatten_arco_items(context)]
        items = [item for item in items if item is not None]
        counts = SourceFactRepository(runtime).store_content_items(
            run_id,
            SOURCE_ID,
            items,
            payload_id=payload_ids.get(str(context.get("manifest_path") or "")),
        )
        repository.finish_run(
            run_id,
            "succeeded",
            item_count=counts["items"],
            instrument_count=counts["instrument_links"],
            summary={"payload_manifests": len(payload_ids), "source_status": context.get("source_status")},
        )
        return {
            "status": "ok",
            "database": "postgresql",
            "run_id": str(run_id),
            "items": counts["items"],
            "instrument_links": counts["instrument_links"],
            "payload_manifests": len(payload_ids),
            "source_status": context.get("source_status"),
        }
    except Exception as exc:
        repository.finish_run(run_id, "failed", failure_detail=f"{type(exc).__name__}: {exc}")
        return {"status": "failed", "database": "postgresql", "items": 0, "error": str(exc)}


def _record_context_payloads(repository: IngestionRepository, run_id: Any, context: dict[str, Any]) -> dict[str, int]:
    paths = {
        str(value)
        for value in (
            context.get("brief_beliefs_path"), context.get("bookmarks_path"),
            context.get("web_captures_path"), context.get("manifest_path"),
            (context.get("source_status") or {}).get("signals_path"),
            (context.get("source_status") or {}).get("beliefs_path"),
            *(row.get("path") for row in context.get("source_snapshots") or []),
        )
        if value and Path(str(value)).is_file()
    }
    return {
        path: repository.record_payload_file(run_id, path, source_family="arco")
        for path in sorted(paths)
    }


def _normalize(item: dict[str, Any], known: set[str]) -> dict[str, Any] | None:
    text = str(item.get("text") or "").strip()
    title = str(item.get("title") or "").strip()
    if not text and not title:
        return None
    source_key = str(item.get("id") or "").strip()
    if not source_key:
        return None
    urls = sorted(_evidence_urls(item.get("evidence")))
    return {
        "source_key": source_key,
        "kind": str(item.get("source_type") or "arco_evidence"),
        "title": title[:1000] or None,
        "summary": text[:8000],
        "url": urls[0] if urls else None,
        "symbols": _symbols(f"{title} {text}", known),
        "license_status": "private_local_evidence",
        "metadata": {
            "score": item.get("score"),
            "evidence_urls": urls[:25],
            "raw_payload_location": "ingest.payload",
        },
    }


def _evidence_urls(value: Any) -> set[str]:
    urls: set[str] = set()
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, item in current.items():
                if key.lower() in {"url", "sourceurl", "canonicalurl"} and item:
                    urls.add(str(item))
                elif isinstance(item, (dict, list)):
                    stack.append(item)
        elif isinstance(current, list):
            stack.extend(current)
    return urls


def _known_symbols(runtime: Any) -> set[str]:
    with runtime.read() as connection:
        return {str(row["symbol"]) for row in connection.execute("SELECT symbol FROM catalog.instrument").fetchall()}


def _symbols(text: str, known: set[str]) -> list[str]:
    output: set[str] = set()
    for match in TOKEN_RE.finditer(text):
        symbol = str(match.group(1) or match.group(2) or "").upper()
        if match.group(1) or symbol in known:
            output.add(symbol)
    return sorted(output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    print(json.dumps(run(args.config), indent=2, default=str))


if __name__ == "__main__":
    main()
