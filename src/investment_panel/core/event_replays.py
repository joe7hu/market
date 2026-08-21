"""Acceptance fixtures for point-in-time Event Scout replay."""

from __future__ import annotations

from typing import Any


MRNA_REPLAY_SOURCE = "https://www.google.com/finance/quote/MRNA:NASDAQ"
MRNA_SHORT_SOURCE = "https://www.marketbeat.com/stocks/NASDAQ/MRNA/short-interest/"
MRNA_EVENT_SOURCE = "https://www.merck.com/news/merck-and-moderna-announce-phase-3-interpath-001-trial-of-intismeran-autogene-plus-keytruda-met-endpoints-of-recurrence-free-survival-rfs-and-distant-metastasis-free-survival-dmfs-in-patient/"


def mrna_replay_fixture() -> dict[str, Any]:
    """Return the user-supplied 10:32 ET replay without later observations."""

    from investment_panel.core.event_scout import evidence_field

    observed_at = "2026-07-31T10:32:00-04:00"
    return {
        "symbol": "MRNA",
        "as_of": observed_at,
        "event_kind": "clinical_announcement",
        "trigger_type": "formal_announcement",
        "source_url": MRNA_EVENT_SOURCE,
        "source_kind": "official_company_announcement",
        "headline": "MRNA Phase 3 event replay",
        "market_tape": {
            "latest_price": evidence_field(153.44, observed_at=observed_at, source_url=MRNA_REPLAY_SOURCE, source_kind="market_quote", freshness="fresh", evidence_class="reported_fact"),
            "intraday_high": evidence_field(162.65, observed_at=observed_at, source_url=MRNA_REPLAY_SOURCE, source_kind="market_quote", freshness="fresh", evidence_class="reported_fact", note="high known in the supplied 10:32 snapshot, not a later close-time high"),
            "event_return_pct": evidence_field(144.0, observed_at=observed_at, source_url=MRNA_REPLAY_SOURCE, source_kind="market_quote", freshness="fresh", evidence_class="reported_fact"),
            "volume": evidence_field(77_100_000, observed_at=observed_at, source_url=MRNA_REPLAY_SOURCE, source_kind="market_quote", freshness="fresh", evidence_class="reported_fact"),
            "halt_status": evidence_field("unknown", observed_at=observed_at, source_url=MRNA_REPLAY_SOURCE, source_kind="market_quote", freshness="unknown", evidence_class="missing"),
            "bid_ask_spread": evidence_field(None, observed_at=observed_at, source_url=MRNA_REPLAY_SOURCE, source_kind="market_quote", note="not supplied by replay"),
            "liquidity_status": evidence_field(None, observed_at=observed_at, source_url=MRNA_REPLAY_SOURCE, source_kind="market_quote", note="not supplied by replay"),
        },
        "short_interest_history": [
            {
                "short_shares": 52_400_000, "short_pct_float": 14.8, "days_to_cover": 6.1,
                "record_date": "2026-07-15", "publish_date": "2026-07-25T09:00:00-04:00",
                "average_volume_basis": "MarketBeat reported average volume",
                "source_url": MRNA_SHORT_SOURCE, "source_kind": "short_interest_history",
            },
            {
                "short_shares": 49_770_000, "short_pct_float": 13.98, "days_to_cover": 10.2,
                "record_date": "2026-07-31", "publish_date": "2026-07-31T09:00:00-04:00",
                "average_volume_basis": "MarketBeat reported average volume",
                "source_url": MRNA_SHORT_SOURCE, "source_kind": "short_interest_history",
            },
        ],
        "event_fundamentals": {
            "trial_phase": evidence_field("Phase 3", observed_at=observed_at, source_url=MRNA_EVENT_SOURCE, source_kind="official_company_announcement", evidence_class="verified_fact", freshness="fresh"),
        },
        "platform_optionality": {
            "first_in_modality": evidence_field(True, observed_at=observed_at, source_url=MRNA_EVENT_SOURCE, source_kind="official_company_announcement", evidence_class="reported_fact", freshness="fresh"),
            "read_through_to_other_trials": evidence_field("inference only", observed_at=observed_at, source_url=MRNA_EVENT_SOURCE, source_kind="analyst_inference", evidence_class="inference", freshness="fresh"),
            "narrative_change": evidence_field("platform validation narrative strengthened", observed_at=observed_at, source_url=MRNA_EVENT_SOURCE, source_kind="analyst_inference", evidence_class="inference", freshness="fresh"),
            "trial_count": evidence_field(None, observed_at=observed_at, source_url=MRNA_EVENT_SOURCE, source_kind="official_company_announcement", note="a count such as nine trials needs a linked primary source"),
        },
        "risk_inputs": {},
    }


def replay_mrna() -> dict[str, Any]:
    from investment_panel.core.event_scout import build_event_decision_packet

    return build_event_decision_packet(**mrna_replay_fixture())


__all__ = [
    "MRNA_REPLAY_SOURCE", "MRNA_SHORT_SOURCE", "MRNA_EVENT_SOURCE",
    "mrna_replay_fixture", "replay_mrna",
]
