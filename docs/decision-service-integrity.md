# Decision-service integrity: root causes, repaired contracts, and verification

## Product contract

The first screen shows **BUY SETUP / EXIT SETUP / HOLD / WAIT / AVOID**, with the
measured conditions and their original clocks. A required producer failure is a
**SIGNAL SERVICE FAILED** incident, not an investment verdict and not an empty
metric. System health names the instrument, failed contract, owner job, last
observation, run error, and expected cadence.

A measured setup and an authorized capital allocation are different outputs.
`ReferenceSignal` is a versioned interpretation of the existing `daily_trend`
feature. It never supplies calibrated odds, an expected return, a funded quantity,
or order permission. `TradePlan`, qualified forecasts, account risk and execution
quotes retain those authorities. A complete, validated **WAIT** is a useful answer;
an absent input or publication is a service defect. More trades are not an
acceptance criterion.

## Why earlier changes did not solve the screenshots

The previous implementation tested local rejection gates and the presence of
read models more thoroughly than delivery across the whole producer chain.
Repeatedly improving blocker copy did not create the missing facts or correct
their wiring. The concrete defects addressed here are:

| Defect | Repair and owning contract |
|---|---|
| Ticker quote age used wall-clock minutes on Sunday | `assessment_quote` validates original observation/confirmation times and the most recent completed US session. Crypto retains elapsed-time freshness. |
| No independent quote producer for every watched/owned instrument | `refresh_assessment_inputs` collects bounded public reference quotes for the canonical monitored universe. It records per-symbol provider failures and does not authorize fills. |
| A 260-calendar-day fetch cannot reliably supply a 200-session feature | Daily history collects at least 500 calendar days for US equities, 360 for crypto, plus the explicit QQQ benchmark. Empty or wrong-symbol provider responses fail. |
| Stock feature production was capped by the options shortlist | `refresh_symbol_features` resolves all monitored instruments independently of option-radar selection and reports each failed feature. |
| Crypto "candles" were approximated from sampled prices | Coinbase Exchange daily OHLCV is fetched in bounded pages, with genuine Yahoo OHLCV as fallback. No synthetic high/low/volume is manufactured. Crypto daily availability starts after UTC bucket completion. |
| Research/review cards were treated as if they supplied complete trading terms | The existing daily-trend feature now produces a strict, versioned `ReferenceSignal` with complete conditional terms or a named service failure. It is stored inside the same ticker/ranking publication, not a browser recommendation engine. |
| Fundamental expressions consumed tactical conditions | Each expression uses the entry, target and invalidation belonging to its selected horizon. |
| Validation compared a trial ID with a dossier ID | The qualified-stock path compares evaluation and dossier **research trial IDs**. |
| Reused Market publications dragged the consumer cutoff into the past | Market lineage remains immutable, but each new ticker publication uses a new cutoff after its producers are visible. Today is published after ticker decisions. |
| A missing quote/account clock could be replaced by ingestion time | Explicit provider observation and account-observation clocks are required; unknown clocks cannot establish causal eligibility. |
| CASH had a special selection-dependent impact identity | CASH now uses the same canonical expression identity as the other expressions before and after selection. |
| A prospective observation could expire before it was admitted | The 30-minute observation entry window begins at actual publication-visible admission and counts US trading minutes, including holiday/early-close boundaries. |
| Forecast collection/replay treated all assets as an equity-session workload | Research continues outside US execution hours, using the shared quote-use contract. Crypto outcome windows are continuous; equity outcomes use their next observation session. |
| Evidence-blocked forecast attempts could report successful collection | Blocked required evidence degrades the collector result. Mature unresolved outcomes and their producer owner are visible in Health; legitimate pending/unsupported outcomes are not scored as losses. |
| Large ingestion finalization used the 3-second API transaction budget | Finalization uses the existing bounded worker profile while retaining atomic confirmation and projection writes. |
| HTTP/query success was labelled functional health | `/api/status` separates `transport_ready` from `service_ready`. Functional readiness requires monitored quote -> feature -> current signal -> ranking/plan contracts and required workflow health. |

## Input and output authorities

- **Monitored universe:** configured watchlist, persisted watches, and nonzero owned
  positions. An explicit unwatch excludes an unowned instrument, never an owned
  position. A symbol missing from the catalog remains a named incident. An
  explicit empty scope is not interpreted as "all symbols".
- **History:** confirmed daily OHLCV, exact completed-session coverage, real
  provider source IDs. Missing candles are not interpolated. The benchmark is a
  declared dependency, not a recommendation.
- **Assessment quote:** positive finite price; timezone-aware, causal
  `observed_at <= available_at <= cutoff`. US closed-market references must cover
  the last completed close (with a 15-minute closing-trade allowance). Open US
  sessions and crypto require an observation within 15 minutes.
- **Feature:** completed `daily_trend` run and feature revision; complete expected
  session; measured ATR/trend metrics. Failed latest features stay visible in
  diagnostics rather than being hidden by older successful rows.
- **Conditions:** immutable signal and cutoff, original quote time, feature
  revision/session, explicit expiry. Read-time projection and the browser's
  expiry timer remove expired terms without rewriting historical evidence.
- **Allocation:** the existing rank/forecast/TradePlan/account-risk contracts. No
  positive probability or sample count is invented to unlock them.
- **Execution:** independent later, causal, executable quote, liquidity and risk
  checks. The new `assessment-*-quotes` sources are explicitly excluded by the
  ticker paper-execution selectors. Observations are not funded-account P&L.

### Measured trend policy, not estimated alpha

`daily-trend-conditions.v1` uses the existing completed-session trend classifier.
For an uptrend, its reference entry band is completed close +/- 0.25 ATR,
invalidation is 2 ATR below that close, and objective is 4 ATR above it. ATR is a
fraction in the feature contract, not percentage points. Planned per-unit risk
is entry-band high minus invalidation; it is not a guarantee against gaps or
slippage. The rule and its provenance are explicit and testable.

A downtrend gives an exit **assessment** for an owned long and AVOID for an
unowned instrument. A range/transition gives HOLD or WAIT. A price outside the
entry band is a condition to wait for, not a market-order instruction. These
rules supply usable conditions without pretending that a new policy has already
passed historical/forward validation.

## Health semantics

`/api/workstation/status` is the operational diagnostic authority. `/api/status`
includes the same result: a working database with failed required producers is
not ready. Important distinctions:

| Condition | Expected result |
|---|---|
| Friday equity close viewed on Sunday | Closed-session reference, original timestamp; not stale merely because the venue is closed. |
| Same-aged BTC quote on Sunday | Quote-collector incident; there is no weekend exemption. |
| Complete signal with no qualified allocation | Signal visible, capital decision WAIT; not a data outage merely because no trade passed risk/edge checks. |
| Missing candle, feature, quote clock, symbol, rank or plan | Named per-instrument service incident and non-ready overall health. |
| Required worker failed, overdue, never started or disabled | Named workflow incident; successful unrelated jobs cannot mask it. |
| Young forecast cohort | Issued, pending, next-maturity and resolved counts. No empty Brier/accuracy score presented as performance. |
| Mature forecast cannot resolve | Overdue-outcome incident with the resolver/quote dependency identified; not counted as a failed prediction. |
| Unsupported/invalid forecast | Explicitly excluded from scoring, never converted to a fabricated outcome. |
| Disabled optional/unsupported licensed feed | Capability/configuration status; no fabricated substitute and no claim that all possible data can be acquired. |

The existing research drill-downs retain raw evidence and qualification gates for
audit. They are not the primary task list, and missing required data must remain
visible in diagnostics instead of being cosmetically hidden.

## Deployment / first-run recovery

No database migration or historical rewrite is required. Existing immutable
publications without the new signal contract remain inspectable but fail current
signal health until republished. Restart both backend/scheduler and rebuild the
frontend so their versions agree. The scheduler still has two execution slots.

For an explicit one-time repair, use the existing persisted job runner so failures
are recorded in `ops.job_run`, not just printed by a one-off script:

```bash
uv run python -m investment_panel.core.refresh_jobs update_market_data --config config.yaml
uv run python -m investment_panel.core.refresh_jobs refresh_symbol_features --config config.yaml
uv run python -m investment_panel.core.refresh_jobs refresh_assessment_inputs --config config.yaml
uv run python -m investment_panel.core.refresh_jobs refresh_market_publication --config config.yaml
uv run python -m investment_panel.core.refresh_jobs refresh_decision_models --config config.yaml
```

These commands collect facts and publish assessments. They do not fund an
account, place live orders, buy licensed feeds or change provider credentials.
The application remains advisory/paper-only. Existing configured paper workers
retain their existing authorization and risk rules.

Default reference-quote and decision cadence is five minutes; monitored features
refresh every fifteen minutes. Heavy history refresh has its own schedule.
News, events and macro sources retain their own independent cadence. A reference
price refresh is not evidence that news or macro facts have been updated.

## Verification and release acceptance

New focused checks:

```bash
uv run python -m pytest tests/test_decision_service_integrity.py \
  tests/providers/test_assessment_sources.py \
  tests/postgres/test_signal_service_integrity.py -q
npm --prefix frontend run test:frontend -- src/components/market/ReferenceSignalCard.test.tsx
```

The PostgreSQL tests use an isolated migrated database. They seed real-shaped
OHLCV/quote facts, compute features, run the actual ticker publisher, read back
the immutable signal/rank/plan, and query Health. They assert that no paper order
is created from a reference setup. They also verify missing instruments, failed
features and UTC crypto bucket completion.

The regression suite checks horizon/impact identities, quote/account clocks,
reference-source execution exclusion, future/expired terms, holiday and
Friday-to-Monday admission, public-provider paging/failure handling, generated
API schemas, frontend rendering, and production builds. The release gate remains
`make release-gate`; it is not weakened to force a green result.

Deployment acceptance is deliberately separate from workspace tests:

1. Each configured watched/owned instrument has a current signal or a specific
   failed service incident. No healthy label is based on job/query success alone.
2. US equity references survive a weekend with original observation times;
   crypto quotes and research/forecast jobs continue on their real cadence.
3. Disconnect a required source: affected symbols and the exact owner appear in
   Health. Restore it and let the producer chain republish: the incident clears.
4. Confirm actual provider entitlements, paid-source credentials, NAS access,
   scheduler environment and live quote latency on the deployment host.
5. Inspect subsequent prospective fills and mature forecast outcomes. No local
   fixture proves that an unobserved future trade is profitable or that live
   provider connectivity is healthy.

Functional health also checks recorded capital-decision and risk-policy failures.
A working trend card does not hide an absent or expired account snapshot, an
inconsistent ranking identity, or a missing Market publication. These incidents
name their producer separately from signal conditions. An unvalidated strategy,
negative utility or an exceeded risk budget remains a legitimate WAIT, not an
operational outage.
