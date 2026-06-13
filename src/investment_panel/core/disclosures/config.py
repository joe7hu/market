"""Auto-split from core/disclosures.py — see ARCHITECTURE.md."""
from __future__ import annotations

from pathlib import Path
from typing import Any
import yaml
from investment_panel.core.config import resolve_path


def load_13f_trackers_from_config(config_path: str | Path | None = None) -> list[dict[str, Any]]:
    path = resolve_path(config_path or "config.yaml")
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return extract_13f_trackers(raw)


def extract_13f_trackers(raw_config: dict[str, Any]) -> list[dict[str, Any]]:
    disclosures = raw_config.get("disclosures") or {}
    global_ticker_map = normalize_13f_ticker_map(
        disclosures.get("13f_ticker_map")
        or disclosures.get("thirteen_f_ticker_map")
        or raw_config.get("13f_ticker_map")
        or raw_config.get("thirteen_f_ticker_map")
        or {}
    )
    tracker_rows = (
        disclosures.get("13f_trackers")
        or disclosures.get("thirteen_f_trackers")
        or raw_config.get("13f_trackers")
        or raw_config.get("thirteen_f_trackers")
        or []
    )
    trackers: list[dict[str, Any]] = []
    for row in tracker_rows:
        if not isinstance(row, dict) or not row.get("cik"):
            continue
        cik = str(row["cik"]).strip()
        trackers.append(
            {
                "cik": cik,
                "name": row.get("name") or row.get("trader_name") or row.get("filer_name") or cik,
                "max_filings": row.get("max_filings"),
                "ticker_map": global_ticker_map | normalize_13f_ticker_map(row.get("ticker_map") or row.get("cusip_ticker_map") or {}),
            }
        )
    return trackers


def normalize_13f_ticker_map(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, value in raw.items():
        cusip = normalize_cusip(key)
        symbol = str(value or "").strip().upper()
        if cusip and symbol:
            normalized[cusip] = symbol
    return normalized


def normalize_cusip(value: Any) -> str:
    return str(value or "").strip().upper().replace(" ", "")


def load_public_disclosure_csvs_from_config(config_path: str | Path | None = None) -> list[dict[str, Any]]:
    path = resolve_path(config_path or "config.yaml")
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return extract_public_disclosure_csvs(raw, path.parent)


def load_tracked_traders_from_config(config_path: str | Path | None = None) -> list[dict[str, Any]]:
    path = resolve_path(config_path or "config.yaml")
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return extract_tracked_traders(raw, path.parent)


def extract_public_disclosure_csvs(raw_config: dict[str, Any], base: Path | None = None) -> list[dict[str, Any]]:
    disclosures = raw_config.get("disclosures") or {}
    source_rows = disclosures.get("public_disclosure_csvs") or raw_config.get("public_disclosure_csvs") or []
    sources = disclosure_csv_sources(source_rows, base or Path.cwd())
    for trader in extract_tracked_traders(raw_config, base):
        sources.extend(trader.get("daily_csvs", []))
    return sources


def extract_tracked_traders(raw_config: dict[str, Any], base: Path | None = None) -> list[dict[str, Any]]:
    disclosures = raw_config.get("disclosures") or {}
    trader_rows = disclosures.get("tracked_traders") or raw_config.get("tracked_traders") or []
    traders: list[dict[str, Any]] = []
    root = base or Path.cwd()
    for row in trader_rows:
        if not isinstance(row, dict) or not (row.get("name") or row.get("trader_name")):
            continue
        trader_name = str(row.get("trader_name") or row.get("name")).strip()
        filer_name = row.get("filer_name") or row.get("source_name") or trader_name
        default_source = {
            "trader_name": trader_name,
            "filer_name": filer_name,
            "source_kind": row.get("source_kind") or row.get("source_type") or "public_disclosure",
        }
        traders.append(
            {
                "trader_name": trader_name,
                "filer_name": filer_name,
                "source_kind": default_source["source_kind"],
                "official_house": row.get("official_house") or row.get("house") or {},
                "benchmark": row.get("benchmark") or {},
                "historical_csvs": disclosure_csv_sources(row.get("historical_csvs") or [], root, default_source),
                "daily_csvs": disclosure_csv_sources(row.get("daily_csvs") or row.get("incremental_csvs") or [], root, default_source),
            }
        )
    return traders


def disclosure_csv_sources(
    source_rows: list[Any],
    root: Path,
    defaults: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for row in source_rows:
        if isinstance(row, str):
            row = {"path": row}
        if not isinstance(row, dict) or not row.get("path"):
            continue
        row_defaults = defaults or {}
        sources.append(
            {
                "path": str(resolve_path(row["path"], root)),
                "trader_name": row.get("trader_name") or row.get("name") or row_defaults.get("trader_name") or "Tracked Trader",
                "source_type": row.get("source_type") or "public_disclosure_transaction",
                "filer_name": row.get("filer_name")
                or row.get("name")
                or row.get("trader_name")
                or row_defaults.get("filer_name")
                or "Public disclosure",
                "source_kind": row.get("source_kind") or row_defaults.get("source_kind") or "public_disclosure",
            }
        )
    return sources
