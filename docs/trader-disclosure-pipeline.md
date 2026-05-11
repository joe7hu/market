# Trader Disclosure Pipeline

Market tracks traders from primary public disclosures, not from third-party
tracker pages. Third-party products can inform UI patterns and sanity checks,
but they are not ingestion sources.

## Daily Pipeline

Run daily after market data has refreshed:

```bash
uv run python -m investment_panel.jobs.update_disclosures --config config.yaml --skip-holdings
```

The job:

1. Loads configured public disclosure CSV feeds from `disclosures.public_disclosure_csvs`
   and each tracked trader's `daily_csvs`.
2. Searches configured official House disclosure feeds, downloads matching PTR
   and annual FD PDFs, and parses reproducible rows from the PDFs.
3. Normalizes transaction/baseline rows into
   `source_type = public_disclosure_transaction`.
4. Ensures local price history reaches back to each trader's earliest
   disclosure event.
5. Refreshes configured SEC 13F metadata rows.
6. Rebuilds one `source_type = trader_portfolio_model` row per trader from
   normalized disclosures and local `prices_daily`.
7. Writes job status to `mini-market-disclosures.json`.

Use `--skip-holdings` for the daily scheduler until 13F holding fetch volume is
intentionally widened. Remove it for manual deeper 13F refreshes.

## Required CSV Contract

Each feed row should represent one disclosed transaction. Preferred columns:

```csv
trader_name,symbol,transaction_type,transaction_date,filed_date,amount_min,amount_max,source_url
Nancy Pelosi,NVDA,BUY,2025-01-15,2025-02-10,1000000,5000000,https://disclosures-clerk.house.gov/
```

Accepted aliases:

- `symbol` or `ticker`
- `transaction_type`, `type`, or `action`
- `transaction_date`, `event_date`, or `date`
- `filed_date` or `filing_date`
- `amount_min` and `amount_max`, or a single `amount` / `amount_range`
- `source_url` or `url`

The replica portfolio uses the midpoint of disclosed amount ranges and the
nearest local close price on or before the transaction date. This is an
estimated model, not proof of current holdings.

## Onboard A New Trader

1. Choose a primary source:
   - Congressional PTRs: House Clerk / Senate eFD public disclosures.
   - Fund managers: SEC 13F for quarterly long holdings.
   - Corporate insiders: SEC Form 4.
   - Other jurisdictions: only if the disclosure source is official and
     mechanically reproducible.
2. Create or identify normalized historical and daily CSV feeds under a durable
   source location. Historical feeds should cover the trader's full disclosed
   activity back to the desired model inception date. Daily feeds should contain
   new or recently changed filings only.
3. Add the trader to `config.yaml`. For House members, prefer the official
   House search configuration so backfill can download the full PDF history:

```yaml
disclosures:
  tracked_traders:
    - name: Nancy Pelosi
      filer_name: House financial disclosures
      source_kind: house_disclosures
      official_house:
        last_name: Pelosi
        state: CA
        start_year: 2018
        end_year: 2026
        filing_types:
          - PTR Original
          - FD Original
```

4. Make sure symbols are market tickers that can be priced. Backfill will fetch
   missing price history for disclosure symbols, but unsupported symbols or
   ambiguous assets still need a defended mapping before they can be modeled.
5. Run the one-time historical backfill:

```bash
uv run python -m investment_panel.jobs.backfill_trader_disclosures --config config.yaml --trader "Nancy Pelosi"
```

By default the backfill clears that trader's prior normalized transaction/model
rows before re-ingesting configured `historical_csvs` and `daily_csvs`. Use
`--no-replace` only when intentionally appending/upserting without rebuilding
from a clean trader ledger.

6. After backfill, the scheduled daily job keeps the trader current:

```bash
uv run python -m investment_panel.jobs.update_disclosures --config config.yaml --skip-holdings
```

7. Check `/api/disclosures` for:
   - `public_disclosure_transaction` rows for the raw transactions.
   - one `trader_portfolio_model` row for the derived portfolio.
8. Open Trader Filings in the app and verify holdings, transaction history,
   caveats, and ticker links.

## Acceptance Checklist

- Source is official or otherwise primary enough to defend.
- Every row has a source URL or source file reference.
- Date semantics are explicit: transaction date vs filed date.
- Amount semantics are explicit: exact notional vs disclosed range.
- No third-party tracker page is used as the data source.
- The daily disclosure job can run idempotently without duplicating rows.

## Definition Of Done

Do not call trader replication done because ingestion, backfill, scheduling, or
UI plumbing exists. It is done only when Market can reproduce a close result
from primary-source history and the benchmark is recorded.

Minimum benchmark gate for each trader:

- Full historical disclosure backfill has run from the chosen official source.
- A benchmark report compares derived holdings against an external reference
  portfolio that was not ingested as source data.
- Symbol overlap, top-holding rank overlap, portfolio value error, and
  materially wrong extras/misses are recorded.
- The result is explicitly marked `close`, `not close`, or `blocked`, with root
  causes for any miss.

Until that benchmark passes, describe the work as pipeline scaffolding or an
unverified model, not as a completed tracker.

## Current Verification Status

As of 2026-05-10, the scalable pipeline is still `not_close` against the live
PelosiTracker reference. This is a verified result, not a completed replication.

The current run uses official House search for Nancy Pelosi, CA, 2018-2026,
with `PTR Original` and `FD Original` filings enabled. It deleted prior
third-party tracker rows, ingested 58 official House PDFs, normalized 223
disclosure rows, refreshed missing historical prices, and rebuilt the replica
portfolio.

Benchmark file:
`/Volumes/agent/data-sources/status/mini-market-trader-benchmark-nancy-pelosi.json`

Latest benchmark:

- Verdict: `not_close`
- Symbol overlap: `0.8333` (`10/12`)
- Top-six overlap: `0.6667`
- Mean weight error: `5.3979`
- Missing reference symbols: `IBTA.L`, `TSLA`
- Extra official-model symbols include `AB`, `AXP`, `CRM`, `DIS`, `V`, and
  other holdings that appear in House annual disclosures but not in the
  reference portfolio.

Root causes:

- The reference includes `IBTA.L`, which is not the ticker disclosed in the
  official House rows. The official 2026 filing contains
  `AllianceBernstein Holding L.P. Units (AB)`, not `IBTA.L`.
- The latest annual FD shows `Tesla, Inc. (TSLA)` with no current asset value
  and a capital-loss note, while the reference includes TSLA as a current
  allocation.
- Congressional annual reports disclose broad ranges, not exact quantities.
  Weight matching remains approximate even when the symbol appears.
- The reference appears to be a third-party/model portfolio allocation, not a
  directly reproducible current-position statement from House disclosures.

Do not mark this replication as done until those source/model gaps are either
resolved from primary disclosures or explicitly accepted as reference-specific
calibration rules.

## Kevin Hern Verification Status

As of 2026-05-10, Kevin Hern is configured through the same official House
disclosure pipeline and is also `not_close` against the visible PelosiTracker
politician page benchmark.

The current run uses official House search for Kevin Hern, OK-01, 2020-2026,
with `PTR Original` and `FD Original` filings enabled. It ingested 76 official
House PDFs, normalized 1048 disclosure rows, refreshed missing historical prices,
and rebuilt the replica portfolio.

Benchmark file:
`/Volumes/agent/data-sources/status/mini-market-trader-benchmark-kevin-hern.json`

Latest benchmark:

- Verdict: `not_close`
- Reference coverage: `top_holdings_only`
- Symbol overlap: `1.0` (`5/5` visible top symbols)
- Top-five overlap: `0.6`
- Mean weight error: `2.4392`
- Total value error: `11.9961%`
- Reference top five: `DVN`, `WMB`, `XOM`, `JPM`, `RTX`
- Model top five: `DVN`, `RTX`, `ROK`, `GOOGL`, `WMB`

Root causes:

- The visible reference page publishes only top holdings plus `OTHER`, not a
  full holdings ledger.
- Official annual FD rows contain many disclosed holdings outside the reference
  top five. The model ranks several of them above `WMB`, `XOM`, and `JPM`.
- The value and visible-symbol weights are close, but top-rank ordering is not
  close enough under the current benchmark gate.
