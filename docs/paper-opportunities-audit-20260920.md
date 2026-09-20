# Paper trading and opportunities audit — September 20, 2026

Baseline: `696be6761122ba0051aeba98740555cda2c2b7a2`. This revision preserves the
September 20 options candidate, binding-lineage and local-verification fixes.
It changes the existing owners, not the app's execution authority or risk budget.
[Product goals](product-goals.md) and [architecture](../ARCHITECTURE.md) are the
current contracts; this document records this audit and its verification.

## Plan and implementation

### 1. Separate market observations, research freshness and execution

Implemented in the decision calendar/freshness domain and paper workbench:

- Retain the last completed-session price and its original observed/available
  timestamps through weekends, holidays and early closes. A missed completed
  session, absent leg clock, impossible clock ordering, or unsupported settlement
  remains an explicit gap. Crypto uses elapsed time, not the US holiday calendar.
- Keep the four clocks distinct: observation, source availability, decision
  cutoff and presentation/read time. A successful refresh or new ingestion ID
  never changes the age of the original observation.
- News freshness uses elapsed hours. Macro/source jobs retain their existing
  configured publication/refresh cadences outside equity sessions. Their worker
  states are now visible in workstation status. The scheduler already separated
  research jobs from the session-only advisor; the change adds regression
  coverage, not a second scheduler or a promise that every macro release changes
  hourly.
- Present verified last-session marks as valuations, not live executable prices.
  A package with one missing timestamp cannot borrow another leg's timestamp.
  Missing option contract identity is an unavailable mark, not an exception.

### 2. Correct paper execution and partial-order lifecycle

Implemented in the shared paper quote-consumption rule and both ticker and
Radar execution owners:

- Entry/exit fills require a regular US session, source observations from that
  session, observed and available clocks at or before the cutoff, and observations
  no older than 120 seconds. Valuation carry-forward does not relax these gates.
- Require post-order/post-fill observations. Persist per-order, per-leg quote
  consumption with the fill journal in the existing transaction and shared
  sleeve lock. Repeated ticks, or a new ingestion row for the same source
  observation, cannot refill against the same displayed liquidity.
- Legacy fills without a consumption ledger require a new post-fill quote rather
  than manufactured historical provenance. Invalid consumption structures fail
  closed with a named blocker.
- Apply existing option quote quality, spread, size and skew policy to ticker
  option execution, with consistent positive contract multipliers.
- Cancel only the unfilled remainder of an expired partial entry, then manage
  the filled position in the same and later ticks. Previously the expired entry
  branch could repeatedly prevent holding/exit management.
- Interpret date-only entry deadlines at the exchange close rather than midnight
  UTC. Cash-secured-put expiration requires the exact confirmed expiration-day
  close after the actual session close; no early settlement against an older
  intraday quote. Missing settlement evidence stays pending.

There is deliberately no live broker submission, risk-gate relaxation, automatic
funding, strategy promotion or configuration change enabling more trading.
A quiet paper book on Sunday is correct. “The worker succeeded” and “an eligible
order received a new fill” are different assertions.

### 3. Make Today and Opportunities actionable without rewriting history

Implemented in existing read-model, Today workflow and presentation owners:

- Current-time overlays block expired or future-cutoff actionable plans. Retain
  the immutable published plan for inspection; do not silently rewrite its
  eligibility, price, timestamp or decision identity.
- Keep research useful while markets are closed. Closure alone does not remove
  supported research or last-session prices.
- Missing-plan guidance names entry, size, invalidation and maximum loss and
  points to the ticker assessment and decision-model refresh. Specific ranking,
  identity or source failures keep their own blocker and next action.
- Replace vague Today evidence links with ticker/destination-specific labels.
  Missing or expired plans cannot leak old entry terms into an actionable card.
- Preserve separate real, funded-paper and research-observation books. No
  unavailable mark is converted into a zero or a fictitious NAV improvement.

### 4. Reuse verification interfaces and update the product contract

Implemented: optional WebMCP adapter below; extend the existing
`scripts/verify_workstation.py`; update README and ARCHITECTURE; add the canonical
product-goals document. No new service, database migration, API endpoint,
provider dependency or generated API schema is required.

## Verification matrix

Automated regression coverage includes:

| Area | Cases |
| --- | --- |
| Market clocks | Friday/Sunday, holiday weekend, early close, DST, missing/future source clocks, continuous-market elapsed age |
| Execution | Closed session, preopen quote at open, stale quote, pre-order quote, repeated quote ID, duplicate ingestion with the same observation, each spread leg advancing, legacy fill provenance |
| Holding | Expired partial-entry remainder, next-tick holding management, partial stock exit and original-lot accounting |
| Marks | Missing contract ID, incomplete package clocks, last-session labels and preserved timestamps |
| Opportunities | Expired terms, future cutoff, immutable plan retained, useful research/valid terms during closure |
| Today | Named missing fields, actual blocking reason retained, no old terms or vague evidence link |
| Diagnostics | Optional/unsupported browser, read-only request allowlist, argument limits, cancellation, registration rollback, bounded results and private-error redaction |

Focused tests run through the existing owners and PostgreSQL-backed lifecycle
suite. Some old execution fixtures used Saturday quotes; those now use explicit
regular sessions and distinct source observations for distinct fills. This is
not a relaxation of the new guards. The unchanged full release gate remains
required; see the PR for final results and any environment-specific failures.

```sh
uv run pytest tests/test_market_clock_semantics.py tests/test_paper_quote_consumption.py \
  tests/test_ticker_execution_clock_regressions.py tests/test_opportunity_display_currentness.py \
  tests/test_paper_workbench.py tests/test_workstation_readiness.py \
  tests/test_workstation_verification.py -q
uv run pytest tests/postgres/test_ticker_paper_execution.py \
  tests/options/paper_execution/test_options_paper_execution.py -q
npm --prefix frontend run test:frontend
make release-gate
```

## Read-only local verification

Use the deployed app's normal authenticated/local access. The existing verifier
now checks nine bounded GET surfaces: runtime, workflow, Market, paper
performance, account history, research, Today, Opportunities and paper trades.
It outputs allowlisted counts/states rather than tickers, account holdings,
prompt text or provider errors. Warning/partial is not a pass and is never
silently recast as “no opportunities.”

```sh
uv run python scripts/verify_workstation.py --help
uv run python scripts/verify_workstation.py \
  --base-url http://127.0.0.1:8010 \
  --expected-commit "$(git rev-parse HEAD)" \
  --output /tmp/market-verification.json
```

Before the next session: check that Friday's supported marks retain their
Friday timestamps; closed-session labels are visible; no Sunday fill appears;
news/macro/calendar workers have their own schedule/status; expired plans show
specific recovery actions; supported research and reference prices remain
visible. A fresh API response is not a fresh quote.

During the next regular session, with existing authorized paper settings and
qualified orders only: verify the pending reason clears only after a new
eligible source observation; repeated polls cannot duplicate fills; spread legs
must all advance; partial entries/exits reconcile quantity, fees, cash and NAV;
an expired entry remainder does not prevent risk management. No fixed number
of fills or profitable trades is an acceptance criterion.

Finally inspect the actual browser at desktop and mobile widths: Today links,
Opportunities filters/details and the paper timeline. API checks do not prove
DOM layout, deployed asset parity, native browser tool availability or provider
behavior. Compare the backend commit and built frontend before interpreting a
stale screenshot as a code regression.

## WebMCP

Decision: **yes, as an optional read-only inspection adapter, not the debugging
foundation**. The existing API clients, backend policy and browser tests remain
authoritative. WebMCP is experimental and feature-detected; unsupported browsers
continue to run the normal application.

This adapter targets the September 2026 imperative API:
`document.modelContext.registerTool(tool, {signal})`, string results and
abort-driven unregistration. It does not install a polyfill or use the older
`navigator.modelContext` examples. Sources checked for this implementation:

- [Chrome imperative API, updated September 11, 2026](https://developer.chrome.com/docs/ai/webmcp/imperative-api)
- [WebMCP draft specification](https://webmachinelearning.github.io/webmcp/)

Enable only for a trusted local session with a compatible browser and its
required experimental feature/origin-trial setup. A flag in Market does not
install browser support. Development:

```sh
VITE_MARKET_WEBMCP=true npm --prefix frontend run dev
```

Built assets require the flag **at build time**, then the normal server restart:

```sh
VITE_MARKET_WEBMCP=true npm --prefix frontend run build
```

Five tools use the same typed GET clients as the UI:
`market_inspect_workstation`, `market_inspect_today`,
`market_inspect_opportunities`, `market_inspect_paper`,
`market_inspect_paper_trade`. Paper inspection is scoped to the paper book;
arguments accept only bounded counts, symbols or a UUID. There is no arbitrary
URL, refresh-job, order, approval, funding or settings tool.

In a compatible browser's DevTools, inspect and execute using the current API:

```js
const tools = await document.modelContext.getTools();
const tool = tools.find(t => t.name === "market_inspect_workstation");
await document.modelContext.executeTool(tool, {});
```

Registration is cleaned up on unmount, including partial failure. In-flight
reads receive cancellation, a 15-second limit, a two-call concurrency limit and
a 256-KiB result cap. Server partial/blocked states are preserved; transport
errors are reduced to safe codes. Source prose is marked untrusted data and
must never be interpreted as tool instructions.

Read-only does **not** mean public: diagnostic results can contain private
financial research and paper positions already available to the browser.
Keep the flag off by default; authorize only trusted agents/browser sessions.
WebMCP annotations describe behavior and are not an access-control boundary.
Adapter tests use a model-context double; a real Chrome WebMCP session still
needs local verification. Playwright/DevTools-style DOM testing is complementary,
not replaced by successful tool registration.

## Remaining model and verification limits

Stock paper execution still uses a labelled confirmed last-price model, not an
exchange depth/queue simulator. Option displayed-size consumption is per order,
not a reconstructed global order book across all orders/venues. Cash-secured-put
expiration records the existing paper intrinsic-value settlement model, not a
broker's physical assignment and stock delivery. Unsupported settlements remain
visible gaps; do not infer a verified return from them.

This audit does not certify the user's running deployment, provider entitlements,
current production records, every strategy's statistical edge or profitability.
Tests establish behavior under their inputs. Prospective outcomes, realistic
costs and reconciled journals—not extra model prose—establish trading evidence.
