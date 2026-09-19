# Trader workstation audit and completion plan

Implementation follow-through: see [the PR #35 engineering and local-verification handoff](trader-workstation-implementation-handoff.md). The findings below describe the original audited baseline, not the status of the updated branch.

Baseline: `053d1d698e7034f70f285937d4c12d319582a26e` (September 19, 2026).
Scope: the nine supplied production screenshots and the corresponding frontend,
read-model, publication, strategy, and paper-execution paths. Screenshots and
personal portfolio amounts/positions are not included in this public repository.

## Verdict

The app has substantial infrastructure, but the screenshots do not demonstrate a
complete trading workstation. They demonstrate a research and operations console
with an incomplete path from source facts to a tradable decision and a measured
outcome. More cards, more collector successes, or a larger rejection log will not
close that gap.

The most important failure is not a missing chart. It is that the user cannot
reliably distinguish a genuine decision to stay in cash from a broken input,
publication, qualification, sizing, or execution dependency.

A complete app must handle unavailable data honestly. It cannot promise that all
possible fields always exist. It should make required supported capabilities
reliable, keep optional limitations local, show useful partial evidence, and
provide a specific recovery path. Replacing missing values with zero, neutral,
fabricated history, or an uncalibrated probability would make this app worse.

## Evidence and limits

**Observed:** the supplied screenshots show an empty Market surface; 992 paper
observations consisting of 423 unfilled and 569 rejected, with no pending/open/
closed observations; no funded paper fills; zero comparable learning cases;
repeated unsupported-parameter evaluations; and a decision funnel with facts
present but no downstream actionable output. Those counts are screenshot-time
observations, not a fresh database query.

**Confirmed in code:** file-specific defects below were read at the baseline
commit. Source refreshes DO call the Market publisher. It would be incorrect to
conclude that there is no scheduled publication path merely because the decision
refresh job excludes Market publication.

**Not verified here:** the running PostgreSQL instance, live provider credentials,
job logs, exact missing-field distribution, production configuration, local NAS
brain context, and deployed behavior after these changes. The exact cause of the
production Market blank state and each rejected/unfilled observation still needs
runtime evidence. This audit does not claim that an existing strategy is profitable.

## Changes in this review branch

1. `frontend/src/pages/MarketRoute.tsx`: consume the existing scope-status contract;
   distinguish loading, failed reads, empty snapshots, partial coverage, and
   retained last-good data; add reload/recovery navigation; avoid stacking empty
   chart cards. Reload is explicitly a publication read, not a collector repair.
2. `frontend/src/components/market/PaperObservations.tsx`: correct the company
   evidence link to `/tickers/:symbol`; expose lifecycle reason and next action
   before expansion; include unmeasurable observations; add reload/retry; avoid
   meaningless entry/exit price placeholders for observations that never traded.
   Missing fill evidence remains explicit when a supposedly filled/closed row
   requires it. Global observation counts are labeled separately from filters.
3. `frontend/src/presentation/paperProgress.ts`: summarize recorded observation
   progress without inferring worker health, statistical edge, or funded P&L.
4. `src/investment_panel/jobs/options_paper_execution.py`: run the funded-book
   processor before optional experiment generation. Research failure still
   reports partial status, and entry switches remain unchanged. **This is only
   an ordering fix:** expensive research still needs to move off the fast tick.
5. `src/investment_panel/infrastructure/postgres/options_paper_quotes.py`: reject
   NaN, infinities, boolean numbers, malformed fractional liquidity, and package
   arithmetic overflow instead of propagating them as usable price/size inputs.
   Valid conservative quote-side pricing and information-time semantics remain.
6. Add focused quote, orchestration, progress, Market availability, and paper-row
   regression tests. No dependencies, schema migrations, entry permissions,
   strategy thresholds, promotion policies, or live brokerage paths are changed.

### Validation actually performed

- 40 quote-owner pytest cases passed against the edited, dependency-free module.
- Four isolated orchestration scenarios passed using the actual edited `run`
  function with dependency doubles: order-before-research, research failure,
  entries disabled, and execution failure. This is not an application import or
  PostgreSQL integration test.
- Ten progress scenarios passed against the strictly compiled TypeScript owner.
- All six changed/new TypeScript/TSX files passed syntax transpilation.
- Python files passed bytecode compilation.

Full React/Vitest rendering, full-project TypeScript checking, architecture and
contract guards, PostgreSQL integration, release gate, and deployed smoke tests
remain required. Local checkout/download access was unavailable; GitHub read/
write access was available. Do not interpret these focused checks as a completed
release gate. Keep the pull request in draft until integrated verification passes.

## Prioritized findings

### P0: Required data and publication readiness are not represented end to end

The Market route reads publication-backed models but previously ignored
`scopeStatus.market`, rendering empty models during loading and failed reads.
The branch fixes that presentation defect, not the unseen database failure.

Relevant owners:
- `frontend/src/pages/MarketRoute.tsx`
- `frontend/src/marketData.tsx`
- `frontend/src/components/market/scopeStatus.tsx`
- `src/investment_panel/application/read_models/loaders.py`
- `src/investment_panel/infrastructure/postgres/panel_models.py`
- `src/investment_panel/workflows/market.py`
- `src/investment_panel/infrastructure/postgres/market_analysis.py`

`load_panel_data` can convert a read exception into empty requested tables. The
Market scope combines basic market evidence with advanced models. Audit whether
an optional model can fail a required baseline read; keep its failure local where
possible rather than discarding unrelated usable evidence.

`update_market_valuations.run` explicitly allows source ingestion to succeed while
the downstream Market publication fails. Therefore green source health is not
proof that Market has a usable publication. Surface both outcomes and their
watermarks. Do not just repeat the collector when publication is the failing step.

### P0: Fast execution and slow research share one tick

The baseline paper job calls `run_experiments` before `repository.process`.
`run_experiments` can resolve candidates and invoke a full Radar computation and
publication. Meanwhile the scheduler classifies this job as a fast database job,
with a default 15-second cadence and a 60-second job timeout.

The branch puts order processing first. Remaining work: move expensive candidate
research to a separate existing-scheduler job, preserve the fixed capacity of two,
and ensure due exits and pending order management cannot be starved by collectors
or research. Within `OptionsPaperExecutionRepository.process`, staging also
precedes management; isolate entry-admission failures from existing-position exits.

`manage_orders` selects the oldest nonterminal orders with a bounded limit. Test
fairness with more orders than the limit; repeated selection of the same old
unfillable rows must not starve newer positions. This is a code-path risk, not an
explanation for the screenshot's currently empty book.

### P0: Economic quote freshness must be checked separately from availability

`latest_option_legs` exposes `observed_at` and sets `quote_time` to `available_at`.
`execution_policy` checks quote age and interleg skew using `quote_time`. Later
availability is necessary for point-in-time safety, but by itself does not prove
that the economic quote is recent or that legs were observed together.

Retain both timestamps. Audit and enforce three separate conditions: information
was available before the decision/fill cutoff; the underlying quote observation
is recent enough for execution; and observations across legs satisfy the skew
policy. Delayed ingestion must not rejuvenate an old quote. Reject future,
inconsistent, missing, or non-finite timestamps/values explicitly. Do not simply
rename `available_at` or bypass the existing cutoff checks.

The numeric guard defect is fixed here. The dual-clock policy is not changed in
this branch and requires domain-level tests before implementation.

### P0: The observation-to-order lifecycle is not a usable product

`PaperWorkbenchRepository.observations` correctly reads research shadows separately
from the funded paper ledger. Preserve that separation. An unfilled or rejected
shadow is not a trade, a realized loss, or a sample of profitable execution.

`seed_experiment_shadows` uses an entry deadline of decision time plus 30 minutes.
Trade tickets also have much shorter quote-validity constraints. Measure time
spent collecting, computing, publishing, staging, and waiting for a later quote.
The snapshot does not establish whether failures arise from score rejection,
expired windows, missing later quotes, authority changes, size/OI/spread gates,
account constraints, or worker failure. Retrieve the exact reason distribution.

For every eligible observation/order expose last transition, due action, last
quote, last worker check, deadline, reason, owner, and retry policy. The current
frontend cannot manufacture these facts from counts; add backend-owned contracts.

### P1: Experiment creation is coupled to automatic promotion

`jobs/options_paper_execution.run_experiments` stops creating new candidates when
`strategy_auto_promotion_enabled` is false. Existing observations still advance.
The two statements must not be conflated.

Research collection and permission to promote a strategy are different controls.
Introduce a separately authorized experiment-collection policy. An operator should
be able to gather bounded, clearly labeled prospective evidence while automatic
promotion remains disabled. Keep candidate lineage, passing historical validation
where required, entry admission, and paper-only boundaries intact.

Audit the entire gate graph for circular prerequisites: evidence collection must
not require the same evidence that only collection can produce. Do not remove the
qualified trading gate; provide a separate observation lane with its own contract.

### P1: Strategy parameter errors are being presented as learning progress

Repeated `Unsupported Parameters` records are not evidence accumulation. Trace
persisted revision parameters through `strategy_parameters`, the strategy catalog,
implementation binding, evaluator, run, and publication. Validate supported keys,
types, ranges, units, and implementation version before starting a run. A failed
configuration must identify the exact field and responsible revision, not produce
another generic collecting-evidence card on every tick.

Count unique independent resolved outcomes, not retry rows, contract variants,
repeated forecasts, or overlapping marks. A minimum sample count is an eligibility
floor, not proof of edge. Keep strategy performance and forecast calibration
separate and join them through explicit revision and episode identities.

### P1: Advanced Market observations can select old, unbalanced evidence

The inspected `market_analysis.load_market_inputs` phase-2 query orders by
`dimension, observed_at, observation_id` ascending before applying a global limit
of 500. With enough retained rows, that selects old rows and can exclude later
sort-order dimensions entirely.

Replace this with a requirement-driven bounded input selection: latest admissible
observations and the necessary trailing window per dimension/source/horizon,
selected at the requested cutoff. Preserve deterministic ordering and PIT source
lifecycle rules. Test a corpus larger than 500 rows and uneven dimension density.
This selection risk is confirmed from the query; it is not proven to cause the
entire empty Market screenshot.

### P1: The frontend can invent a missing-history diagnosis

`Phase2Evidence` in `frontend/src/views/market/panels.tsx` defaults absent posterior
rows to `MISSING_HISTORY`. Absence alone does not distinguish loading, failed read,
unrequested model, unavailable source, or insufficient history. The route-level
fix avoids this misleading screen when the whole scope is empty. Partial scopes
still need backend-owned advanced-model availability and a corrected component.

### P1: Healthy sources and useful decisions use different denominators

The screenshots combine healthy-source counts, full coverage for one historical
subset, low recovery-slot coverage, and zero actionable decisions. These measures
are not interchangeable. Every metric needs its capability, universe, horizon,
expected observations, observation cutoff, and denominator. Measure the conversion
from valid facts through decisions to outcomes; do not collapse all of it into a
green source badge or a single completeness percentage.

## User-visible redesign, screen by screen

### Today

The first screen should answer: what requires attention, what changed materially,
what is tradable, and what is broken. Use a compact market/session strip, a small
prioritized action list, and one progress/blocker summary. Put research reading
below those, behind a bounded list.

Deduplicate alerts by position/episode and decision revision. Do not repeat the
same concentration concern in action queue, risk, and pulse. A blocked decision
needs the actual missing input, impact, owner, and permitted recovery action.
Separate a genuine `no qualifying trade` from `decision unavailable`.

A directional daily brief must cite its own cutoff and coverage. When it disagrees
with unavailable Market evidence, explain the distinct evidence basis or withhold
that directional summary; do not silently make one look authoritative for the other.

### Market

Show useful observed baseline evidence before advanced models: broad-index trend,
well-defined breadth, volatility/risk appetite, rates/credit where available, and
valuation with units and historical comparison. Use named proxy instruments and
a disclosed universe; never label watchlist-only breadth as the entire market.

Each dimension needs source/as-of/coverage and a plain-language consequence. Keep
advanced posterior, scenario, and calibration detail in a research/evidence drawer.
A missing optional model must not blank supported index-price information. Never
pretend missing required data is neutral. Baseline publication must not require
a reconciled personal brokerage account just to display public-market context.

### Opportunities

Replace the default wall of thesis prose with a sortable comparison table. Distinct
views: qualified for paper, awaiting a defined trigger, research-only, blocked,
and archived/rejected. Explain why an empty qualified view is empty.

For a qualified setup show symbol/instrument, strategy revision, horizon, catalyst,
entry condition/limit, invalidation, payoff/risk, net expected value with evidence
status, liquidity, paper-capital requirement, portfolio effect, and freshness.
Display probability only when its definition and calibration are defensible. A
research idea can remain useful without being given a spurious trade score.

The detail view should connect thesis -> source facts -> alternatives/countercase
-> forecast -> risk policy -> immutable ticket -> subsequent order/outcome. Avoid
recomputing decision authority in React.

### Real portfolio

Preserve the transaction ledger and reversal semantics. Explain separately what
can be valued, what performance can be computed, and what account reconciliation
is missing for sizing. Never use unknown cash as zero or pretend position value
is complete account equity.

Reconcile the portfolio curve, invested-capital return, session P&L, realized/
unrealized P&L, external flows, and high-water-mark drawdown against one declared
basis. A drawdown in dollar P&L is not automatically an account-NAV drawdown. The
screenshots motivate these tests; they do not alone prove an accounting bug.

Rank shared risk by portfolio contribution/concentration rather than alphabetic
pairs. Provide a labeled correlation heatmap and position/sector/factor exposure
where inputs support it. Show sample window and missing exposures. Avoid generic
sentences repeated for each pair.

### Paper trading

Lead with worker/market-session status, book state, next scheduled action, blocked
stage, and last successful lifecycle transition. Keep funded-account performance
separate from observation experiments. Make the execution funnel and rejection
reason distribution visible without scrolling through hundreds of rows.

Show an equity curve as soon as verified cash flows/marks support it; do not require
a closed trade to display a legitimately marked open position. Conversely, do not
turn an initialized cash account into evidence of trading success. The blotter
must show staged/open/closed/expired/canceled states and link to frozen decisions.

### Research

Show the active strategy, named experiment, hypothesis, changed parameter, eligible
population, evaluation method, independent evidence accumulated, blocking error,
and next actual collection task. Distinguish stalled, misconfigured, collecting,
awaiting outcomes, and rejected. A generic `keep collecting` is not remediation.

Charts: matched baseline/challenger after-cost outcomes, forecast calibration,
coverage over time, and revision history. Label hypothetical, shadow, and funded
paper results distinctly. Group repeated operational events; preserve raw runs
in a paginated drilldown rather than the main learning screen.

### Agent controls and contextual drawer

Keep operational configuration, prompts, run logs, costs, and provider failures in
the controls workspace. Start with current task, health, budget, last meaningful
result, and controls. Load raw prompts and history on demand.

The drawer should explain the current decision with citations to its immutable
packet, expose missing evidence, and support a bounded research question. It
should not consist only of a policy description and a link elsewhere. Keep the
agent advisory-only; quoted source material must not acquire action permissions.

### System health

Lead with the blockers preventing required user workflows. A useful row answers:
which capability is affected, how many decisions it blocks, last valid data,
last successful publication, failed stage, retry outcome, and owner. Separate
source health, collector execution, normalized fact coverage, model readiness,
publication readiness, and consumer/read-model health.

Do not call the full system healthy merely because all collectors returned. Keep
raw logs and build identifiers available, but prioritize the shortest actionable
path from a broken screen to its failing dependency.

## Implementation plan: four substantial phases

### Phase 1 — Restore required data and a trustworthy readiness contract

1. Capture live diagnostics before changing data: deployed frontend/API/schema
   identities, effective redacted configuration, last Market publications, source
   watermarks, API response statuses, failed jobs, and rejection distributions.
2. Trace each missing required field from provider through normalized facts,
   available-at selectors, model, publication content, API contract, and React.
   Assign one authoritative owner; repair the actual failed stage.
3. Introduce/extend a typed capability/readiness contract carrying status,
   reason, required-for, scope, timestamps, denominator, owner, and retry action.
   Reuse existing owners; do not add parallel state stores or facade layers.
4. Isolate optional market evidence failures; fix the bounded historical selector;
   correct absent-posterior diagnosis; make source-success/publication-failure
   observable. Republish safely after verified repairs.
5. Check supported provider capabilities at startup and before strategy admission.
   An adapter without needed quote size/history/consensus must be explicitly
   unsupported for that requirement rather than repeatedly rediscovered as null.

**Acceptance:** required supported Market baseline renders from a fresh live
publication; optional gaps are labeled locally; initial load, empty success,
partial response, timeout, stale retention, and retry all have tested states;
healthy ingestion plus failed publication is not reported as end-to-end healthy;
every remaining required gap has a named cause and recovery action. No fabricated
history, broadened PIT cutoff, or fallback to a second data authority.

### Phase 2 — Close the prospective paper lifecycle without weakening gates

1. Separate bounded order management, shadow observation, and expensive candidate
   generation into correctly budgeted existing-scheduler work. Preserve capacity
   two. Prioritize exits; use fair batching, idempotency, and explicit time budgets.
2. Separate experiment collection permission from automatic promotion. Define
   three clear modes: observation-only research, qualified funded paper, and live
   execution disabled. Give paper sizing the explicit paper-account authority;
   audit that unreconciled real-account constraints are not accidentally reused.
3. Make quote collection requirement-driven for staged/open contracts and all
   legs. Persist economic and information timestamps. Coordinate collection,
   publication, and deadline budgets instead of relaxing stale-quote thresholds.
4. Implement/test the complete state machine: decision -> immutable ticket ->
   staged/reserved -> later eligible quote -> conservative fill -> marks -> exit
   -> reconciliation -> outcome. Add expired/canceled/unmeasurable paths with
   durable reasons, no fake trades, and no hindsight fills.
5. Expose per-stage counts, reasons, last transition, worker heartbeat, last quote,
   next due action, and account reservations. Keep observational outcome counts
   and funded-account performance separate.

**Acceptance:** deterministic fixtures produce a full entry/mark/exit/journal/
learning path under eligible quotes; unavailable/stale/crossed/zero-size quotes do
not fill; disabling entries does not stop exits; restart/retry does not duplicate
fills or reservations; more-than-batch-size populations are not starved; long
research cannot delay the next required execution cycle. Session closure, early
close, DST, expirations, partial fills, and incomplete marks have explicit tests.

On live paper data, an eligible marketable order with an admissible subsequent
quote must progress within the declared service budget. A valid limit that never
becomes marketable may remain unfilled. No eligible setup is an acceptable outcome
only when required data is healthy and the rejection explanation is concrete.
Do not mandate a trade count or a positive P&L to pass an operational release gate.

### Phase 3 — Deliver the trader-facing workflow, not more log surfaces

Implement the screen changes above using shared typed projections and progressive
disclosure. Add comparison tables, portfolio/performance visualizations, a
canonical decision detail, and a usable contextual drawer. Retain separate real
and paper navigation. Consolidate repeated cards and generic recovery text.

The canonical evidence chain should join `decision_id`, opportunity episode,
strategy revision, forecast claim, publication, ticket, paper order, quote/fill,
and resolved outcome. Show frozen historical evidence separately from current
company evidence so a later thesis cannot rewrite why an earlier trade happened.

**Acceptance:** from the first screen a user can identify what needs attention,
what is actionable, why a trade is blocked, the next actual operation, and whether
learning is progressing. Every visible action has a tested destination/result.
All nine screenshot surfaces are inspected against the real running app at a
desktop and narrow viewport. Empty-state fixtures alone do not satisfy this gate.
There are no unsupported probability labels, unlabeled zero substitutions, or
inconsistent account/performance denominators.

### Phase 4 — Validate edge and make learning measurable

1. Select a small supported strategy universe with sufficient data and explicit
   hypotheses. Keep long-horizon and short-horizon requirements distinct. Define
   entry, exit, sizing, failure conditions, and after-cost evaluation in advance.
2. Resolve parameter/version mismatches before launching experiments. Record data
   manifest, feature/model versions, evidence cutoff, changed parameters, and
   independent episode identity for reproducibility.
3. Evaluate historical/walk-forward evidence without leakage, then gather
   prospective observations and qualified paper results. Account for overlapping
   episodes, selection bias, multiple trials, costs, slippage, tail loss, and
   regime changes. Do not treat thousands of marks as thousands of independent
   trades or a high win rate as positive expectancy.
4. Measure forecast probability calibration separately from execution P&L. Compare
   baseline/challenger on matched cases and realistic after-cost returns. Use
   predeclared uncertainty-aware gates; keep inconclusive results inconclusive.
5. Display promotion/rollback readiness and its evidence. Preserve independent
   permission for promotion and deterministic risk controls. A passing process
   or a fluent thesis does not establish an investment edge.

**Acceptance:** at least one named strategy has a reproducible evidence dossier,
prospective collection that progresses, and a clearly reported pass/fail/
inconclusive result. No promise of profitability is part of acceptance. Promote
only under the existing validated governance policy; record why every rejected
or delayed challenger remains so. Live brokerage stays disabled.

## Runtime diagnostic starting points (read-only)

Run against the configured authority using the project's existing secure local
access. Do not paste credentials or full private account data into issues.

```sql
-- Are Market publications present, and do they contain the expected models?
SELECT p.id, p.scope, p.status, p.published_at,
       r.status AS analysis_status, r.input_cutoff,
       i.model_name, count(i.publication_id) AS rows
FROM app.publication p
LEFT JOIN analysis.run r ON r.id = p.analysis_run_id
LEFT JOIN app.publication_content_item i ON i.publication_id = p.id
WHERE p.scope = 'market'
GROUP BY p.id, p.scope, p.status, p.published_at,
         r.status, r.input_cutoff, i.model_name
ORDER BY p.published_at DESC, i.model_name
LIMIT 100;

-- Separate rejection from missed entry windows and authority changes.
SELECT status, pending_entry_reason, count(*) AS observations
FROM analysis.shadow_trade
WHERE source_kind = 'options_paper_experiment'
GROUP BY status, pending_entry_reason
ORDER BY observations DESC;

-- Do funded orders exist at all, and in which execution lanes/states?
SELECT lane, status, count(*) AS orders
FROM app.paper_order
GROUP BY lane, status
ORDER BY lane, status;
```

These queries do not explain everything. Join representative blocked decisions to
their immutable tickets and source facts, inspect job error details, and compare
observed/available/published/staged times. Report each conclusion with a cutoff
and scope; do not infer live causes from the screenshots alone.

## Release gate

Run the focused new tests first, then the repository's prescribed `make guards`,
`make check`, and `make release-gate`. Verify PostgreSQL 18 integration, generated
contracts, production frontend build, and served build identities on the running
app. Verify paper-only controls and absence of live submissions. Capture new
screenshots privately for all affected surfaces and record remaining gaps.

Do not merge this patch as a declaration that the app is complete. The branch
removes concrete defects and makes failures more legible; the four phases above
are the remaining work required to establish a reliable trading and learning loop.
