# Market: Post-Foundation Audit and Next Improvement Plan

Audit date: 2026-09-01  
Repository: joe7hu/market  
Audited head: 23c470e1959f1d17b78fd6715f2c4f6920fd21c5

This file preserves the ordered implementation phases and their acceptance
criteria from the user-provided plan. The five phases are immutable campaign
authority. The broader audit, target architecture, non-goals, and evidence map
remain in the originating user plan.

## Global invariants

- Preserve PostgreSQL as the only runtime and development-test authority.
- Keep exactly five top-level workspaces: Command Center, Opportunities,
  Portfolio, Research, and System.
- Keep all decisions fail-closed. Missing or mismatched evidence is not a
  trade authorization.
- Do not let an LLM calculate authoritative features, choose validation folds,
  omit failed trials, promote a strategy, change compiled risk limits, or
  authorize a trade.
- Do not add more top-level tabs, rewrite PostgreSQL authority, promote
  Martingale, or activate intraday systems before event-time data and fill
  calibration exist.
- Do not use latent-state models, simulations, or agents as shortcuts around
  point-in-time, out-of-sample, cost, capacity, and promotion gates.

## Phase 0 — Release integrity and one truthful decision surface

### Objective

Make the current foundation operationally reliable before adding research
complexity.

### Deliverables

1. Fix the two failing market-ci tests.
2. Make CI PostgreSQL configuration deterministic and TCP-explicit.
3. Require green market-ci on main and before any strategy promotion.
4. Repair compact funnel opportunity-episode validation/materialization.
5. Add deterministic replay fixtures for Command Center.
6. Create DataFieldStateV1 and the explicit availability enum.
7. Replace plain “Unavailable” on decision-critical surfaces.
8. Add MarketStateDeltaV1 or remove the unsupported panel.
9. Consolidate Today into one canonical action queue.
10. Ensure the compact trade plan is visible without an extra load.
11. Replace exact decision-critical RowRecord/alias probing with generated DTOs.
12. Fix expression mapping to exact backend enums.
13. Relabel external live charts as non-authoritative.
14. Relabel portfolio risk/scenario/replacement panels where the current model is only heuristic.
15. Add a critical-data coverage badge rather than a generic “Data loaded” badge.

### Acceptance criteria

- main CI is green;
- all frontend checks, migrations, slow PostgreSQL tests, and coverage gates pass;
- two calls to a cacheable scope perform one authoritative load/build;
- a valid opportunity episode survives compact funnel validation;
- every canonical action is derived from one immutable action object;
- no duplicate independent ranking on Command Center;
- no decision-critical bare “Unavailable” remains;
- every missing field has source, reason, blocking status, and next action;
- frontend fails tests when a required backend field is renamed;
- exact expression enum mapping is covered by golden payloads;
- the canonical trade plan is always visible;
- current/live external data is visually distinguished from snapshot data.

## Phase 1 — Scientific research authority and model-owned forecasts

### Objective

Turn the existing governance skeleton into a reproducible hypothesis-driven
research operating system.

### Deliverables

1. Add hypothesis, experiment family, research trial, trial result, validation dossier, strategy forecast, and full-denominator universe observation tables.
2. Extend current strategy revisions/evaluations rather than replacing them.
3. Require mechanism classification and falsification.
4. Record every trial, including failed trials.
5. Build point-in-time universe tapes.
6. Persist ranked-out candidates and later outcomes.
7. Separate discovery, forecast, expression, allocation, and execution records.
8. Move stock alpha forecast generation into the qualified model artifact.
9. Remove legacy forecast ranges from trade authority.
10. Add the five validation gates.
11. Add negative-control and future-information trap suites.
12. Add purged/embargoed and combinatorial validation paths.
13. Add DSR, PSR, PBO, data-snooping, and false-discovery metrics.
14. Add parameter stability and neutralization outputs.
15. Add 1x/2x/3x cost and capacity stress.
16. Add immutable promotion dossier and compiled promotion policy.
17. Add a Research workspace for hypotheses, trials, and validation.

### Acceptance criteria

- every actionable forecast references exactly one model-owned StrategyForecast;
- the artifact that is promoted is the artifact that generated the distribution;
- no forecast is derived from the legacy decision view;
- all eligible candidates at each cutoff are persisted;
- trial count is complete and immutable;
- randomized labels and white-noise markets produce no persistent positive edge;
- intentionally leaked data fails the family;
- neutralized results are available for each promoted strategy;
- parameter-neighborhood stability is visible;
- DSR/PBO/data-snooping results are part of promotion;
- 3x-cost result is explicit;
- strategy promotion is impossible without all mandatory dossier sections;
- Research UI exposes failed trials and rejected revisions.

## Phase 2 — Data contracts and probabilistic market state

### Objective

Fill the dimensions that currently make the “every dimension of the market”
claim incomplete and build a bounded latent-state foundation.

### Deliverables

1. Add field-level data contract and status registries.
2. Build point-in-time macro/rates/credit facts and real-time vintages.
3. Add actual/consensus/surprise/revision event records.
4. Add quarterly corporate expectations and revision history.
5. Add historical full-chain option data with OI/volume/surface.
6. Add venue-level crypto funding/basis/OI/liquidation/depth.
7. Add positioning and flow datasets.
8. Add historical intraday/microstructure data only for strategies that require it.
9. Add data conflict, fallback, and source-confidence policies.
10. Publish a market coverage vector, not a single optimistic status.
11. Add MarketStatePosterior using HMM/state-space/change-point baselines.
12. Add uncertainty, entropy, persistence, missingness, and transition probabilities.
13. Add observable-baseline versus latent-state challenger tests.
14. Add scenario paths for portfolio stress.
15. Keep latent state advisory until incremental utility passes Phase 1 gates.

### Acceptance criteria

- every required field has a canonical definition and PIT availability semantics;
- no unsupported dimension is presented as available;
- trade-critical coverage is calculated per expression and strategy;
- macro events include actual, consensus, surprise, revision, and availability;
- option strategies needing positioning remain blocked until OI/volume pass SLA;
- crypto strategies use venue-level executable data, not aggregate volume alone;
- market-state posterior includes uncertainty and missingness;
- the latent model is compared against a simple observable baseline;
- it cannot affect live rank without positive incremental OOS net utility;
- data removal/fallback tests show how state confidence degrades;
- scenario paths are reproducible from immutable artifacts.

## Phase 3 — Mechanism-first strategy factory

### Objective

Build a diversified set of strategies whose sources of alpha are distinct,
explainable, and scientifically validated.

### Deliverables

1. Implement the common strategy interface and registry.
2. Encode classic rules as source-versioned baseline templates.
3. Permanently classify Martingale as a negative control.
4. Build medium-horizon trend/underreaction family.
5. Build gap continuation-versus-reversal family.
6. Build event information-propagation family.
7. Extend options recovery with full-chain state and controls.
8. Build crypto funding/basis family.
9. Build structural flow families as supporting data matures.
10. Add strategy-level cost, capacity, failure-regime, and source manifests.
11. Add champion/challenger comparisons.
12. Add strategy P&L tapes for correlation and crowding.
13. Add decay and regime-conditional monitoring.
14. Put all families through the same promotion ladder.
15. Keep intraday systems blocked until event-time data and fill models are proven.

### Acceptance criteria

- at least one validated research family exists in each of the four alpha mechanism classes, even if some remain shadow-only;
- every strategy has a one-sentence economic explanation and falsification rule;
- every strategy references a complete data and cost manifest;
- classic systems are benchmarks, not special-cased promotion candidates;
- no informal name is used without a versioned source definition;
- every promoted strategy has full-denominator outcomes;
- every active strategy has correlation, tail correlation, crowding, and capacity estimates;
- strategies that are merely factor replicas are rejected or labeled as exposure sleeves;
- the system can explain why two apparently similar trend systems are or are not distinct;
- intraday strategies cannot be actionable on daily-only data;
- crypto strategies include venue and liquidation failure scenarios.

## Phase 4 — Alpha portfolio, execution, and closed-loop product

### Objective

Turn validated forecasts into a coherent, risk-budgeted portfolio rather than a
collection of attractive individual trades.

### Deliverables

1. Add strategy exposure, alpha correlation, crowding, and allocation snapshots.
2. Build shrinkage covariance and tail-dependence models.
3. Add factor, sector, asset-class, Greek, liquidity, and venue constraints.
4. Add volatility targeting and marginal risk contribution.
5. Add drawdown control and fractional Kelly caps.
6. Add capacity and expected unwind cost.
7. Add a joint alpha portfolio optimizer with cash hurdle.
8. Add explicit funding/trim recommendations.
9. Build probabilistic scenario/stress engine.
10. Add execution models for spread, fill probability, latency, and impact.
11. Extend paper/canary telemetry to all strategy families.
12. Integrate canonical allocator output into all five workspaces.
13. Add drift, crowding, capacity, and calibration rollback.
14. Add selected-versus-ranked-out postmortems.
15. Add book-level attribution from hypothesis to realized P&L.

### Acceptance criteria

- the same forecast can be rejected because of portfolio overlap, capacity, or execution;
- every funded action has a positive marginal book utility;
- cash can rank above all trades;
- target weight is traceable through uncertainty, volatility, risk budget, Kelly cap, and portfolio constraints;
- portfolio UI shows current versus proposed risk contributions;
- tail correlation and simultaneous unwind risk are visible;
- scenario results are generated by a real scenario artifact, not risk-card labels;
- proposed actions state what funds them;
- realized fills update the execution model;
- strategy decay can reduce allocation before a binary rollback;
- all five tabs consume the same immutable forecast/allocation/action objects;
- the system can answer in one view: why this trade, why now, why this expression, why this size, what invalidates it, what data is missing, and what funds it.

