from __future__ import annotations

from datetime import UTC, datetime

from investment_panel.jobs.update_company_financials import company_fact_rows, ticker_cik_map


def test_sec_company_ticker_map_normalizes_cik_and_symbols() -> None:
    assert ticker_cik_map({
        "0": {"ticker": "acme", "cik_str": 1234, "title": "Acme"},
        "1": {"ticker": "", "cik_str": 99},
    }) == {"ACME": "0000001234"}


def test_company_fact_rows_keep_acceptance_timestamp_and_revision() -> None:
    submissions = {
        "filings": {
            "recent": {
                "accessionNumber": ["0000123456-26-000001", "0000123456-26-000002"],
                "acceptanceDateTime": [
                    "2026-02-02T16:05:22.000Z",
                    "2026-02-03T17:10:00.000Z",
                ],
            }
        }
    }
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "accn": "0000123456-26-000001",
                                "end": "2025-12-31",
                                "filed": "2026-02-02",
                                "form": "10-K",
                                "fy": 2025,
                                "fp": "FY",
                                "val": 100,
                            },
                            {
                                "accn": "0000123456-26-000002",
                                "end": "2025-12-31",
                                "filed": "2026-02-03",
                                "form": "10-K/A",
                                "fy": 2025,
                                "fp": "FY",
                                "val": 110,
                            },
                        ]
                    }
                }
            }
        }
    }

    rows = company_fact_rows(
        symbol="ACME",
        name="Acme Corp",
        asset_class="equity",
        cik="0000001234",
        submissions=submissions,
        facts=facts,
        max_filings=10,
    )

    assert len(rows) == 2
    assert rows[0]["filed_at"] == datetime(2026, 2, 3, 17, 10, tzinfo=UTC)
    assert rows[0]["values"]["metrics"]["revenue"] == 110.0
    assert rows[0]["values"]["accession_number"] == "0000123456-26-000002"
    assert rows[1]["values"]["accepted_at"] == "2026-02-02T16:05:22+00:00"

    latest_filing_only = company_fact_rows(
        symbol="ACME",
        name="Acme Corp",
        asset_class="equity",
        cik="0000001234",
        submissions=submissions,
        facts=facts,
        max_filings=1,
    )
    assert len(latest_filing_only) == 1
    assert latest_filing_only[0]["values"]["accession_number"] == "0000123456-26-000002"


def test_company_fact_rows_keep_quarter_and_year_to_date_durations_separate() -> None:
    submissions = {
        "filings": {
            "recent": {
                "accessionNumber": ["0000123456-26-000003"],
                "acceptanceDateTime": ["2026-05-02T16:05:22.000Z"],
            }
        }
    }
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "accn": "0000123456-26-000003",
                                "start": "2026-01-01",
                                "end": "2026-03-31",
                                "form": "10-Q",
                                "val": 25,
                            },
                            {
                                "accn": "0000123456-26-000003",
                                "start": "2025-07-01",
                                "end": "2026-03-31",
                                "form": "10-Q",
                                "val": 75,
                            },
                        ]
                    }
                }
            }
        }
    }

    rows = company_fact_rows(
        symbol="ACME",
        name="Acme Corp",
        asset_class="equity",
        cik="0000001234",
        submissions=submissions,
        facts=facts,
        max_filings=10,
    )

    assert {(row["period_start"], row["period_end"], row["values"]["metrics"]["revenue"]) for row in rows} == {
        ("2026-01-01", "2026-03-31", 25.0),
        ("2025-07-01", "2026-03-31", 75.0),
    }


def test_company_fact_rows_collect_dei_shares_outstanding() -> None:
    accepted_at = "2026-05-02T16:05:22.000Z"
    rows = company_fact_rows(
        symbol="ACME",
        name="Acme Corp",
        asset_class="equity",
        cik="0000001234",
        submissions={
            "filings": {
                "recent": {
                    "accessionNumber": ["0000123456-26-000003"],
                    "acceptanceDateTime": [accepted_at],
                }
            }
        },
        facts={
            "facts": {
                "dei": {
                    "EntityCommonStockSharesOutstanding": {
                        "units": {
                            "shares": [{
                                "accn": "0000123456-26-000003",
                                "end": "2026-04-25",
                                "form": "10-Q",
                                "val": 1000000,
                            }]
                        }
                    }
                }
            }
        },
        max_filings=10,
    )

    assert len(rows) == 1
    assert rows[0]["values"]["metrics"]["shares_outstanding"] == 1_000_000.0
    assert rows[0]["values"]["tags"]["shares_outstanding"]["taxonomy"] == "dei"
