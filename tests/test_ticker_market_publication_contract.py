from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import investment_panel.jobs.ticker_decisions as ticker_decisions
from investment_panel.core.decision.ticker import InputLineage, MarketStateSnapshot


CUTOFF = datetime(2026, 8, 22, 14, 0, tzinfo=UTC)


def _publication() -> dict[str, object]:
    lineage = InputLineage(
        field="market_daily_price",
        source_id="test-source",
        available_at=CUTOFF,
        cutoff=CUTOFF,
    )
    snapshot = MarketStateSnapshot(
        snapshot_id="market-state:test",
        as_of=CUTOFF,
        input_cutoff=CUTOFF,
        input_lineage=(lineage,),
    )
    return {
        "publication_id": "market-publication:test",
        "publication_scope": "market",
        "publication_status": "published",
        "input_cutoff": CUTOFF.isoformat(),
        "published_at": (CUTOFF + timedelta(seconds=1)).isoformat(),
        "source_lineage": [lineage.model_dump(mode="json")],
        "models": {"market_state_snapshot": [snapshot.model_dump(mode="json")]},
    }


def test_market_snapshot_accepts_actual_visibility_after_fact_cutoff() -> None:
    snapshot = ticker_decisions._market_snapshot_for_decision(_publication(), CUTOFF)

    assert snapshot is not None
    assert snapshot.publication_id == "market-publication:test"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("publication_scope", "ticker"),
        ("publication_status", "superseded"),
        ("input_cutoff", (CUTOFF - timedelta(minutes=1)).isoformat()),
        ("published_at", CUTOFF.isoformat()),
        ("published_at", None),
    ),
)
def test_market_snapshot_rejects_invalid_publication_metadata(field: str, value: object) -> None:
    publication = _publication()
    publication[field] = value

    assert ticker_decisions._market_snapshot_for_decision(publication, CUTOFF) is None


def test_market_snapshot_rejects_mismatched_identity_and_future_lineage() -> None:
    publication = _publication()
    snapshot = publication["models"]["market_state_snapshot"][0]  # type: ignore[index]
    snapshot["publication_id"] = "market-publication:other"  # type: ignore[index]
    assert ticker_decisions._market_snapshot_for_decision(publication, CUTOFF) is None

    publication = _publication()
    snapshot = publication["models"]["market_state_snapshot"][0]  # type: ignore[index]
    snapshot["input_lineage"][0]["cutoff"] = (CUTOFF - timedelta(minutes=1)).isoformat()  # type: ignore[index]
    assert ticker_decisions._market_snapshot_for_decision(publication, CUTOFF) is None

    publication = _publication()
    snapshot = publication["models"]["market_state_snapshot"][0]  # type: ignore[index]
    snapshot["input_lineage"][0]["cutoff"] = None  # type: ignore[index]
    assert ticker_decisions._market_snapshot_for_decision(publication, CUTOFF) is None
