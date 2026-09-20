# Trader workstation implementation and local verification

This supersedes the partial scheduling handoff. PR #35 now implements changes
across all four engineering phases of `trader-surface-audit-20260919.md` and all
nine reviewed surfaces. This is an implementation handoff, not a declaration that
production data is complete or that a strategy has demonstrated profitable edge.
The original audit remains the acceptance specification.

## Phase 1: data authority and useful readiness

- Fix the advanced-observation decoder that stripped required `source_id` and
  `source_version` fields along with SQL metadata and silently discarded valid
  observations. Decode against the correct PIT/Event model; preserve event fields.
- Select trailing observations per source/field/dimension/asset class instead of
  taking the first 500 oldest observations across the entire database.
- Isolate optional valuation/event/corporate/crypto/advanced reads with transaction
  savepoints. A failed optional model no longer discards supported baseline data.
  Required prices and instrument reads still fail explicitly rather than inventing
  neutral inputs.
- Publish the baseline before optional advanced-model publication. Propagate source
  success versus downstream failure as distinct statuses. Add an independently
  scheduled, bounded `refresh_market_publication` job so stored facts can be
  republished without repeating expensive collection.
- Read baseline Market models from one current publication in a repeatable snapshot.
  A successful empty current model cannot resurrect an older populated model.
  Apply per-model row limits in SQL and preserve full-population counts.
- Add typed `/api/workstation/status`, separating workers, source/publication state,
  funded orders, observation counts, parameter failures, and recovery actions.
  Failed reads are not represented as zero populations.

## Phase 2: prospective execution and accounting

- Keep candidate research separate from the fast paper manager, with independent
  collection permission and a reserved scheduler slot. All existing risk,
  qualification, entry, and promotion policies remain authoritative.
- Manage existing orders before staging; preserve completed work when a later
  stage fails. A management error or exhausted management budget blocks new staging.
- Use database-backed fair claims and actual-check timestamps for bounded order
  batches. Claims do not rewrite the accounting update clock. Failures in one
  order are reported without discarding other completed transitions. Fast owners
  have short SQL/lock budgets; shadow processing has a separate bounded budget.
- Refresh active pending/open contracts and their complete leg sets in a separate
  read-only, regular-session job. Reuse the provider lease and canonical quote
  persistence; prioritize missing/old quotes and do not scan the whole universe.
- Validate economic observation age/skew separately from information availability.
  Delayed ingestion cannot rejuvenate an old quote; explicit missing or inconsistent
  clocks fail validation. Preserve finite numeric and integral size checks.
- Explain later-quote, limit, policy, deadline, and authority reasons in observation
  lifecycle records. Unfilled/rejected research observations remain outside funded
  paper-account P&L.
- Persist prospective paper NAV every five-minute bucket, append-only under the
  application role. Missing evidence creates an explicit null gap, never a zero or
  invented mark. The account curve begins when observations exist; it is not
  fabricated for an earlier period. Hourly display sampling preserves gap flags.
- The existing initialized paper-account authority remains the sizing source;
  incomplete funded paper evidence must not fall back to a real brokerage account.

## Phase 3: user-facing workflow

| Surface | Implemented behavior |
|---|---|
| Today | Workflow readiness above a bounded, deduplicated action queue; collapsed research reading and fewer repeated portfolio warnings |
| Market | Usable baseline and available charts first, individual model status, advanced evidence on demand, explicit read/empty/stale distinctions, rebuild action and publication-change reload |
| Opportunities | Comparison table with state filters, canonical entry/risk/size/expectancy, dated decisions, expandable thesis/countercase, and explicit loaded/universe coverage |
| Real portfolio | Position value versus account reconciliation explained; shared-risk pairs ranked by combined exposure (not falsely claimed variance contribution); correlation heatmap with unknown cells and sample window |
| Paper trading | Workflow stages, funded/observation counts and reasons, pending-order links, prospective funded NAV chart, explicit gaps, refresh/polling, and no research P&L mixed into the account |
| Trade detail | Reload, staged-versus-filled wording, probability semantics, finite observed price points without fabricated interpolation, and immutable decision/fill evidence |
| Research | Actual independent counts and evaluation blockers, no run-count proxy for outcome evidence, finite calibration points, matched prompt lineage, grouped events, and actionable stalled/misconfigured states |
| Agent controls | Bounded assessment and run lists, prompts on demand, explicit missing cost/token values, operational controls retained |
| Context drawer | Three evidence-bound questions about the decision, next required work, and falsification; packet identity binds displayed evidence and citations; advisory-only |
| System health | Required-workflow failures first, raw metrics/details on demand, separate collector/publication/consumer readiness |

## Phase 4: reproducible learning, not invented confidence

- Validate candidate parameters before expensive evaluation or creating a candidate:
  canonical aliases, finite numbers, integral counts, supported units/ranges,
  parent/implementation lineage, and existing relaxed-filter counterfactual gates.
- Give the mutation provider the same bounded parameter schema and explicit units.
  Configuration failures include the exact field rather than becoming more
  collecting-evidence records.
- Deduplicate identical latest evaluations; expose configuration errors and actual
  named evidence from the current revision. A minimum count is labeled a count
  floor, not a statistical pass or proof of edge.
- Keep forecast calibration, research observations, and funded execution results
  distinct. Missing calibration data is not rendered as zero. Only an explicitly
  active prompt and its related challenger are compared.
- Preserve the existing historical/walk-forward, forward observation, after-cost,
  uncertainty, promotion and rollback mechanisms. No new strategy is asserted to
  have positive expectancy, and no population is synthesized to make a gate pass.

## Apply and restart

Use the repository checkout, not a review ZIP. Build and migrate before restarting
both API and scheduler. Schema head is `20260920_0027`.

```sh
uv sync --extra test --locked
npm ci --prefix frontend
uv run market-db-migrate
npm --prefix frontend run build
make check
make release-gate
```

The migration adds `app.paper_nav_observation` and a paper-management due index;
application permissions allow NAV SELECT/INSERT but not UPDATE/DELETE. No history
backfill is performed. Do not downgrade after collecting observations unless
losing that new NAV history is intentional.

New job cadence controls (seconds):

- `MARKET_MARKET_PUBLICATION_REFRESH_SECONDS` (default 3600)
- `MARKET_PAPER_QUOTE_REFRESH_SECONDS` (default 60)
- `MARKET_PAPER_EXPERIMENT_REFRESH_SECONDS` (default 300)

Check the exact environment names in `core/job_policy.py` before configuring a
local override. Existing paper-management cadence and entry switches are unchanged.
Collection configuration is separate from promotion. Restart the scheduler after
changing these settings. No live brokerage path is introduced.

## Focused verification

```sh
uv run --extra test python -m pytest \
  tests/test_market_observation_lineage.py \
  tests/test_market_scope_isolation.py \
  tests/test_quote_clock_evidence.py \
  tests/test_strategy_parameter_preflight.py \
  tests/test_workstation_readiness.py \
  tests/test_targeted_paper_quotes.py \
  tests/test_paper_management_fairness.py \
  tests/test_paper_tick_ordering.py \
  tests/test_paper_execution_scheduling.py \
  tests/postgres/test_workstation_completion.py -q
```

Also run the existing options-paper, market-publication, strategy-learning,
application-role, and API suites. The new PostgreSQL cases cover coherent empty
publications, actual status queries, append-only cash-only/gapped NAV, fair
management batches, and recent balanced observation selection beyond 500 rows.

## Real-data acceptance checklist

1. Confirm frontend/API/schema identities match this checkout. Inspect the typed
   status endpoint: unavailable reads must have a reason, not misleading zeros.
2. Rebuild Market from existing facts. Check current publication ID and source
   cutoffs; independently fail an optional feed and verify price/trend evidence
   remains visible. Check valid advanced observations retain source lineage.
3. During a regular session, verify the active-contract collector captures every
   selected leg and respects the shared provider lease. Compare observed,
   available, published, staged and filled times on one representative decision.
4. Verify one genuinely eligible immutable ticket can stage, receive a later
   admissible quote, fill conservatively, mark, exit and reconcile. A valid limit
   that never becomes marketable must remain unfilled. Do not bypass qualification
   to obtain a demonstration trade.
5. Disable entry switches: existing positions and shadows must keep advancing.
   Test a slow research run, a failed order and more orders than one batch; exits
   must not be stranded. Review deferred/error reasons and account reservations.
6. Verify paper NAV against the canonical journal/account, including initial cash,
   fees, marks, exits and an intentionally missing mark. Missing evidence must
   create a gap. Observations must never enter the funded account curve.
7. Inspect an unsupported candidate: see the exact field/revision, no fabricated
   progress and no repeated identical evaluations. For a supported candidate,
   verify hypothesis, manifest/revision, matched independent outcomes and costs.
8. Inspect every reviewed screen at desktop and narrow widths. Test links, reload,
   failed API, successful empty API, partial coverage, stale retention and retry.
   In the drawer, confirm evidence and citations remain scoped to the selected
   decision when switching tickers quickly.
9. Evaluate actual edge only after enough valid independent data accumulates.
   Passing operational checks is not evidence of profitability. Report a strategy
   as pass/fail/inconclusive from its real evidence, not from a minimum count or
   successful job status.

Production provider availability, data entitlements, authentication, historical
backfill, real-data accounting, and statistical edge are local verification tasks.
The implementation does not make unavailable paid-provider fields magically exist.
Record any remaining unsupported capability explicitly rather than substituting
zeros, neutral signals, synthetic fills or an uncalibrated win probability.

## Continuation review: corrections to the integrated implementation

The final review is against the integrated four-phase implementation, not the
old scheduling-only PR description. It found and corrected these additional issues:

- **Budget-limited batching:** claim time alone could repeatedly select the same
  processed prefix while leaving the rest of a full batch unchecked. Ordering now
  considers actual check time first. Claims still do not masquerade as checks or
  alter financial `updated_at`. A PostgreSQL regression forces one check per tick
  across a five-order batch and requires all five orders to be reached.
- **Quote-collector fairness:** recent attempted symbols are read from the existing
  job journal. Failed/unsupported symbols no longer permanently occupy the first
  eight slots. A failed collection records its attempted universe, not a successful
  capture, and releases the provider lease. Malformed legacy shadow-leg JSON is
  isolated rather than breaking the entire active-contract query.
- **Execution evidence:** explicit quote clocks require a timezone. Fractional
  contract sizes and non-finite package arithmetic fail validation. A late data
  arrival remains distinct from when the quote was observed.
- **Readiness:** populated placeholder rows do not establish an available Market
  baseline. Price/trend/breadth/risk availability is inspected, input cutoffs are
  compared with the expected completed session, and optional valuation coverage
  is reported separately. The UI displays source and downstream worker outcomes.
- **Missing-value semantics:** blank/formatting-only strings do not become zero;
  legitimate zero valuation values remain zero; absent valuation posture is not
  labeled neutral. Market shows actual valuation context and independently usable
  named index ETF prices rather than an invented valuation score.
- **Performance displays:** a completely missing hourly NAV bucket breaks the
  curve even if adjacent timestamps happen to be only 65 minutes apart. Paired
  performance charts show losses to the left of zero and gains to the right,
  without duplicate P&L aliases. Failed research loads no longer display endless
  initial-loading skeletons, and superseded paging requests cannot append rows.

## Read-only local smoke report

After installing, migrating, building and restarting, run:

```sh
uv run python scripts/verify_workstation.py \
  --base-url http://127.0.0.1:8010 \
  --expected-commit "$(git rev-parse HEAD)" \
  --output /tmp/market-workstation-verification.json
```

The script uses **GET only** against runtime status, workflow status, the Market
snapshot, paper performance, seven days of prospective NAV and the learning
overview. It checks response shape, coherent publication identities, finite
accounting values, basic account arithmetic, ordered NAV timestamps and explicit
missing evidence. Its report contains allowlisted operational counts/states, not
raw balances, holdings, prompts, SQL or exception bodies. Keep reports local;
inspect them before sharing. It does not bypass local authentication.

Exit code `1` means a transport/contract failure. Without `--strict`, operational
warnings produce a `needs_attention` report but exit `0`; add `--strict` after
reviewing expected disabled/collecting states to make those warnings fail too.
Zero trades, a legitimate unfilled limit, a cash-only account or an inconclusive
strategy do **not** cause the script to manufacture a trade or demand profit.
The report is not a replacement for an actual fill-to-outcome check or browser QA.

## Acceptance traceability for the local session

| Requirement | Implementation / automated evidence | Local observation to record |
|---|---|---|
| Useful Market despite optional failures | `market_analysis.py`, `panel_publications.py`, `test_market_scope_isolation.py`, PostgreSQL completion tests | Current publication and cutoff; supported index data survives an optional-feed failure |
| Source success is not consumer readiness | `workstation.py`, `WorkflowReadiness.tsx`, `test_workstation_readiness.py` | Collector, publication and model readiness have distinct states; rebuild old facts does not appear fresh |
| Supported quotes and active instruments | `jobs/paper_quotes.py`, `postgres/options.py`, targeted quote tests | Entitlement, all required legs, economic/availability clocks and recent failed-attempt rotation |
| Safe progressing paper lifecycle | Existing paper execution suites, new fairness and quote-clock regressions | Exact immutable decision/ticket, later quote, entry, fees, mark, exit, journal and outcome IDs |
| Account authority and performance | `paper_workbench.py`, append-only NAV migration, PostgreSQL completion tests | Whole-book cash/reservations/NAV reconcile; filtered trade P&L and research observations stay distinct |
| First-screen actionable workflow | Today, Market, Opportunities, paper/real portfolio and shared readiness UI | Load, failure, stale, empty, partial and populated states; each displayed action reaches its intended result |
| User-facing evidence and learning | Research/agent/drawer components; strategy preflight and presentation tests | Named revision/hypothesis, exact blocker, matched independent cases and evidence-bound explanations |
| Promotion and statistical integrity | Existing strategy governance, walk-forward and after-cost outcome owners; stricter parameter preflight | Pass/fail/inconclusive from real evidence, not a run count; collection on with promotion off remains supported |
| Deployment and safety | OpenAPI generation, TypeScript/Vitest, architecture guards, release gate | API/frontend/schema identities match; application-role privileges and paper-only switches remain intact |

For the fill-to-outcome observation, record one identifier chain locally:
`strategy revision → opportunity episode → decision/publication → immutable ticket
→ paper order → entry quote/fill → mark → exit quote/fill → reconciled outcome`.
Record the actual timestamps and the reason for each transition. Do not use a
current revised thesis as the explanation for an older frozen decision.

## Verification responsibilities and stop conditions

Engineering tests use controlled fixtures and can verify failure handling without
production credentials. The local session owns provider/authentication checks,
real data gaps/backfill, browser interaction across the nine supplied surfaces,
and evidence that the complete lifecycle advances under actual eligible quotes.
Statistical edge remains an empirical result, not an implementation checkbox.

Do not merge/deploy as "profitable" on the basis of green tests. Stop if any test
requires relaxing a data-age limit, bypassing independent qualification, changing
an immutable ticket, filling from its own decision quote, or mixing observation
returns into funded-account NAV. Fix the upstream requirement instead. Preserve
an incomplete/inconclusive state when the evidence truly is not available.

Use `make release-gate` in the normal Python 3.11 / PostgreSQL 18 environment.
New focused checks include `tests/test_workstation_verification.py` in addition
to the tests listed above. The PR verification note records the exact checks
executed; an unavailable environment or coverage failure must not be relabeled
as a successful full release gate.
