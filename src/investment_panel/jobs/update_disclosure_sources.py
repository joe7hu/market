"""Ingest configured public disclosure files into PostgreSQL."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
from typing import Any

import yaml

from investment_panel.core.config import load_config, resolve_path
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.ingestion import IngestionRepository


def run(config_path: str | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    sources = _configured_csvs(config_path)
    runtime = runtime_for_config(config)
    results = [_ingest_csv(runtime, source) for source in sources]
    return {
        "status": "partial" if any(row["status"] != "ok" for row in results) else "ok",
        "database": "postgresql",
        "files_configured": len(sources),
        "rows_ingested": sum(int(row.get("rows") or 0) for row in results),
        "runs": results,
    }


def _ingest_csv(runtime: Any, source: dict[str, Any]) -> dict[str, Any]:
    path = Path(source["path"])
    source_id = _slug(f"disclosure_csv_{source['trader_name']}_{path.stem}")
    repository = IngestionRepository(runtime)
    repository.register_source(
        source_id,
        name=f"{source['trader_name']} disclosures",
        family="disclosures",
        kind="public_csv",
        origin=path.resolve().as_uri(),
        capabilities={"disclosures": True},
    )
    run_id = repository.start_run(source_id, "disclosures")
    if not path.is_file():
        repository.finish_run(run_id, "failed", failure_detail=f"file not found: {path}")
        return {"source_id": source_id, "status": "failed", "rows": 0, "error": f"file not found: {path}"}
    try:
        payload_id = repository.record_payload_file(run_id, path, source_kind=source.get("source_kind"))
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = [_normalize(row, source) for row in csv.DictReader(handle)]
        rows = [row for row in rows if row is not None]
        count = repository.store_disclosures(run_id, source_id, rows, payload_id=payload_id)
        repository.finish_run(run_id, "succeeded", item_count=count, summary={"source_file": path.name})
        return {"source_id": source_id, "status": "ok", "rows": count}
    except Exception as exc:
        repository.finish_run(run_id, "failed", failure_detail=f"{type(exc).__name__}: {exc}")
        return {"source_id": source_id, "status": "failed", "rows": 0, "error": str(exc)}


def _normalize(row: dict[str, Any], source: dict[str, Any]) -> dict[str, Any] | None:
    symbol = str(row.get("symbol") or row.get("ticker") or "").strip().upper()
    event_date = row.get("transaction_date") or row.get("event_date") or row.get("date")
    action = str(row.get("transaction_type") or row.get("type") or row.get("action") or "").strip().upper()
    if not symbol or not event_date or not action:
        return None
    identity = "|".join(
        str(value or "")
        for value in (
            source.get("trader_name"), symbol, event_date, action,
            row.get("amount") or row.get("amount_range"), row.get("source_url") or row.get("url"),
        )
    )
    return {
        "source_key": str(row.get("id") or hashlib.sha256(identity.encode()).hexdigest()),
        "source_type": str(source.get("source_type") or "public_disclosure_transaction"),
        "trader_name": row.get("trader_name") or source.get("trader_name"),
        "filer_name": row.get("filer_name") or source.get("filer_name"),
        "symbol": symbol,
        "event_date": event_date,
        "filed_date": row.get("filed_date") or row.get("filing_date") or event_date,
        "action": action,
        "amount_text": row.get("amount") or row.get("amount_range"),
        "source_url": row.get("source_url") or row.get("url"),
        "details": {key: value for key, value in row.items() if value not in (None, "")},
    }


def _configured_csvs(config_path: str | None) -> list[dict[str, Any]]:
    path = resolve_path(config_path or "config.yaml")
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8") as handle:
        disclosures = dict((yaml.safe_load(handle) or {}).get("disclosures") or {})
    sources: list[dict[str, Any]] = []
    for row in disclosures.get("public_disclosure_csvs") or []:
        sources.extend(_source_rows(row, path.parent, {}))
    for trader in disclosures.get("tracked_traders") or []:
        defaults = {
            "trader_name": trader.get("trader_name") or trader.get("name") or "Tracked Trader",
            "filer_name": trader.get("filer_name") or trader.get("name") or "Public disclosure",
            "source_kind": trader.get("source_kind") or "public_disclosure",
        }
        for row in trader.get("daily_csvs") or trader.get("incremental_csvs") or []:
            sources.extend(_source_rows(row, path.parent, defaults))
    return sources


def _source_rows(row: Any, base: Path, defaults: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(row, str):
        row = {"path": row}
    if not isinstance(row, dict) or not row.get("path"):
        return []
    return [{
        "path": str(resolve_path(row["path"], base)),
        "trader_name": row.get("trader_name") or row.get("name") or defaults.get("trader_name") or "Tracked Trader",
        "filer_name": row.get("filer_name") or defaults.get("filer_name") or "Public disclosure",
        "source_type": row.get("source_type") or "public_disclosure_transaction",
        "source_kind": row.get("source_kind") or defaults.get("source_kind") or "public_disclosure",
    }]


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "disclosure"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    print(json.dumps(run(args.config), indent=2, default=str))


if __name__ == "__main__":
    main()
