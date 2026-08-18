from investment_panel.database.superinvestor_portfolios import (
    build_superinvestor_portfolios,
)
from investment_panel.core.disclosures import load_13f_trackers_from_config
from investment_panel.core.panel.contracts import PANEL_SCOPE_TABLES


def _filing(date, filed, holdings):
    return {
        "trader_name": "Test Investor",
        "filer_name": "Test Filer",
        "event_date": date,
        "filed_date": filed,
        "source": "sec",
        "source_url": f"https://sec.example/{date}",
        "source_key": f"key-{date}",
        "details": {"cik": "0001536411", "form": "13F-HR", "holdings": holdings},
    }


def test_two_filing_portfolio_keeps_unresolved_holdings_exits_and_truthful_estimate():
    rows = build_superinvestor_portfolios(
        [
            _filing(
                "2025-03-31",
                "2025-05-15",
                [
                    {
                        "symbol": "AAA",
                        "name": "Alpha",
                        "cusip": "000000001",
                        "shares_or_principal_amount": 100,
                        "value_usd": 1000000,
                    },
                    {
                        "name": "Unmapped Co",
                        "cusip": "000000002",
                        "shares_or_principal_amount": 50,
                        "value_usd": 500000,
                    },
                ],
            ),
            _filing(
                "2025-06-30",
                "2025-08-14",
                [
                    {
                        "symbol": "AAA",
                        "name": "Alpha",
                        "cusip": "000000001",
                        "shares_or_principal_amount": 80,
                        "value_usd": 1200000,
                    },
                    {
                        "symbol": "NEW",
                        "name": "New Co",
                        "cusip": "000000003",
                        "shares_or_principal_amount": 30,
                        "value_usd": 300000,
                    },
                ],
            ),
        ]
    )

    assert len(rows) == 1
    portfolio = rows[0]
    assert portfolio["event_date"] == "2025-06-30"
    assert portfolio["filed_date"] == "2025-08-14"
    assert portfolio["source"] == "sec"
    assert portfolio["provenance"]["cik"] == "0001536411"
    assert [item["event_date"] for item in portfolio["filing_history"]] == [
        "2025-03-31",
        "2025-06-30",
    ]
    assert {item["symbol"] for item in portfolio["holdings"]} == {"AAA", "NEW"}
    assert {item["change_type"] for item in portfolio["latest_allocation_changes"]} == {
        "DECREASE",
        "ADD",
        "EXIT",
    }
    exit_row = next(
        item
        for item in portfolio["latest_allocation_changes"]
        if item["change_type"] == "EXIT"
    )
    assert exit_row["symbol"] is None and exit_row["cusip"] == "000000002"
    aaa = next(item for item in portfolio["holdings"] if item["symbol"] == "AAA")
    effect = aaa["estimated_retained_share_price_effect"]
    assert effect["label"] == "estimated_retained_share_price_effect"
    assert "profit" not in effect["label"]
    assert effect["usd"] == 400000.0
    assert [
        point["estimated_retained_share_price_effect_usd"] for point in aaa["history"]
    ] == [0.0, 400000.0]


def test_estimate_is_unavailable_without_usable_reported_inputs():
    rows = build_superinvestor_portfolios(
        [
            _filing(
                "2025-03-31",
                "2025-05-15",
                [
                    {
                        "symbol": "AAA",
                        "shares_or_principal_amount": 10,
                        "value_usd": 100000,
                    }
                ],
            ),
            _filing(
                "2025-06-30",
                "2025-08-14",
                [{"symbol": "AAA", "shares_or_principal_amount": 10}],
            ),
        ]
    )
    assert rows[0]["holdings"][0]["estimated_retained_share_price_effect"] is None


def test_amendments_do_not_create_duplicate_quarters_and_cusip_survives_label_changes():
    original = _filing(
        "2025-03-31",
        "2025-05-15",
        [
            {
                "symbol": "OLD",
                "name": "Alpha Incorporated",
                "cusip": "000000001",
                "shares_or_principal_amount": 100,
                "value_usd": 1000000,
            },
        ],
    )
    amendment = _filing(
        "2025-03-31",
        "2025-05-20",
        [
            {
                "symbol": "AAA",
                "name": "Alpha Inc",
                "cusip": "000000001",
                "shares_or_principal_amount": 100,
                "value_usd": 1100000,
            },
        ],
    )
    amendment["source_key"] = "key-2025-03-31-amendment"
    latest = _filing(
        "2025-06-30",
        "2025-08-14",
        [
            {
                "symbol": "AAA",
                "name": "Alpha Holdings",
                "cusip": "000000001",
                "shares_or_principal_amount": 100,
                "value_usd": 1300000,
            },
        ],
    )

    portfolio = build_superinvestor_portfolios([original, amendment, latest])[0]

    assert [point["event_date"] for point in portfolio["filing_history"]] == [
        "2025-03-31",
        "2025-06-30",
    ]
    assert portfolio["filing_history"][0]["reported_value_usd"] == 1100000
    holding = portfolio["holdings"][0]
    assert len(holding["history"]) == 2
    assert holding["estimated_retained_share_price_effect"]["usd"] == 200000.0


def test_current_legacy_field_name_is_dollars_and_duplicate_manager_rows_aggregate():
    portfolio = build_superinvestor_portfolios(
        [
            _filing(
                "2025-03-31",
                "2025-05-15",
                [
                    {
                        "name": "Alpha",
                        "cusip": "000000001",
                        "shares_or_principal_amount": 5,
                        "value_thousands": 1250,
                    },
                    {
                        "name": "Alpha",
                        "cusip": "000000001",
                        "shares_or_principal_amount": 5,
                        "value_thousands": 1250,
                    },
                ],
            )
        ]
    )[0]

    assert portfolio["holdings_count"] == 1
    assert portfolio["reported_portfolio_value_usd"] == 2500
    assert portfolio["holdings"][0]["reported_value_usd"] == 2500
    assert portfolio["holdings"][0]["implied_price"] == 250


def test_ambiguous_usd_rows_infer_thousands_scale_from_implied_price():
    filing = _filing(
        "2026-06-30",
        "2026-08-14",
        [
            {
                "name": "10X Genomics",
                "cusip": "88025U109",
                "shares_or_principal_amount": 403100,
                "value_usd": 15455,
            }
        ],
    )
    filing["details"].update({"holdings_value_usd": 15455, "value_unit": "usd"})

    portfolio = build_superinvestor_portfolios([filing])[0]

    assert portfolio["reported_portfolio_value_usd"] == 15455000
    assert portfolio["holdings"][0]["reported_value_usd"] == 15455000
    assert round(portfolio["holdings"][0]["implied_price"], 2) == 38.34


def test_stanley_druckenmiller_tracker_is_configured_with_history_depth():
    trackers = load_13f_trackers_from_config("config.yaml")
    druckenmiller = next(
        item for item in trackers if item["name"] == "Stanley Druckenmiller / Duquesne"
    )
    assert druckenmiller["cik"] == "0001536411"
    assert druckenmiller["max_filings"] >= 4


def test_superinvestor_scope_uses_compact_portfolio_projection():
    assert PANEL_SCOPE_TABLES["superinvestors"] == (
        "superinvestor_portfolios",
        "ownership_consensus",
    )
