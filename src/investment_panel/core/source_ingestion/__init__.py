"""Canonical source ingestion package."""

from investment_panel.core.source_ingestion.definitions import SOURCE_DEFINITIONS, VERIFIED_SOURCES
from investment_panel.core.source_ingestion.health import lightweight_online_check, record_verified_sources
from investment_panel.core.source_ingestion.registry import ensure_source_registry
from investment_panel.core.source_ingestion.canonical import (
    ensure_canonical_sources,
    promote_source_signal_instruments,
    record_source_run,
    sync_canonical_sources,
    update_signal_market_context,
    upsert_signals_for_item,
    upsert_source_item,
)
from investment_panel.core.source_ingestion.read_models import (
    source_detail_payload,
    source_item_rows,
    source_registry_rows,
    source_run_rows,
    ticker_source_signal_rows,
)
from investment_panel.core.source_ingestion.audit import source_ingestion_audit
from investment_panel.core.source_ingestion.utils import (
    decode_row,
    evidence_refs_from_claims,
    infer_sentiment,
    normalize_signal_symbol,
    parse_json,
    slug,
    source_row_freshness,
    stable_id,
    symbols_from_value,
)

__all__ = [
    "SOURCE_DEFINITIONS",
    "VERIFIED_SOURCES",
    "decode_row",
    "ensure_canonical_sources",
    "ensure_source_registry",
    "evidence_refs_from_claims",
    "infer_sentiment",
    "lightweight_online_check",
    "normalize_signal_symbol",
    "parse_json",
    "promote_source_signal_instruments",
    "record_source_run",
    "record_verified_sources",
    "slug",
    "source_detail_payload",
    "source_ingestion_audit",
    "source_item_rows",
    "source_registry_rows",
    "source_row_freshness",
    "source_run_rows",
    "stable_id",
    "symbols_from_value",
    "sync_canonical_sources",
    "ticker_source_signal_rows",
    "update_signal_market_context",
    "upsert_signals_for_item",
    "upsert_source_item",
]
