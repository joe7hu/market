"""Disclosure ingestion package — facade.

Import from this package; add a responsibility submodule and re-export it
here rather than growing a god-file. See ARCHITECTURE.md.
"""
from __future__ import annotations

from investment_panel.core.disclosures.coerce import (
    amount_midpoint,
    days_since,
    disclosure_amount_range,
)
from investment_panel.core.disclosures.config import (
    disclosure_csv_sources,
    extract_13f_trackers,
    extract_public_disclosure_csvs,
    extract_tracked_traders,
    load_13f_trackers_from_config,
    load_public_disclosure_csvs_from_config,
    load_tracked_traders_from_config,
    normalize_13f_ticker_map,
    normalize_cusip,
)
from investment_panel.core.disclosures.constants import (
    PUBLIC_DISCLOSURE_CAVEAT,
    THIRTEEN_F_CAVEAT,
    THIRTEEN_F_FORMS,
    stable_id,
)
from investment_panel.core.disclosures.house import (
    backfill_trader_disclosure_history,
    delete_trader_disclosure_rows,
    ingest_official_house_disclosures_for_trader,
)
from investment_panel.core.disclosures.prices import (
    ensure_disclosure_symbol_prices,
    latest_price_for_symbol,
    price_on_or_before,
)
from investment_panel.core.disclosures.public_csv import (
    ingest_public_disclosure_csvs,
    normalize_public_disclosure_transaction,
    upsert_public_disclosure_transaction,
)
from investment_panel.core.disclosures.replica import (
    allocation_weight,
    build_portfolio_history,
    build_replica_portfolio_snapshot,
    compact_history,
    disclosed_quantity_from_raw,
    diversification_score,
    holding_values_at,
    rebuild_trader_replica_portfolios,
    upsert_replica_portfolio_snapshot,
)
from investment_panel.core.disclosures.thirteen_f import (
    fetch_13f_holding_payload,
    fetch_13f_holdings_from_submission_text,
    information_table_candidates,
    ingest_13f_trackers,
    next_13f_filing_due_date,
    parse_information_table_xml,
    preserve_existing_13f_holdings_when_not_requested,
    purge_direct_tracker_rows,
    quarter_end_date,
    recent_13f_filings,
    resolve_13f_holding_tickers,
    split_sec_documents,
    upsert_13f_disclosure,
)

__all__ = [
    "PUBLIC_DISCLOSURE_CAVEAT",
    "THIRTEEN_F_CAVEAT",
    "THIRTEEN_F_FORMS",
    "allocation_weight",
    "amount_midpoint",
    "backfill_trader_disclosure_history",
    "build_portfolio_history",
    "build_replica_portfolio_snapshot",
    "compact_history",
    "days_since",
    "delete_trader_disclosure_rows",
    "disclosed_quantity_from_raw",
    "disclosure_amount_range",
    "disclosure_csv_sources",
    "diversification_score",
    "ensure_disclosure_symbol_prices",
    "extract_13f_trackers",
    "extract_public_disclosure_csvs",
    "extract_tracked_traders",
    "fetch_13f_holding_payload",
    "fetch_13f_holdings_from_submission_text",
    "holding_values_at",
    "information_table_candidates",
    "ingest_13f_trackers",
    "ingest_official_house_disclosures_for_trader",
    "ingest_public_disclosure_csvs",
    "latest_price_for_symbol",
    "load_13f_trackers_from_config",
    "load_public_disclosure_csvs_from_config",
    "load_tracked_traders_from_config",
    "next_13f_filing_due_date",
    "normalize_13f_ticker_map",
    "normalize_cusip",
    "normalize_public_disclosure_transaction",
    "parse_information_table_xml",
    "preserve_existing_13f_holdings_when_not_requested",
    "price_on_or_before",
    "purge_direct_tracker_rows",
    "quarter_end_date",
    "rebuild_trader_replica_portfolios",
    "recent_13f_filings",
    "resolve_13f_holding_tickers",
    "split_sec_documents",
    "stable_id",
    "upsert_13f_disclosure",
    "upsert_public_disclosure_transaction",
    "upsert_replica_portfolio_snapshot",
]
