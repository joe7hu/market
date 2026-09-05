"""Collect point-in-time issuer financial facts from SEC EDGAR."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from typing import Any, Iterable

from investment_panel.core import sec
from investment_panel.core.config import load_config
from investment_panel.core.provider_identity import provider_user_agent
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.ingestion import IngestionRepository


SOURCE_ID = "sec_companyfacts"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SUPPORTED_FORMS = frozenset({"10-K", "10-Q", "10-K/A", "10-Q/A"})
DEFAULT_MAX_FILINGS = 24

# These are deliberately a small, stable set of issuer cash-flow, financing,
# profitability, and share-count facts.  The original tag and accession stay
# in the stored row so adding a tag later never rewrites an older observation.
FACT_TAGS: dict[str, tuple[str, ...]] = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ),
    "gross_profit": ("GrossProfit",),
    "operating_income": ("OperatingIncomeLoss",),
    "net_income": ("NetIncomeLoss",),
    "operating_cash_flow": (
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ),
    "capital_expenditure": (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    ),
    "cash": (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ),
    "long_term_debt": (
        "LongTermDebtNoncurrent",
        "LongTermDebtCurrent",
    ),
    "stockholders_equity": ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
    "diluted_shares": ("WeightedAverageNumberOfDilutedSharesOutstanding",),
    "shares_outstanding": ("EntityCommonStockSharesOutstanding",),
}
FACT_TAGS_BY_TAXONOMY: dict[str, dict[str, tuple[str, ...]]] = {
    "us-gaap": FACT_TAGS,
    # EntityCommonStockSharesOutstanding is a DEI fact, not a us-gaap fact.
    "dei": {"shares_outstanding": ("EntityCommonStockSharesOutstanding",)},
}


def run(
    config_path: str | None = None,
    *,
    symbols: Iterable[str] | None = None,
    max_filings: int = DEFAULT_MAX_FILINGS,
) -> dict[str, Any]:
    """Fetch SEC facts for the catalog equity universe and preserve filing vintages."""

    config = load_config(config_path)
    runtime = runtime_for_config(config)
    repository = IngestionRepository(runtime)
    repository.register_source(
        SOURCE_ID,
        name="SEC EDGAR company facts",
        family="fundamentals",
        kind="sec_companyfacts",
        origin="https://data.sec.gov/api/xbrl/companyfacts/",
        capabilities={
            "company_financials": True,
            "filing_vintages": True,
            "acceptance_timestamps": True,
            "revisions": True,
        },
    )
    requested = {
        str(symbol).strip().upper()
        for symbol in symbols or []
        if str(symbol).strip()
    }
    with runtime.read() as connection:
        catalog_rows = connection.execute(
            """
            SELECT symbol, name, asset_class
            FROM catalog.instrument
            WHERE asset_class IN ('equity', 'etf')
            ORDER BY symbol
            """,
        ).fetchall()
    instruments = [
        {
            "symbol": str(row["symbol"]).upper(),
            "name": row["name"],
            "asset_class": row["asset_class"],
        }
        for row in catalog_rows
        if not requested or str(row["symbol"]).upper() in requested
    ]
    if not instruments:
        return {
            "status": "partial",
            "ok": True,
            "database": "postgresql",
            "source": SOURCE_ID,
            "source_status": "missing_source",
            "downstream_status": "not_run",
            "requested_symbols": sorted(requested),
            "coverage": {"catalog_symbols": 0, "mapped_symbols": 0, "stored_rows": 0},
            "missing_fields": ["company_financials"],
            "reason": "No catalog equity or ETF symbols matched the request.",
        }

    user_agent = provider_user_agent(config, "sec")
    started_at = datetime.now(UTC)
    errors: dict[str, str] = {}
    unmapped: list[str] = []
    not_applicable: list[str] = []
    all_rows: list[dict[str, Any]] = []
    try:
        mapped = ticker_cik_map(sec.company_tickers(user_agent))
    except Exception as exc:
        with repository.run(SOURCE_ID, "company_financials", started_at=started_at) as ingestion_run:
            ingestion_run.finish(
                "failed",
                failure_detail=f"{type(exc).__name__}: {exc}",
                summary={"source": SOURCE_ID, "stage": "company_tickers", "failed_symbols": len(instruments)},
            )
            run_id = ingestion_run.id
        return {
            "status": "failed",
            "ok": False,
            "database": "postgresql",
            "source": SOURCE_ID,
            "source_status": "failed",
            "downstream_status": "not_run",
            "run_id": str(run_id),
            "requested_symbols": len(instruments),
            "coverage": {"catalog_symbols": len(instruments), "mapped_symbols": 0, "symbols_with_facts": 0, "stored_rows": 0},
            "errors": {"company_tickers": f"{type(exc).__name__}: {exc}"},
        }
    for instrument in instruments:
        symbol = instrument["symbol"]
        if instrument["asset_class"] == "etf":
            not_applicable.append(symbol)
            continue
        cik = mapped.get(symbol)
        if not cik:
            unmapped.append(symbol)
            continue
        try:
            submissions = sec.company_submissions(cik, user_agent)
            facts = sec.company_facts(cik, user_agent)
            all_rows.extend(
                company_fact_rows(
                    symbol=symbol,
                    name=instrument["name"],
                    asset_class=instrument["asset_class"],
                    cik=cik,
                    submissions=submissions,
                    facts=facts,
                    max_filings=max_filings,
                )
            )
        except Exception as exc:  # one issuer must not hide the rest of the universe
            errors[symbol] = f"{type(exc).__name__}: {exc}"

    with repository.run(SOURCE_ID, "company_financials", started_at=started_at) as ingestion_run:
        stored = repository.store_fundamental_observations(
            ingestion_run.id,
            SOURCE_ID,
            "sec_companyfacts",
            all_rows,
        )
        symbols_with_facts = len({row["symbol"] for row in all_rows})
        no_fact_symbols = [
            instrument["symbol"]
            for instrument in instruments
            if instrument["asset_class"] != "etf"
            and instrument["symbol"] in mapped
            and instrument["symbol"] not in {row["symbol"] for row in all_rows}
        ]
        status = "partial" if errors or unmapped or no_fact_symbols else "succeeded"
        ingestion_run.finish(
            status,
            item_count=stored,
            instrument_count=len({row["symbol"] for row in all_rows}),
            failure_detail="; ".join(
                [
                    *(f"{symbol}: {error}" for symbol, error in list(errors.items())[:10]),
                    *(f"{symbol}: no SEC CIK mapping" for symbol in unmapped[:10]),
                    *(f"{symbol}: no supported SEC facts" for symbol in no_fact_symbols[:10]),
                ]
            ) or None,
            summary={
                "source": SOURCE_ID,
                "mapped_symbols": len(instruments) - len(unmapped) - len(not_applicable),
                "symbols_with_facts": symbols_with_facts,
                "unmapped_symbols": unmapped[:100],
                "not_applicable_symbols": not_applicable[:100],
                "no_fact_symbols": no_fact_symbols[:100],
                "failed_symbols": len(errors),
                "filing_vintages": True,
                "acceptance_timestamps": True,
                "revisions_preserved": True,
            },
        )
    return {
        "status": "partial" if errors or unmapped or no_fact_symbols else "ok",
        "ok": True,
        "database": "postgresql",
        "source": SOURCE_ID,
        "source_status": "partial" if errors or unmapped or no_fact_symbols else "ok",
        "downstream_status": "not_run",
        "run_id": str(ingestion_run.id),
        "requested_symbols": len(instruments),
        "coverage": {
            "catalog_symbols": len(instruments),
            "mapped_symbols": len(instruments) - len(unmapped) - len(not_applicable),
            "symbols_with_facts": len({row["symbol"] for row in all_rows}),
            "stored_rows": stored,
        },
        "unmapped_symbols": unmapped[:100],
        "not_applicable_symbols": not_applicable[:100],
        "errors": errors,
        "next_job": "market-publish-ticker-decisions",
    }


def ticker_cik_map(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    rows = payload.values() if all(isinstance(value, dict) for value in payload.values()) else []
    result: dict[str, str] = {}
    for row in rows:
        symbol = str(row.get("ticker") or "").strip().upper()
        cik = row.get("cik_str") or row.get("cik")
        if not symbol or cik in (None, ""):
            continue
        result[symbol] = str(cik).zfill(10)
    return result


def company_fact_rows(
    *,
    symbol: str,
    name: str | None,
    asset_class: str,
    cik: str,
    submissions: dict[str, Any],
    facts: dict[str, Any],
    max_filings: int,
) -> list[dict[str, Any]]:
    acceptance = _acceptance_map(submissions)
    fact_taxonomies = facts.get("facts", {}) if isinstance(facts, dict) else {}
    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for taxonomy, taxonomy_tags in FACT_TAGS_BY_TAXONOMY.items():
        facts_for_taxonomy = fact_taxonomies.get(taxonomy, {})
        if not isinstance(facts_for_taxonomy, dict):
            continue
        for metric, tags in taxonomy_tags.items():
            for tag in tags:
                definition = facts_for_taxonomy.get(tag)
                if not isinstance(definition, dict):
                    continue
                for unit, points in (definition.get("units") or {}).items():
                    if not isinstance(points, list):
                        continue
                    for point in points:
                        if not isinstance(point, dict) or not _supported_point(point):
                            continue
                        accn = str(point.get("accn") or "").strip()
                        accepted_at = acceptance.get(_accession_key(accn))
                        if not accn or accepted_at is None:
                            continue
                        start = str(point.get("start") or "")[:10]
                        end = str(point.get("end") or "")[:10]
                        # A single 10-Q can contain both the quarter and the
                        # year-to-date duration for the same end date. Keep the
                        # start date so those point-in-time facts cannot be
                        # collapsed into one mixed-period observation.
                        key = (accn, start, end, accepted_at.isoformat())
                        row = grouped.setdefault(
                            key,
                            {
                                "symbol": symbol,
                                "name": name or symbol,
                                "asset_class": asset_class,
                                "period_start": start or None,
                                "period_end": end,
                                "filed_at": accepted_at,
                                "observed_at": accepted_at,
                                "values": {
                                    "source": SOURCE_ID,
                                    "cik": cik,
                                    "accession_number": accn,
                                    "accepted_at": accepted_at.isoformat(),
                                    "filed": point.get("filed"),
                                    "start": start or None,
                                    "form": point.get("form"),
                                    "fiscal_year": point.get("fy"),
                                    "fiscal_period": point.get("fp"),
                                    "frame": point.get("frame"),
                                    "source_url": COMPANY_FACTS_URL.format(cik=cik),
                                    "filing_url": _filing_url(cik, accn),
                                    "metrics": {},
                                    "tags": {},
                                },
                            },
                        )
                        if metric not in row["values"]["metrics"]:
                            number = _number(point.get("val"))
                            if number is not None:
                                row["values"]["metrics"][metric] = number
                                row["values"]["tags"][metric] = {
                                    "taxonomy": taxonomy,
                                    "tag": tag,
                                    "unit": unit,
                                }
    rows = [row for row in grouped.values() if row["values"]["metrics"]]
    max_filing_count = max(1, int(max_filings))
    selected_accessions = {
        accession
        for accession, _accepted_at in sorted(
            {
                str(row["values"].get("accession_number")): row["observed_at"]
                for row in rows
            }.items(),
            key=lambda item: item[1],
            reverse=True,
        )[:max_filing_count]
    }
    rows = [
        row for row in rows
        if str(row["values"].get("accession_number") or "") in selected_accessions
    ]
    rows.sort(key=lambda row: row["observed_at"], reverse=True)
    return rows


def _acceptance_map(submissions: dict[str, Any]) -> dict[str, datetime]:
    recent = submissions.get("filings", {}).get("recent", {}) if isinstance(submissions, dict) else {}
    if not isinstance(recent, dict):
        return {}
    accessions = recent.get("accessionNumber") or []
    accepted = recent.get("acceptanceDateTime") or []
    result: dict[str, datetime] = {}
    for accn, timestamp in zip(accessions, accepted):
        parsed = _datetime(timestamp)
        if parsed is not None:
            result[_accession_key(str(accn))] = parsed
    return result


def _supported_point(point: dict[str, Any]) -> bool:
    return (
        str(point.get("form") or "").strip().upper() in SUPPORTED_FORMS
        and bool(point.get("end"))
        and point.get("val") not in (None, "")
    )


def _accession_key(value: str) -> str:
    return value.replace("-", "").strip().upper()


def _filing_url(cik: str, accession: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{_accession_key(accession)}/"


def _datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--symbol", action="append", dest="symbols")
    parser.add_argument("--max-filings", type=int, default=DEFAULT_MAX_FILINGS)
    args = parser.parse_args(argv)
    print(json.dumps(run(args.config, symbols=args.symbols, max_filings=args.max_filings), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
