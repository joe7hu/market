# Campaign validation

This is an execution aid for the existing campaign plans, not replacement
authority. Preserve both source plans, their ordered phase/gate locks, and the
existing checkpoint. This table does not declare any gate satisfied or waive a
locked command. The phase planner owns the final exact check-to-gate mapping.

## Four test stages

| Stage | Run | Escalate when |
| --- | --- | --- |
| Edit | The failing node plus the affected behavior/contract | A sibling caller, persistence rule, SQL guard, or consumer changes |
| Phase candidate | Owned workflow and affected shared contracts; `make test-workflow` for app-role paths | Schema/role changes add raw migration checks; shared runtime/fixtures affect the full suite |
| Release | `make release-gate` once on the frozen candidate, valid independent review, required migration and live route proof | Inputs or environment change; invalidate only affected proof |
| Observation | Real scheduled operation, source history, and locked observation window | Explicit failures require repair; elapsed time alone cannot supply observations |

`release-gate` includes slow tests, coverage,
contracts, lint, frontend tests/typecheck, build, and diff checks. It does not
rename an active campaign phase. `make coverage` skips slow tests by default,
and `make test-postgres` overlaps coverage. These are not equivalent gates.
If the source mandates both, both remain required. CI stays disabled until Joe
changes that policy; local evidence must never be called a remote CI pass.

For SQL-only edits, use the affected PostgreSQL node and `make lint`; add
`make contracts` if an API/panel contract changes. Use `make frontend` and
`make build` for frontend changes and the final release, not each SQL edit.
Use exact paths, not automatic filename-based test selection.

## Database isolation

- `migrated_postgres_dsn`: a fresh per-test clone of a session-migrated template.
- `application_postgres_dsn`: that same clone, using the configured application
  login. Seed as owner when necessary, commit, then exercise the real app path.
- `postgresql` / `postgres_dsn`: blank per-test databases in a separate process.
  Use these for migrations, recovery, or changes to shared role definitions.
- The foundation and stock-alpha role/signing-key modules retain raw migration
  fixtures because their migration inputs change per test.
- Roles are cluster-wide. Tests that create temporary roles must remove them;
  tests that modify shared roles belong on the raw process and restore state.
- Close pools before teardown. Never use production PGDATA, NAS/SMB, or a shared
  mutable database for test clones. Verify disk and dependencies before broad
  validation. Do not lower the storage reserve or add workers to hide setup cost.

`make test-workflow` covers a provider-shaped Treasury payload through the real
producer, persisted lineage, job completion and source-catalog API; unavailable
FRED remains explicit. It also checks application-role activation and denied
escalation in research, persisted manual cash funding capacity, and fixture
isolation. It is a focused baseline, not proof of every campaign workflow.
`make test-migrations` runs the raw-database foundation suite.

## Phase acceptance table

Before each phase, copy its row into the planner packet and expand it with the
exact owned gate IDs, actual paths being changed, input/output contract, positive
and negative cases, expected artifact, and escalation check. Global prerequisite:
the preceding source phase is accepted and landed. Final check for every row:
the source-required release checks plus its actual affected live routes. A
focused command below is a starting check, not sufficient acceptance by itself.

All commands below are `uv run pytest -q <paths>`. Database behavior uses the
migrated clone and the application login where production access matters.
Migrations additionally use the raw/restored database. Frontend consumers use
focused Vitest tests and typecheck when changed.

| Phase / behavior | Owned boundary and input → output | Focused paths | Positive / negative evidence and escalation |
| --- | --- | --- | --- |
| P0 release integrity | migrations, runtime readiness, release IDs → compatible deployed release | `tests/postgres/test_postgres_foundation.py tests/application_api/test_api.py` | Restore/upgrade/downgrade/re-upgrade and app-role routes succeed; incompatible schema blocks readiness. Any shared fixture/runtime change needs the full backend suite. |
| P1 actionable gaps | source status, collector completion, publication, inbox → current actionable task | `tests/postgres/test_postgres_source_health.py tests/options/decision_truth/test_decision_inbox.py tests/application_api/test_api.py` | Missing optional source stays explicit; expired rows stay in history; repair completion creates a new decision revision. Trace the real collector-to-publication job before closure. |
| P2 manual account | ledger → cash replay → allocation reader → SQL funding guard → portfolio API | `tests/test_portfolio_ledger.py tests/postgres/test_postgres_user_state.py tests/postgres/test_postgres_phase4_portfolio.py` | Deposit/withdrawal/reversal changes both displayed cash and funding capacity; stale preview, missing cash, duplicate input and unauthorized writes fail safely. Any authority change adds migration and production-login tests. |
| P3 usable inputs | provider payload → adapter → facts → scheduled publication | `tests/test_phase2_sources.py tests/postgres/test_postgres_phase2.py tests/postgres/test_application_workflows.py` | Actual provider shape persists clocks/lineage and reaches its consumer; malformed, inaccessible or short-history sources remain restricted. Add a real bounded scheduled run for the affected source. |
| P4 ticker packet | thesis/evidence/decision publication → ticker API → view | `tests/test_ticker_decision_contract.py tests/test_phase4_panel_contract.py tests/application_api/test_ui_data_access.py` | Complete and incomplete tickers render coherently; stale/cross-lineage evidence and display-price updates cannot rewrite authority. Add focused frontend checks when the consumer changes. |
| P5 research | observations → trial → artifact → forecast/rejection | `tests/test_research_validation.py tests/postgres/test_phase2_stock_alpha.py tests/postgres/test_postgres_phase3_strategy_factory.py` | Eligible observations reach stored output; leakage/negative controls reject promotion; missing qualification remains visible. Run through the production login and preserve genuine observation requirements. |
| P6 expression and paper follow-through | comparable expressions → paper plan → fills → attribution | `tests/options/history/test_option_expressions.py tests/postgres/test_ticker_paper_execution.py tests/postgres/test_postgres_phase4_portfolio.py` | Valid paper flow persists/replays; invalid lineage, liquidity, or allocation cannot become executable. Changes to fills/calibration add app-role, partial-fill and conservation checks. |
| P7 portfolio management | portfolio evidence → constraints → funded action queue | `tests/test_market_portfolio_decision_contract.py tests/test_phase4_portfolio.py tests/postgres/test_phase4_book_action_queue.py` | Conserved funding and current actions; missing cash, stale evidence and unavailable source actions remain blocked. Trace SQL guards and queue consumers together. |
| P8 daily workflow | inbox + ticker + portfolio → daily review/action | `tests/options/decision_truth/test_decision_inbox.py tests/options/decision_truth/test_decision_inbox_relay.py tests/application_api/test_api.py` | Current tasks and local review work with relay absent; delivery outages do not stop local computation. Prove the actual browser workflow before closure. |
| P9 sustained operation | accepted releases + scheduled runs + observations → final acceptance | `tests/contracts/test_architecture_guards.py tests/contracts/test_postgres_runtime_boundary.py` plus the required complete release gate | Reconcile every original gate, release identity and required trading-session window. No unit test or synthetic clock substitutes for the real observation period. |

## Validation ownership and review

For each broad check record: exact command, base/candidate/tree, environment,
owner, process/CI handle, state, exit code, counts, duration and result locator.
RUNNING is adopted, PASS is reused, FAIL is diagnosed at the smallest failing
node. A changed candidate cannot inherit a running old check. Preserve explicit
CI policy and locked commands. Review whole behaviors and batch findings before
the next full run; do not create a review/deploy cycle for each sibling defect.

The reviewer verifies actual Git identity and dependency readiness first. Its
result must identify the base/candidate, checks and outcomes, and either concrete
findings or explicit none. Empty/wrong-candidate results are not approval and
not code repair input. Keep healthy reviewers for repairs; preserve exhausted
recovery counts. Independent review and required checks precede deployment.

Run checks directly or save output to a log. A caller pipeline needs pipefail
in that same shell, including `make check | tail`:

```sh
set -o pipefail
make release-gate 2>&1 | tee /tmp/market-release.log
```

## Wait on one existing job

```sh
uv run python scripts/wait_for_job.py JOB_UUID --config config.yaml --timeout 1800
```

The waiter only reads the named job. It releases each database read before
sleeping, prints state changes, and exits 0 only for `succeeded`; other terminal
states/errors return 1, a still-running timeout returns 2. Timeout does not
cancel the job or prove job failure. Keep its process handle and use bounded
tool waits of at most 55 seconds; do not launch another copy while it runs.
The deadline may include the last bounded database read/connection wait.

## Measure before claiming campaign improvement

At base `49b51a4`, the same 16 source-health tests passed in 8.089 seconds with
per-test migrations and 4.347 seconds with the template (one local run each,
September 6). This is a 46% reduction for that sample; it is not a whole-suite
or live-campaign speed claim. JUnit/log evidence is in the implementation report.

For the next real phase retain a compact row with build/review/release elapsed
time, unique broad runs by candidate/environment, repair rounds, verdict delay,
controller input size and returned characters, plus setup/call/teardown timing.
Keep old evidence in a linked archive; never append raw logs to current control
state. Record a real live result before attributing speed to the skill change.
