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
2. Normalizes transaction rows into `source_type = public_disclosure_transaction`.
3. Refreshes configured SEC 13F metadata rows.
4. Rebuilds one `source_type = trader_portfolio_model` row per trader from
   normalized transactions and local `prices_daily`.
5. Writes job status to `mini-market-disclosures.json`.

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
3. Add the trader to `config.yaml`:

```yaml
disclosures:
  tracked_traders:
    - name: Nancy Pelosi
      filer_name: House periodic transaction reports
      source_kind: house_ptr
      historical_csvs:
        - path: /absolute/path/to/nancy-pelosi-ptrs-history.csv
      daily_csvs:
        - path: /absolute/path/to/nancy-pelosi-ptrs-daily.csv
```

4. Make sure symbols are market tickers already covered by `prices_daily`.
   Add missing symbols to `watchlist` and run the relevant price job before
   expecting high-quality replica weights.
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

As of 2026-05-10, the scalable pipeline is not yet close enough to reproduce a
third-party Pelosi-style tracker portfolio from official filings alone.

A benchmark using the official House PTR filing `20033725` produced a model with
6 holdings and 5 of 12 benchmark symbols overlapping. The model was dominated by
`AB`, missed historical positions such as `AVGO`, `PANW`, `CRWD`, `TSLA`, and
`MSFT`, and had a materially different estimated value/performance profile.

Root causes:

- The current importer consumes already-normalized CSV rows; it does not yet
  discover and parse new House/Senate PTR PDFs itself.
- A single PTR only captures recent transactions, not the full historical
  position ledger required to reconstruct current holdings.
- Congressional PTRs disclose ranges and comments. Close replication needs
  comment parsing for exact share counts, option exercise semantics, spinoffs,
  gifts/contributions, and historical open lots.
- Missing ticker coverage in `prices_daily` degrades reconstruction unless new
  trader symbols are added to the watchlist and refreshed.

Before treating this as a close replication engine, build the official
House/Senate PTR importer, ingest each trader's historical filing set, parse
share-count comments where present, and add a benchmark report that compares
derived holdings against a chosen reference portfolio without ingesting that
reference as source data.
