# Market: From incomplete signals to a dependable daily trading assistant

## 1. Target and review findings

Build a daily decision partner for your current holdings and watchlist. Prioritize stocks and options over days to months. Make a reconciled manual account the first supported portfolio authority. Keep execution paper-only. Use existing data sources first; consider paid data only after a measured coverage gap establishes the need.

The main problem is the incomplete path from **source → stored facts → analysis → decision → portfolio action → outcome**. The app has substantial infrastructure, but parts are undeployed, disconnected, or unable to produce the evidence that their own gates require.

### Verified baseline

This review used the current checkout, GBrain campaign context, read-only PostgreSQL queries, live API calls, browser inspection, and isolated adapter probes. It did not run the full regression suite or change the app.

| Area | Finding | Effect |
|---|---|---|
| Deployment | Live database revision is `0058`; current source expects `0077`. Local `main` is 30 commits ahead of remote `main`. | New research, forecast, and allocation tables do not exist in the live database. |
| Campaign closure | The GBrain checkpoint records unresolved Phase 4 findings against an older candidate; newer repairs exist locally. | Completion and deployment must be established against the exact current candidate. |
| Command Center | Of 100 displayed queue items, 96 are expired transitions, three are blocked capital actions, and one is a current portfolio risk. | Old events dominate the daily workflow. |
| Market coverage | The published matrix has 42 unavailable cells out of 48. This includes unsupported horizons and assets. | Coverage must be measured against the chosen workflow, not a broad market-wide denominator. |
| Ticker decisions | TSLA and NVDA have missing decision prices, entry ranges, targets, invalidation, confidence, and supporting/opposing evidence. | Directional labels do not supply a usable decision. |
| Conflicting presentation | TSLA displays directional `BUY` views beside capital action `AVOID` and selected expression `CASH`. | The distinction between a view, permission to trade, and position management is unclear. |
| Opportunities | The live workspace and its snapshot contain no opportunity rows. | Discovery does not reach the main comparison surface. |
| Research | The live research snapshot has no packets, memos, earnings, estimates, or valuations despite substantial stored fundamental data. | Stored data volume does not establish usable research coverage. |
| Account authority | Eight ledger positions are visible, but broker account and position snapshot tables are empty. New allocation code depends on broker-derived account evidence. | Manual holdings cannot currently support the complete allocation path. |
| Collectors | The full refresh omits the Phase 2 collector and company-financials collector. Registration alone does not provide scheduled collection. | Important data can remain absent indefinitely. |
| Adapter contracts | Provider-shaped FRED and Alpha Vantage examples produce zero observations. Default SEC/options Phase 2 paths return empty payloads. | Credentials and deployment alone will not repair coverage. |
| Reliability | Portfolio scope returned HTTP 503 on one probe and succeeded later. Inbox synchronization repeatedly fails because its relay is not configured. | Health must measure usable workflows and separate local processing from delivery. |

The provider mismatch is concrete: FRED supplies observation and vintage fields; Treasury supplies an XML feed. The current collector/parser path expects a different normalized shape. Normalization must be implemented at the provider boundary. [FRED API](https://fred.stlouisfed.org/docs/api/fred/series_observations.html), [Treasury feed](https://home.treasury.gov/treasury-daily-interest-rate-xml-feed), [Alpha Vantage documentation](https://www.alphavantage.co/documentation/#earnings).

**Success means fewer unexplained gaps and useful decisions with explicit uncertainty. It does not mean forcing confident signals or guaranteeing profitable trades.**

## 2. Implementation phases

These are follow-on delivery phases. They do not replace or renumber the existing campaign’s locked phases. Phase 0 must establish its closure before dependent changes proceed.

### Phase 0 — Establish one verified release

**Outcome:** The reviewed code, live database, API, frontend, and scheduler run a compatible release.

- Reconcile the current candidate with the existing Phase 4 checkpoint, review findings, and acceptance gates. Verify which findings the newer repairs actually close.
- Complete the existing campaign’s required independent review and exact-candidate checks.
- Test migration from the deployed `0058` database shape through the approved head on a restored database. Include production-role permissions and rollback/re-upgrade checks.
- Back up and verify recovery before production migration. Deploy migrations, backend, frontend, and scheduler as a controlled release.
- Expose actual database revision, backend commit, frontend build, and scheduler release in System.
- Fail readiness when required schema or read models are incompatible. Do not describe a successful connection as complete readiness.
- Reproduce and fix the intermittent portfolio failure without increasing the 3,000 ms statement timeout.
- Update the existing campaign checkpoint with verified closure evidence.

**Acceptance**

- Reviewed commit equals local and remote `main`.
- Live schema matches the release; API and frontend identify the same release.
- All five workspaces and representative ticker routes load from the deployed build.
- Production-role allocation, telemetry, attribution, and funding regressions pass.
- Required campaign checks pass. Existing dirty work and campaign history remain preserved.

### Phase 1 — Make missing data and daily status actionable

**Outcome:** Every important gap has an accurate explanation and a practical resolution path.

- Extend the existing `DataFieldStateV1`, coverage contracts, and source registry. Do not create another status system.
- Define required fields by instrument type, horizon, and action. Separate trade-blocking fields from useful context and fields that do not apply.
- Trace each required field through its collector, stored fact, calculation, publication, API field, and UI consumer.
- Classify gaps as collection failure, unavailable access, insufficient history, transformation failure, stale publication, conflict, unsupported capability, or genuine analytical uncertainty.
- Attach a specific next action: runnable collection job, required account input, history requirement, or explicit access decision.
- Connect existing data requests to allowlisted jobs. Deduplicate requests and republish affected decisions after successful repair.
- Remove expired and superseded transitions from the default queue. Preserve them in history.
- Show a maximum of ten current items by default. Prioritize current portfolio exceptions, valid actions, and material research changes.
- Separate inbox calculation from notification delivery so a missing relay cannot stop local processing.

**Acceptance**

- Every decision-critical missing field has a reason, owner, impact, and next step.
- No expired item appears as a current task.
- An unavailable optional source does not block an unrelated supported action.
- A repairable missing field can move from request through collection to a new decision revision.
- An unconfigured delivery channel produces a stable configuration state, not repeated rapid failures.

### Phase 2 — Complete the manual account and portfolio ledger

**Outcome:** You can maintain an accurate account without a broker connection.

- Extend the existing append-only ledger with account cash, opening cash, deposits, withdrawals, and reconciliation snapshots.
- Preserve existing positions and transactions. Do not invent opening cash or historical transactions.
- Provide preview and confirmation for transaction changes and reconciliation, using existing idempotency and version checks.
- Calculate cash, position value, equity, realized/unrealized P&L, and reserved option collateral from explicit inputs.
- Distinguish external cash flows from investment returns.
- Record account source, effective time, recorded time, reconciliation state, and reconciliation version.
- Add manual account evidence to the existing portfolio authority path. Do not fabricate broker records to satisfy broker-specific checks.
- Update allocation readers, signing/validation rules, and database guards to accept an explicitly supported manual account source.
- Keep paper positions and their cash separate from the real manual ledger.
- Retain USD as the initial account currency. Unsupported currency or instrument accounting must be explicit.

**Acceptance**

- The current eight positions migrate without changes to quantity or cost basis.
- Cash reconciles through buys, sells, fees, dividends, deposits, withdrawals, and reversals.
- Duplicate submissions do not duplicate transactions.
- A stale preview cannot overwrite newer account state.
- Missing or unreconciled cash blocks sizing, while research remains available.
- Manual account evidence supports allocation without requiring IBKR.

### Phase 3 — Supply the data the chosen workflow needs

**Outcome:** Holdings and watchlist decisions receive usable, current inputs through scheduled production paths.

Implement in this order:

| Data group | Required work |
|---|---|
| Identity and prices | Correct issuer/ETF/option identity; quote timestamp and session; adjusted daily bars; splits/dividends; sufficient history for each supported horizon. |
| Company fundamentals | Schedule SEC collection; repair symbol-to-issuer coverage; preserve filing availability and revisions; calculate comparable quarterly and trailing metrics. |
| ETF context | Use applicable ETF data rather than requiring issuer operating-company facts. Keep unavailable holdings/exposure detail explicit. |
| Earnings and estimates | Normalize earnings dates, actuals, estimates, guidance, and revisions where access supports them. Distinguish unknown dates from no scheduled event. |
| Macro and rates | Repair FRED vintage requests and Treasury XML parsing. Map named series to canonical units and fields. Add only the dimensions used by supported decisions. |
| Options | Connect existing quote/history stores to the Phase 2 path. Preserve contract identity, bid/ask, timestamps, OI dates, volume sessions, Greeks, and coverage. |
| Research evidence | Restore Arco, filing, and other existing evidence into ticker-linked facts and packets. Preserve original references and availability clocks. |

Additional rules:

- Put missing collectors in both the scheduled job path and appropriate full-refresh dependency order.
- Normalize actual provider payloads before calling domain parsers.
- Request FRED series individually or through a documented supported endpoint.
- Read SEC and options evidence from their existing PostgreSQL owners; replace the empty default seams.
- Never invent historical publication times from ingestion times. Label conservative first-observed availability when that is the only evidence.
- Bound backfills by holdings/watchlist priority and existing storage reserves.
- For paid-data gaps, produce a comparison covering exact fields, historical depth, timestamp semantics, access terms, cost, and replay evidence. Purchase is a separate user decision.

**Acceptance**

- Each required collector passes an actual provider-payload contract test.
- A normal scheduled run creates usable facts and updates an affected decision.
- Holdings receive first priority; watchlist coverage follows.
- Supported price history meets the selected horizon requirement; short-history instruments remain explicitly restricted.
- Fundamentals distinguish valid zero, missing, stale, and not applicable.
- No new subscription is justified only by a provider’s advertised feature list.

### Phase 4 — Build complete ticker decision packets

**Outcome:** Opening a ticker explains the investment view and the next decision.

- Reuse the existing ticker decision, thesis, evidence, and trade-plan models.
- Show one primary capital action, with separate tactical and fundamental views.
- Replace unexplained `BUY`/`AVOID` combinations with explicit statements such as “bullish research view; no new trade because validation is incomplete.”
- Separate “do not add” from “exit an existing holding.”
- Include cited thesis, strongest countercase, recent material changes, catalyst, invalidation, review date, and portfolio relevance.
- Distinguish snapshot decision prices from newer display quotes. New quotes must not silently rewrite historical decisions.
- Show entry, target, loss, and scenarios only when their inputs support them. Otherwise state exactly which input or validation is absent.
- Make the compact trade plan visible without a second fetch. Load long evidence and audit detail on demand.
- Turn “what would change the decision?” into a concrete condition with a source or explicitly user-authored assumption.
- Preserve manual thesis edits and locks when automated research runs.

**Acceptance**

- Held stocks, an ETF, an option candidate, and an incomplete-data ticker each render a coherent packet.
- Material factual claims link to evidence.
- No evidence-free directional label is presented as permission to trade.
- Current quotes and historical decision inputs remain distinguishable.
- Generic empty bear/base/bull cards do not substitute for an analysis.
- A material new fact produces a traceable thesis or decision revision.

### Phase 5 — Connect research and calibration to production

**Outcome:** Existing research infrastructure produces inspectable evidence instead of remaining empty infrastructure.

- Connect the existing stock walk-forward and strategy-factory paths to research hypotheses, trials, dossiers, forecasts, and scheduled publication.
- Start with the existing daily stock trend/underreaction baseline. Complete one production path before adding families.
- Persist all eligible observations, failed trials, exclusions, costs, and subsequent outcomes.
- Separate research priority, directional evidence, validated forecasts, and trade eligibility.
- Make calibration status specific: cohort, sample count, required count, missing observations, outcome horizon, and next evaluation.
- Preserve existing promotion thresholds. Do not lower them to make signals appear.
- Permit isolated shadow observation for unqualified strategies so evidence can accumulate without granting trade authority.
- Make Research support inspection and controlled execution of existing research jobs, including failures and rejections.
- Add another strategy family only after its data and outcome path work end to end.

**Acceptance**

- A scheduled research run creates a trial, results, dossier, and either a qualified forecast or an explicit rejection.
- Every actionable forecast identifies the model artifact that generated it.
- Future-information traps and negative controls fail promotion.
- Exact-cohort probability calibration can progress through genuine observations.
- A rejected or inconclusive model remains a valid research result, but cannot authorize a trade.
- An empty research table has a specific operational explanation.

### Phase 6 — Complete expression comparison and paper follow-through

**Outcome:** The app can explain stock versus option versus cash, then follow a paper decision through its lifecycle.

- Compare expressions from the same thesis, horizon, forecast, and invalidation.
- Prioritize stock, cash, and existing defined-risk option structures. Support covered or cash-secured structures only with verified shares or collateral.
- Show contract terms, quote quality, maximum loss, breakeven, fees, spread cost, expiry, assignment exposure, and exit conditions.
- Keep payoff calculations distinct from calibrated probabilities and expected returns.
- Scope OI/volume requirements to strategies that use them. Missing positioning data must not disable unrelated analysis.
- Repair observation collection so historical capture and calibration can progress through the existing qualification gates.
- Exercise paper staging, unfilled orders, partial fills, exits, expiration, cancellation, and assignment treatment.
- Tie fills and costs to the exact authorizing decision/allocation. Learn execution models only from eligible prior observations.

**Acceptance**

- A valid fixture and a real qualified candidate can complete the paper lifecycle when available.
- Invalid, stale, uncalibrated, or collateral-deficient candidates remain blocked.
- Fees, quantities, and contract multipliers reconcile through partial exits.
- Outcome collection runs without manual repair.
- Real-data qualification is reported separately from fixture-based correctness.

### Phase 7 — Make portfolio management useful every day

**Outcome:** The app explains what to hold, review, trim, hedge, or fund in the context of the whole account.

- Feed the reconciled manual account into the existing portfolio loop.
- Supply required position exposures, constraints, and risk inputs through production calculations.
- Use existing compiled risk limits; expose their current values and provenance. Do not silently invent a risk budget.
- Show current versus proposed cash, weights, concentration, sector exposure, option Greeks, and stress losses.
- Explain each rejected candidate through its binding portfolio constraint.
- Preserve cash and trim funding by source, with database conservation checks.
- Separate risk-reduction reviews from new-alpha qualification. A held-position problem should remain visible when new trades are blocked.
- Add entry thesis, invalidation, next catalyst, review date, and intended management rule to each holding.
- Distinguish deterministic stress scenarios from probabilistic forecasts.

**Acceptance**

- Portfolio totals and proposed funding reconcile to the manual ledger.
- Cash-plus-multiple-trim funding persists and replays without overdraw.
- A good standalone candidate can be rejected for overlap, loss budget, liquidity, or funding.
- Missing data on one candidate does not erase supported analysis for unrelated holdings.
- The app can explain a cash decision and the condition that would change it.

### Phase 8 — Complete the daily assistant workflow

**Outcome:** A short daily review tells you what changed, what needs attention, and what happens next.

- Organize Command Center around portfolio exceptions, current decisions, material evidence changes, and upcoming catalysts.
- Reuse one canonical action identity across Command Center, Opportunities, Portfolio, Research, and ticker detail.
- Show opportunities once per active episode, with research rank, trade eligibility, portfolio fit, and a specific blocker.
- Add acknowledge, snooze, dismiss-with-reason, and review-complete states to existing action storage.
- Prevent refreshes from recreating acknowledged unchanged work. Reopen only for a material new revision or reached condition.
- Give the contextual assistant access to the current immutable packet. It may explain evidence and propose thesis edits; it may not calculate authoritative values or bypass gates.
- Provide a compact daily brief and weekly review using the same decision records.
- Repair existing notification delivery separately. Deduplicate by transition identity and preserve local actions during outages.
- Keep operational details and campaign labels in System. Investment pages show only relevant data limitations.

**Acceptance**

- All workspaces show the same current action for the same decision.
- Acknowledge and snooze survive refresh and restart.
- No expired or unchanged event causes a new urgent notification.
- The assistant cites the packet and states when it lacks evidence.
- A daily review can be completed from the default screen and ticker drill-downs without visiting System.

### Phase 9 — Verify sustained operation and close the plan

**Outcome:** The app works across real sessions without repeated manual repair.

- Run a minimum ten-trading-session observation period after deployment.
- Test premarket, regular session, after-hours, weekends/holidays, source outages, API restart, failed jobs, and recovery.
- Track coverage for the holdings/watchlist denominator, stale decisions, unresolved requests, queue duplication, route failures, and completed outcome collection.
- Preserve existing options qualification and calibration gates; the operational observation period does not replace them.
- Run desktop, mobile, and LAN checks against the canonical build.
- Complete independent review of the final changes and repair all release-blocking findings.
- Record exact release, migration, test, live-route, and observation evidence in the appropriate GBrain project/campaign records.

**Acceptance**

- Every holding has a current position review or an explicit due action.
- Every critical missing field has an accurate reason and resolution path.
- No expired or duplicate action appears in the default queue.
- No unexplained critical-route failures occur during the acceptance window.
- Scheduled producers, publication, and outcome collection recover from tested interruptions.
- Readiness, coverage, research validity, and trading permission remain separate measures.

## 3. Interface and migration changes

Reuse existing owners and generated contracts.

| Interface | Change |
|---|---|
| `/api/status` | Add actual release/schema compatibility and required-capability readiness; retain existing fields. |
| `/api/today` | Return current queue items, grouped counts, durable user state, and complete compact decision context. |
| Ticker detail and decision snapshot | Align primary action, field states, evidence, snapshot prices, compact plan, and repair requests. |
| Portfolio APIs | Extend transactions for cash accounting; add account reconciliation preview/commit and account snapshot reads. |
| Panel scopes | Return the models required by each workspace, with explicit empty/unavailable states and publication identity. |
| Existing job/research actions | Add missing producer wiring and bounded research execution through the existing authorization and job controls. |

All new account records remain in PostgreSQL. Manual and broker source kinds stay explicit. Existing immutable decisions retain their original evidence; new data creates new revisions. Regenerate TypeScript/OpenAPI contracts from backend models.

## 4. Verification and delivery rules

Each phase must prove the complete path it changes:

**producer → PostgreSQL → calculation → publication → API → browser → outcome, where applicable.**

- Use actual provider-shaped fixtures, including missing fields, revisions, malformed values, and timestamp edge cases.
- Test database behavior under the production application role, not only the migration owner.
- Include both valid-path and safe-rejection tests for account, forecast, allocation, and paper-execution changes.
- Test scoped frontend refresh, omitted tables, stale publications, and cross-workspace identity.
- Run focused checks during repair, then required repository checks and affected live routes before phase closure.
- Test migration from the deployed revision and verify recovery before production changes.
- Preserve PostgreSQL-only authority, paper-only execution, source clocks, storage reserves, and the 3,000 ms statement timeout.
- Do not use profitability, signal count, or “all fields populated” as software acceptance criteria.

Deliver phases in order. Start collecting genuine historical and shadow observations as soon as their prerequisites pass; do not wait until the final phase. Later phases may remain evidence-blocked without hiding their exact requirements.

## 5. Explicit defaults and boundaries

- **Primary user:** You, reviewing a personal account.
- **Account:** Manual, reconciled, USD first; broker sync later.
- **Universe:** All holdings, then the current watchlist. Broader discovery follows.
- **Horizon:** Existing tactical and fundamental horizons; no new intraday execution system.
- **Execution:** Paper-only.
- **Data spending:** No purchases by default. Require measured field-level need and provider evidence.
- **Architecture:** Keep the current stack, five workspaces, scheduler, ledger, source registry, research contracts, and portfolio loop.
- **Deferred:** Additional strategy families, broader crypto derivatives, complex latent models, new infrastructure, and live execution.
- **Completion standard:** A deployed, sustained daily workflow with traceable decisions and resolvable gaps—not another set of implemented tables.