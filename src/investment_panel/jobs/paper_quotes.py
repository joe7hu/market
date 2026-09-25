"""Refresh only immutable pending/open paper contracts, never the broad universe."""
from __future__ import annotations
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
import logging
from investment_panel.settings import load_config
from investment_panel.infrastructure.postgres.authority import runtime_for_config
from investment_panel.infrastructure.postgres.options_history_policy import OptionHistoryPolicyRepository
from investment_panel.domain.decision import is_market_open
from investment_panel.core.robinhood_options import collect_robinhood_option_chains
from investment_panel.infrastructure.postgres.options import active_paper_contracts, persist_collected_option_chains


logger = logging.getLogger(__name__)


def run(config_path: str | None = "config.yaml") -> dict[str, Any]:
    config = load_config(config_path)
    provider = config.data_sources.brokers.robinhood
    if not config.data_sources.brokers.enabled or not provider.enabled:
        return {"status": "disabled", "reason": "provider_disabled", "paper_only": True}
    if not provider.readonly:
        return {"status": "failed", "reason": "readonly_provider_required", "paper_only": True}
    if not is_market_open(datetime.now(UTC)):
        return {"status": "skipped", "reason": "market_closed", "paper_only": True}
    required = active_paper_contracts(config, "robinhood")
    if not required:
        return {"status": "skipped", "reason": "no_active_contracts", "paper_only": True}
    # The owner prioritizes least-recently attempted symbols, then old quotes.
    # Failed/unsupported symbols must not repeatedly consume the first batch.
    # Keep every leg for each selected symbol together.
    symbols = list(dict.fromkeys(row["symbol"] for row in required))[:20]
    selected = [row for row in required if row["symbol"] in symbols]
    bounded = replace(provider, max_collection_seconds=45, timeout_seconds=10)
    policy = OptionHistoryPolicyRepository(runtime_for_config(config))
    lease = policy.acquire_provider_lease(provider="robinhood", workload="paper_execution_quotes",
                                          symbol="PAPER", ttl_seconds=90)
    if lease is None:
        return {"status": "skipped", "reason": "provider_capacity_busy", "paper_only": True,
                "contracts_required": len(required)}
    source_status = "failed"
    collected: dict[str, Any] = {}
    try:
        collected = collect_robinhood_option_chains(bounded, symbols, required_contracts=selected, required_only=True)
        source_status = "partial" if collected.get("errors") else "ok"
        persisted = persist_collected_option_chains(config, "robinhood", collected, universe="paper-tickets")
    except Exception:
        logger.exception("Targeted paper quote collection/persistence failed")
        # The scheduler persists this failed attempt, including its universe,
        # without treating it as a quote capture or resetting source freshness.
        return {"status": "failed", "reason": "paper_quote_capture_failed", "paper_only": True,
                "live_brokerage_submission": False, "source_id": "robinhood",
                "source_status": source_status, "downstream_status": "failed" if source_status != "failed" else "not_run",
                "symbols_requested": symbols, "symbols_attempted": collected.get("symbols_attempted") or [],
                "contracts_required": len(required),
                "contracts_selected": len(selected)}
    finally:
        policy.release_provider_lease(lease.id)
    count = int(persisted.get("contract_count") or 0)
    return {"status": "partial" if collected.get("errors") or count < len(selected) else "ok",
            "paper_only": True, "live_brokerage_submission": False, "source_id": "robinhood",
            "symbols_requested": symbols, "symbols_attempted": collected.get("symbols_attempted") or [],
            "source_status": source_status, "downstream_status": "ok",
            "contracts_required": len(required),
            "contracts_selected": len(selected), "contracts_captured": count,
            "remaining_contracts": max(0, len(required) - count), "run_id": persisted.get("run_id"),
            "errors": list(collected.get("errors") or [])[:5]}
