"""Ingest configured public disclosure files into PostgreSQL."""

from __future__ import annotations

import argparse
import csv
from datetime import date
import hashlib
import json
from pathlib import Path
import re
from typing import Any
import xml.etree.ElementTree as ET

import httpx
import yaml

from investment_panel.core.config import load_config, resolve_path
from investment_panel.core.house_disclosures import (
    fetch_house_pdf_bytes,
    parse_house_disclosure_text,
    parse_house_pdf_bytes,
    search_house_member_filings,
)
from investment_panel.core import sec
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.source_facts import SourceFactRepository


def run(config_path: str | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    sources = _configured_csvs(config_path)
    traders = _configured_house_traders(config_path)
    trackers = _configured_13f_trackers(config_path)
    runtime = runtime_for_config(config)
    results = [_ingest_csv(runtime, source) for source in sources]
    results.extend(_ingest_house(runtime, config, trader) for trader in traders)
    results.extend(_ingest_13f(runtime, config, tracker) for tracker in trackers)
    return {
        "status": "partial" if any(row["status"] != "ok" for row in results) else "ok",
        "database": "postgresql",
        "files_configured": len(sources),
        "house_traders_configured": len(traders),
        "13f_trackers_configured": len(trackers),
        "rows_ingested": sum(int(row.get("rows") or 0) for row in results),
        "runs": results,
    }


def _ingest_13f(runtime: Any, config: Any, tracker: dict[str, Any]) -> dict[str, Any]:
    cik = str(tracker["cik"]).zfill(10)
    source_id = _slug(f"sec_13f_{cik}")
    repository = IngestionRepository(runtime)
    repository.register_source(
        source_id,
        name=f"{tracker['name']} 13F",
        family="disclosures",
        kind="sec_13f",
        origin=f"https://data.sec.gov/submissions/CIK{cik}.json",
        capabilities={"disclosures": True, "holdings": True},
    )
    run_id = repository.start_run(source_id, "disclosures")
    user_agent = str(config.market_data.user_agent)
    try:
        submissions_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        submissions_payload = _http_bytes(submissions_url, user_agent)
        submissions = json.loads(submissions_payload)
        _record_bytes(repository, config, run_id, source_id, "submissions.json", submissions_payload, submissions_url)
        filings = _recent_13f(submissions, int(tracker.get("max_filings") or 1))
        existing = _existing_source_keys(runtime, source_id, [row["accession_number"] for row in filings])
        ingested = 0
        skipped = 0
        failures: list[str] = []
        for filing in filings:
            accession = filing["accession_number"]
            if accession in existing:
                skipped += 1
                continue
            try:
                index_url = sec.filing_index_url(cik, accession)
                index_payload = _http_bytes(index_url, user_agent)
                index = json.loads(index_payload)
                _record_bytes(repository, config, run_id, source_id, f"{accession}-index.json", index_payload, index_url)
                candidate = _information_table_candidate(index, filing.get("primary_document"))
                if not candidate:
                    raise ValueError("13F information table XML not found")
                document_url = sec.filing_document_url(cik, accession, candidate)
                document_payload = _http_bytes(document_url, user_agent)
                payload_id = _record_bytes(
                    repository, config, run_id, source_id, f"{accession}-{Path(candidate).name}",
                    document_payload, document_url,
                )
                holdings = _resolve_holdings(_parse_information_table(document_payload), tracker.get("ticker_map") or {})
                total_value = sum(int(row.get("value_thousands") or 0) for row in holdings)
                row = {
                    "source_key": accession,
                    "source_type": "13f",
                    "trader_name": tracker["name"],
                    "filer_name": filing.get("filer_name") or tracker["name"],
                    "event_date": filing.get("report_date"),
                    "filed_date": filing.get("filed_date"),
                    "action": filing.get("form"),
                    "amount_text": str(total_value) if holdings else None,
                    "source_url": document_url,
                    "details": {
                        "cik": cik, "accession_number": accession, "report_date": filing.get("report_date"),
                        "holdings": holdings, "holdings_count": len(holdings),
                        "holdings_value_thousands": total_value, "source_document": candidate,
                    },
                }
                ingested += SourceFactRepository(runtime).store_disclosures(run_id, source_id, [row], payload_id=payload_id)
            except Exception as exc:
                failures.append(f"{accession}: {type(exc).__name__}: {exc}")
        status = "partial" if failures else "succeeded"
        repository.finish_run(
            run_id, status, item_count=ingested,
            failure_detail="; ".join(failures[:10]) or None,
            summary={"filings_found": len(filings), "filings_skipped": skipped},
        )
        return {
            "source_id": source_id, "status": "partial" if failures else "ok", "rows": ingested,
            "filings_found": len(filings), "filings_skipped": skipped, "errors": failures,
        }
    except Exception as exc:
        repository.finish_run(run_id, "failed", failure_detail=f"{type(exc).__name__}: {exc}")
        return {"source_id": source_id, "status": "failed", "rows": 0, "error": str(exc)}


def _recent_13f(submissions: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    recent = dict((submissions.get("filings") or {}).get("recent") or {})
    rows: list[dict[str, Any]] = []
    for index, form in enumerate(recent.get("form") or []):
        if form not in {"13F-HR", "13F-HR/A", "13F-NT", "13F-NT/A"}:
            continue
        accession = _at(recent, "accessionNumber", index)
        if not accession:
            continue
        rows.append({
            "accession_number": accession, "form": form,
            "filed_date": _at(recent, "filingDate", index),
            "report_date": _at(recent, "reportDate", index),
            "primary_document": _at(recent, "primaryDocument", index),
            "filer_name": submissions.get("name"),
        })
        if len(rows) >= limit:
            break
    return rows


def _information_table_candidate(index: dict[str, Any], primary_document: Any) -> str | None:
    primary = str(primary_document or "").lower()
    names = [
        str(item.get("name"))
        for item in ((index.get("directory") or {}).get("item") or [])
        if isinstance(item, dict) and str(item.get("name") or "").lower().endswith(".xml")
        and str(item.get("name") or "").lower() not in {primary, "filingsummary.xml"}
    ]
    names.sort(key=lambda name: (0 if "info" in name.lower() else 1, name.lower()))
    return names[0] if names else None


def _parse_information_table(payload: bytes) -> list[dict[str, Any]]:
    root = ET.fromstring(payload)
    rows: list[dict[str, Any]] = []
    for item in root.iter():
        if item.tag.rsplit("}", 1)[-1] != "infoTable":
            continue
        values = {child.tag.rsplit("}", 1)[-1]: (child.text or "").strip() for child in item.iter() if child.text and not list(child)}
        rows.append({
            "name": values.get("nameOfIssuer"), "title": values.get("titleOfClass"),
            "cusip": values.get("cusip"), "value_thousands": _int(values.get("value")),
            "shares_or_principal_amount": _int(values.get("sshPrnamt")),
            "shares_or_principal_type": values.get("sshPrnamtType"), "put_call": values.get("putCall"),
        })
    return rows


def _resolve_holdings(rows: list[dict[str, Any]], ticker_map: dict[str, str]) -> list[dict[str, Any]]:
    normalized_map = {str(key).replace(" ", "").upper(): str(value).upper() for key, value in ticker_map.items()}
    return [
        {**row, "symbol": normalized_map.get(str(row.get("cusip") or "").replace(" ", "").upper())}
        for row in rows
    ]


def _http_bytes(url: str, user_agent: str) -> bytes:
    response = httpx.get(
        url, headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
        timeout=30, follow_redirects=True,
    )
    response.raise_for_status()
    return response.content


def _record_bytes(
    repository: IngestionRepository,
    config: Any,
    run_id: Any,
    source_id: str,
    filename: str,
    payload: bytes,
    source_url: str,
) -> int:
    preferred = Path(config.nas.market_dir) / "provider-payloads"
    root = preferred if preferred.parent.exists() else Path(config.report_dir).parent / "provider-payloads"
    path = root / source_id / "sec" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_bytes() != payload:
        path.write_bytes(payload)
    return repository.record_payload_file(run_id, path, source_url=source_url)


def _existing_source_keys(runtime: Any, source_id: str, keys: list[str]) -> set[str]:
    if not keys:
        return set()
    with runtime.read() as connection:
        rows = connection.execute(
            "SELECT source_key FROM raw.disclosure WHERE source_id = %s AND source_key = ANY(%s)",
            [source_id, keys],
        ).fetchall()
    return {str(row["source_key"]) for row in rows}


def _at(values: dict[str, Any], key: str, index: int) -> Any:
    items = values.get(key) or []
    return items[index] if index < len(items) else None


def _int(value: Any) -> int | None:
    try:
        return int(str(value).replace(",", "")) if value not in (None, "") else None
    except ValueError:
        return None


def _ingest_house(runtime: Any, config: Any, trader: dict[str, Any]) -> dict[str, Any]:
    house = trader["official_house"]
    source_id = _slug(f"house_{trader['trader_name']}")
    repository = IngestionRepository(runtime)
    repository.register_source(
        source_id,
        name=f"{trader['trader_name']} House disclosures",
        family="disclosures",
        kind="house_financial_disclosure",
        origin="https://disclosures-clerk.house.gov/",
        capabilities={"disclosures": True},
    )
    run_id = repository.start_run(source_id, "disclosures")
    user_agent = str(house.get("user_agent") or config.market_data.user_agent)
    current_year = date.today().year
    start_year = max(int(house.get("start_year") or current_year), current_year - 1)
    end_year = min(int(house.get("end_year") or current_year), current_year)
    try:
        filings = search_house_member_filings(
            str(house.get("last_name") or trader["trader_name"].split()[-1]),
            start_year,
            end_year,
            user_agent,
            state=house.get("state"),
            district=str(house.get("district")) if house.get("district") else None,
        )
        wanted = set(house.get("filing_types") or ["PTR Original", "FD Original"])
        existing = _existing_document_ids(runtime, source_id)
        ingested = 0
        skipped = 0
        for filing in filings:
            document_id = str(filing.get("document_id") or "")
            if (wanted and filing.get("filing_type") not in wanted) or document_id in existing:
                skipped += 1
                continue
            payload = fetch_house_pdf_bytes(str(filing["url"]), user_agent)
            archive = _archive_house_pdf(config, source_id, document_id, payload)
            payload_id = repository.record_payload_file(
                run_id, archive, source_url=filing["url"], source_document_id=document_id
            )
            text = parse_house_pdf_bytes(payload)
            rows = [
                _normalize_house(row, filing, trader)
                for row in parse_house_disclosure_text(text, filing, trader["trader_name"])
            ]
            count = SourceFactRepository(runtime).store_disclosures(run_id, source_id, rows, payload_id=payload_id)
            ingested += count
        repository.finish_run(
            run_id,
            "succeeded",
            item_count=ingested,
            summary={"filings_found": len(filings), "filings_skipped": skipped, "years": [start_year, end_year]},
        )
        return {"source_id": source_id, "status": "ok", "rows": ingested, "filings_found": len(filings), "filings_skipped": skipped}
    except Exception as exc:
        repository.finish_run(run_id, "failed", failure_detail=f"{type(exc).__name__}: {exc}")
        return {"source_id": source_id, "status": "failed", "rows": 0, "error": str(exc)}


def _normalize_house(row: dict[str, Any], filing: dict[str, Any], trader: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_key": row.get("id") or f"house:{filing.get('document_id')}:{row.get('symbol')}:{row.get('transaction_date')}",
        "source_type": "public_disclosure_transaction",
        "trader_name": trader["trader_name"],
        "filer_name": row.get("filer_name") or filing.get("name") or trader.get("filer_name"),
        "symbol": row.get("symbol"),
        "event_date": row.get("transaction_date"),
        "filed_date": row.get("filed_date"),
        "action": row.get("transaction_type"),
        "amount_text": row.get("amount"),
        "source_url": filing.get("url"),
        "details": {**row, "source_document_id": filing.get("document_id")},
    }


def _existing_document_ids(runtime: Any, source_id: str) -> set[str]:
    with runtime.read() as connection:
        rows = connection.execute(
            "SELECT DISTINCT details->>'source_document_id' AS id FROM raw.disclosure WHERE source_id = %s",
            [source_id],
        ).fetchall()
    return {str(row["id"]) for row in rows if row.get("id")}


def _archive_house_pdf(config: Any, source_id: str, document_id: str, payload: bytes) -> Path:
    preferred = Path(config.nas.market_dir) / "provider-payloads"
    root = preferred if preferred.parent.exists() else Path(config.report_dir).parent / "provider-payloads"
    path = root / source_id / "house" / f"{document_id}.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_bytes() != payload:
        path.write_bytes(payload)
    return path


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
        count = SourceFactRepository(runtime).store_disclosures(run_id, source_id, rows, payload_id=payload_id)
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


def _configured_house_traders(config_path: str | None) -> list[dict[str, Any]]:
    path = resolve_path(config_path or "config.yaml")
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8") as handle:
        disclosures = dict((yaml.safe_load(handle) or {}).get("disclosures") or {})
    return [
        {
            "trader_name": str(row.get("trader_name") or row.get("name")),
            "filer_name": row.get("filer_name") or row.get("name"),
            "official_house": dict(row.get("official_house") or {}),
        }
        for row in disclosures.get("tracked_traders") or []
        if isinstance(row, dict) and (row.get("trader_name") or row.get("name")) and row.get("official_house")
    ]


def _configured_13f_trackers(config_path: str | None) -> list[dict[str, Any]]:
    path = resolve_path(config_path or "config.yaml")
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8") as handle:
        disclosures = dict((yaml.safe_load(handle) or {}).get("disclosures") or {})
    return [
        {
            "name": str(row.get("name") or row.get("trader_name") or row["cik"]),
            "cik": str(row["cik"]), "max_filings": int(row.get("max_filings") or 1),
            "ticker_map": dict(row.get("ticker_map") or row.get("cusip_ticker_map") or {}),
        }
        for row in disclosures.get("13f_trackers") or []
        if isinstance(row, dict) and row.get("cik")
    ]


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
