# Decision loop, experiment accounting and learning: September 21 repair

## Product acceptance

A current watched or owned instrument must have a complete, coherent decision or a
named producer incident. A conditional price setup is supporting analysis, not a
second capital instruction. A complete CASH decision is not a missing TradePlan.
An entered research experiment must have recorded entry terms, observed marks,
management status, and a terminal outcome or an explicit evidence failure.
Neither a green HTTP response nor a trade count proves the system works.

These changes are paper-only. They do not enable a brokerage, fund an account,
restore paid model generation, weaken qualification gates, or create retrospective
fills. Existing strategy thresholds and account authorization remain in force.

## Root causes and implemented repairs

| Root cause | Repair and owner |
| --- | --- |
| The funnel counted historical published symbols outside the user's current universe. | `load_decision_funnel` resolves the canonical watched/owned stock population; explicit empty scope never becomes all symbols. Crypto retains its separate continuous signal path. |
| Each downstream absence looked like another independent service failure. | Funnel stages now expose reached, passed, first-blocked and not-reached counts. Raw independent availability remains in diagnostic details; each symbol has one first blocker. |
| A missing fundamental alpha row overwrote an available tactical row. | Horizon aggregation prefers an available qualifying artifact instead of last-row-wins. |
| CASH's unevaluated stock impact was called a missing portfolio snapshot. | The fast path records `stock_candidate_not_qualified`; downstream stages are not reached, rather than inventing an account outage. |
| Python compared absent optional plan/rank identities as `None` versus an empty string; SQL unconditionally required them. | Python and both SQL Today projections accept matching absent optional identities only for a blocked CASH plan. Publication, rank and expression identities remain mandatory. One-sided loss and conflicting nested impact identities remain failures. |
| Ranking publication searched the wrong level of the ticker/horizon alpha mapping. | The publisher now flattens horizon artifacts when binding the qualifying strategy revision. |
| The experiment worker overwrote the current return but recorded no price path. | Migration `20260921_0033` adds an append-only quote-backed event journal. Entry, mark, exit and valuation gaps are written in the same transaction as the shadow position. |
| An entered experiment retained its old `limit_not_reached` admission task. | Entry clears the pending reason; lifecycle projection shows current stop/target/time-exit management or a quote incident. Frozen admission instructions remain in audit details. |
| An experiment was shown next to a funded NAV chart without its own performance view. | Open experiments are prioritized and expanded with separate dollar-P&L curves, entry/exit markers, source clocks, fees and an event ledger. They are not added to funded account NAV or counted as funded fills. |
| Funded chart event data contained price and quantity but the tooltip omitted them. | The chart now shows time, symbol, price, quantity and P&L; a non-trade marker cannot select an unrelated trade. |
| Forecast-generation pause also removed outcome settlement from the schedule. | Settlement is scheduled independently, still respecting explicit scheduler/replay disablement. It makes no model calls. The UI distinguishes generation, pending claims and independent resolved evidence. |
| Research said “waiting for first fill” despite active experiments. | Learning shows exact research collection counts and management clocks separately from funded fills and validation floors. Stalled collection is an operational problem, not statistical progress. |
| Health could ignore active experiments with a ticking manager but no valid quote. | Health inspects active-position checks and actual provider quote clocks, reports owner jobs, and uses market-session elapsed time. A weekend alone does not make an equity mark stale. |
| Today displayed setup and capital decision as competing instructions, then repeated a missing-plan card. | A shared component leads with the canonical published capital decision and its actual constraint. Conditions are secondary. Only the exact duplicate capital projection is suppressed; holding-risk and independent inbox items remain. |
| Today required same-publication brief coverage but its producer never wrote it. | The preopen publisher now writes scoped research/portfolio coverage and explicit bounded source/calendar excerpt coverage. A partial calendar is never called an empty complete calendar. |
| Open screens did not pick up subsequent marks and decisions. | Today and experiment reads poll with abort/unmount guards and retain explicitly labelled historical data when refresh fails. |

## Accounting and integrity

Each options research observation tracks one contract with a multiplier of 100.
The journal records the existing execution model: entry at the qualifying ask,
liquidation at a later qualifying bid, less entry and modeled exit fees. Initial
marked P&L includes the spread and round-trip costs; it need not start at zero.
This is a research experiment, not an account ledger or a claim of exchange fills.

The original decision, publication and admission clocks remain unchanged. The
execution owner still requires complete, causal, later quotes, liquidity and
entry/exit constraints. The quote that filled entry can explain the initial mark
but cannot immediately fill an exit. Re-ingesting the same provider tick does not
produce another mark. Gap records have no numeric P&L; the chart breaks at gaps
rather than drawing a false flat or zero return. Unknown/future clocks are not
current valuations. Transaction rollback removes both position changes and events.

The application role receives SELECT/INSERT on the event table, not UPDATE,
DELETE or TRUNCATE. Existing experiments are **not backfilled**. Their recorded
entry snapshot remains visible, and new marks start a prospective history. The
history endpoint is read-only, UUID-scoped, bounded to 2,000 events with explicit
truncation, and does not mix other shadow kinds. Old snapshots do not establish an
unobserved historical price path. Unmeasurable observations remain excluded from
completed performance and learning evidence.

## Learning and qualification

Stock-alpha qualification, options research experiments, funded paper orders, and
advisory prompt evaluation are distinct processes. They now have consistent
visibility, not interchangeable evidence. A measured trend is not a calibrated
forecast. Rejected or unfilled candidates are not zero-return trades; open
observations are not independent completed outcomes. The existing stock walk-
forward worker still needs genuine point-in-time matured outcomes and controls.
No successful OOS evaluation or profitable trade is synthesized to make a counter
nonzero. A required worker that never runs or is disabled is a named health issue;
a completed validation that rejects a strategy is a legitimate capital constraint.

The checked-in configuration pauses new advisory generation after earlier
provider failures. That setting is preserved. Outstanding claims can settle
without paid model calls; the generation pause is shown on Overview and Forecast
quality. Enabling generation still requires the existing Agent controls/configuration
and working authorized provider access.

## Verification

Focused regression coverage includes the real PostgreSQL path from experiment
admission through later entry, mark, duplicate quote, gap and exit; after-fee dollar
P&L; transaction rollback; application-role append-only grants; scope isolation;
CASH identity SQL/Python agreement; same-publication brief coverage; functional
source-to-feature-to-decision publishing; generation-pause/settlement scheduling;
weekend versus open-session liveness; and frontend curve/decision rendering.

Run the normal release gate without reducing its thresholds:

```bash
make release-gate
```

Targeted commands for diagnosis:

```bash
uv run --extra test python -m pytest tests/test_decision_loop_lifecycle.py tests/test_trade_plan_contract.py tests/application_api/test_decision_funnel.py tests/options/paper_execution/test_options_experiments.py tests/postgres/test_postgres_panel_catalog.py tests/postgres/test_signal_service_integrity.py tests/postgres/test_postgres_today_analysis.py -q
npm --prefix frontend run test:frontend
npm --prefix frontend run build
```

These fixtures prove implementation behavior, not access to the deployment's
provider credentials, NAS, account state, or future market liquidity. PR checks
record the tested commit and actual outcomes; this document is not a claim that
an unobserved deployment passed verification.

## Deployment and acceptance on the running app

1. Apply the normal migration using the migration-owner environment before
   starting the updated API and workers: `uv run market-db-migrate`.
   The schema must report `20260921_0033`.
2. Restart API and scheduler on this revision and rebuild the frontend with
   `npm ci --prefix frontend && npm --prefix frontend run build`. Do not leave an
   old worker or browser bundle running against the new schema.
3. Let the configured quote, feature, Market, ticker-decision, Today and paper
   workers run. Existing supported repair jobs remain available in Agent controls;
   do not run writes from a browser diagnostic/read endpoint.
4. Check one open experiment: recorded entry time/price, original quote clocks,
   first new mark, costs and event ordering must agree with its ledger. A later
   executable exit must update status, P&L and the learning collection count once.
   An old observation starts at its first newly recorded mark, not a fake entry
   curve. Funded paper NAV must remain separate.
5. Verify a watched/owned ticker has the same capital instruction in Today and
   detail views. A complete CASH plan must not produce “missing plan.” A price
   setup alone must not authorize an allocation. The funnel must exclude old,
   unwatched symbols and expose the actual first failing stage.
6. Pause or disconnect a required quote worker in an isolated verification
   environment: active-position quote/management incidents must appear, and
   clearing the fault must allow new marks. US closed-session references are
   retained with their original times; crypto gets no weekend exemption.
7. Verify generation-paused forecasts still reach settlement after maturity when
   replay is enabled. Independent sample counts advance only after actual valid
   resolved evidence. Provider or permission failures must be visible, not
   described as a market no-trade decision.

The existing `scripts/verify_workstation.py` provides read-only release/health
checks. Use its `--help` for the deployed API URL, commit and schema options.
There is no requirement to force a trade on a rising market day: the required
behavior is autonomous, auditable decision and position management with visible
producer failures and real execution evidence.
