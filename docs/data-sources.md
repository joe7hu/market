# Verified Data Sources

Verified on 2026-05-10.

## SEC EDGAR

- Official JSON APIs are free and do not require authentication.
- Use server-side requests. `data.sec.gov` does not support CORS.
- Declare a `User-Agent` identifying the app/contact.
- Stay at or below SEC fair-access guidance: no more than 10 requests per second total per user.
- Useful endpoints:
  - `https://data.sec.gov/submissions/CIK##########.json`
  - `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`
  - `https://data.sec.gov/api/xbrl/companyconcept/CIK##########/taxonomy/tag.json`

Sources:
- https://www.sec.gov/search-filings/edgar-application-programming-interfaces
- https://www.sec.gov/about/developer-resources

## SEC Form 13F

- Official 13F datasets are quarterly ZIP downloads, public, and cover May 2013 through current period.
- Format: UTF-8 tab-delimited files plus metadata. Key files include `SUBMISSION`, `COVERPAGE`, `SUMMARYPAGE`, and `INFOTABLE`.
- Caveats: data is as-filed, amendments can duplicate or contradict earlier rows, and SEC says the dataset is not a substitute for full filings.

Sources:
- https://www.sec.gov/files/form_13f.pdf
- https://catalog.data.gov/dataset/form-13f-data-sets

## CoinGecko

- Demo/free API host: `https://api.coingecko.com/api/v3`.
- Demo plan: 30 calls/minute and 10,000 calls/month. Error responses can still count against minute limits.
- Useful endpoints:
  - `GET /coins/markets`
  - `GET /coins/categories`
  - `GET /coins/{id}/market_chart`
- Prefer CoinGecko IDs over symbols/names where possible.

Sources:
- https://docs.coingecko.com/reference/coins-markets
- https://docs.coingecko.com/reference/coins-categories
- https://docs.coingecko.com/docs/common-errors-rate-limit

## DefiLlama

- Free unauthenticated API host: `https://api.llama.fi`.
- Useful endpoints:
  - `GET /protocols`
  - `GET /protocol/{protocol}`
  - `GET /tvl/{protocol}`
  - `GET /overview/fees`
  - `GET /summary/fees/{protocol}`
- Yield pools use `https://yields.llama.fi/pools` in practice.
- TVL is not activity. Pair TVL with fees, revenue, volume, and category context.

Sources:
- https://defillama.com/docs/api
- https://docs.llama.fi/

## yfinance / Stooq

- `yfinance` is unofficial, not affiliated with Yahoo, and intended for research/education/personal use.
- `pandas-datareader` supports Stooq through `StooqDailyReader`, but Stooq is website-backed rather than a contracted API.
- The app treats both as replaceable fetch adapters and keeps deterministic fallback data for local development.

Sources:
- https://pypi.org/project/yfinance/
- https://pydata.github.io/pandas-datareader/readers/stooq.html

## OpenCLI / TradingView

- OpenCLI provides local read adapters. Market uses it through a narrow
  allowlisted provider interface, not by letting arbitrary app code construct
  commands.
- The TradingView adapter in `himself65/finance-skills` is read-only and
  attaches to the logged-in TradingView desktop app over CDP.
- Useful data surfaces: quotes, screeners, news, watchlists, alerts, chart
  state/screenshots, options expiries, and options chains with greeks/IV.
- Caveat: availability follows the logged-in TradingView account/session and
  desktop app state.
- Market stores TradingView personal surfaces as read-only metadata:
  `tradingview_symbol_search`, `tradingview_watchlists`,
  `tradingview_alerts`, and `tradingview_chart_state`. Failures on these
  personal surfaces are non-blocking and should be recorded through
  `provider_runs` / `source_health`.

Sources:
- https://github.com/jackwener/opencli
- https://github.com/himself65/finance-skills/tree/main/opencli-plugins/tradingview

## Decision Freshness

Decision-grade read models must evaluate source freshness from source-specific
contracts, not from row existence alone.

- Intraday quotes, options, and news: stale after `4` market hours.
- Daily prices, technicals, SEPA, liquidity, and correlations: stale after `1`
  trading day.
- Fundamentals, 13F filings, and trader disclosures: stale by filing cadence.
- Arco thesis evidence: stale after `7` days unless refreshed or reinforced.
- Documentation rows, including this file, are documentation coverage only and
  must not be surfaced as healthy provider runs.

## Finance-Skills Codification Contract

Market imports ideas from `himself65/finance-skills`, but it should not run a
skill prompt where deterministic code is enough.

Codified in backend code:

- TradingView reader: provider adapter plus normalized tables for quotes,
  screeners, options, news, symbol search, watchlists, alerts, and chart state.
- Options payoff: Black-Scholes/theoretical and expiry payoff scenarios from
  stored option-chain rows.
- Earnings/estimate analysis: revision momentum, surprise quality, estimate
  spread, and analyst-target setup scores from stored yfinance rows.
- Valuation: DCF base case, relative revenue multiple, and blended valuation
  rows from stored fundamentals, quotes, and market caps.
- SEPA, liquidity, correlation, and ETF premium analyses remain deterministic
  read models.

LLM use is reserved for unstructured inputs: transcript/filing/news synthesis,
memo prose, qualitative assumption selection when structured data is absent, or
parsing a user-provided options screenshot/free-form strategy into normalized
legs before the deterministic payoff engine runs.
