# Daily assistant repair ledger

User request: implement the September 6 review and improvement plan. Base
`66fbf33`; branch `codex/market-assistant-repair`; target `main`. This ledger is
an additive repair record, not a replacement campaign or acceptance authority.
Preserve the original daily-assistant source plan, lock and reviewer history.

Constraints: PostgreSQL authority, paper execution only, holdings/watchlist
priority, fixed risk limits including stricter lane limits, no new subscriptions
or strategy families, no fabricated cash or market observations. Assignment
and recovery permissions stay unchanged. CI remains manually disabled.

| Item | Original phase | State | Evidence / remaining requirement |
|---|---|---|---|
| A1 category brief and totals, accurate event coverage | P1/P8 | done | Full finite brief read; 4/3/3/2 projection; explicit coverage; focused API/UI checks |
| A2 unsupported assumptions distinct from contrary evidence | P4 | done | Separate contract and panel; source attribution retained; regression checks |
| A3 canonical return denominator and clear blockers | P4/P7/P8 | done | Shared portfolio cost basis and blocker actions; held risks remain visible |
| B1 canonical Robinhood registration | P3 | done | Shared registrar retains immutable identity; 6 identity/content regressions |
| B2 scheduled history continuity and production role | P3/P6 | blocked | Existing 900-second schedule is correct. Live history guard needs more than 6.49 GiB additional free space to meet unchanged 30 GiB reserve. Prior missing sessions cannot be recreated |
| B3 provider identity and unambiguous company linking | P3 | done | Catalog aliases/names; ambiguous links remain unresolved; collection/linking/publication counts |
| B4 actual cash reconciliation | P2 | blocked | Joe must supply actual cash and effective date; existing UI remains available |
| C1 stock counterfactual target and independent observations | P5 | done | v3 model, 20-session net stock target, non-overlapping windows with lineage; 33 stock checks |
| C2 valid controls, baselines and stable experiment identity | P5 | done | Stable dataset fingerprint; valid negative controls; no repeated trials for unchanged evidence; separate promotion clock |
| D1 consistent mutation capability and paper authority | P5/P6 | done | Nine numeric gates only; unsupported/relaxed filters stay blocked; stage-specific metric requirements |
| D2 candidate observation and paper lifecycle evidence | P6 | done | Separate publications and later complete quotes; existing shared-risk paper execution; actual journal VWAP/fees and observed liquidation marks; fixed full-union comparison windows |
| D3 atomic promotion and fresh rollback publication | P6 | done | Authority lock and expected parent; exact journal proof; fresh analysis after rollback; 38 focused checks |
| E1 Research strategy results and useful opportunities | P5/P8 | done | Bounded summary endpoint and Research view; top-three held/watched briefs; focused API/database/UI checks |
| E2 signal and review utility metrics | P8/P9 | done | Explicit helpful/not-helpful feedback, separate workflow counts; independent sample metrics with missing values |
| E3 bounded decision-funnel read | P1/P8 | done | SQL-validated CASH fast path and per-request snapshot cache; live 806-row read took 0.431 seconds; 3,000 ms timeout unchanged |
| V1 focused checks, independent review, release gate | release | open | One validation owner for frozen candidate |
| V2 deployment, live API/browser and durable checkpoint | release | open | Exact runtime identity |
| V3 ten-session operational observation | P9 | blocked | Starts after accepted deployment; real sessions only |
| V4 real strategy qualification and improvement proof | P5/P6/P9 | blocked | Requires sufficient valid market and execution outcomes |

Stop condition: no unprocessed software items; required checks, review and live
proof pass. External evidence requirements remain explicitly open acceptance
requirements and cannot be replaced by fixtures.

## Learning contract

Stock labels are the net stock counterfactual over 20 trading sessions. The
stable economic episode can contribute later non-overlapping windows. The
model, target, and cost versions exclude incompatible prior artifacts. Current
live data has no mature samples under this definition; old trial counts are
not evidence of a trained strategy.

Options comparisons retain the whole observed union of incumbent/candidate
episodes. A lineage-valid gate rejection is confirmed CASH; missing, pending,
unfilled, or unmeasurable observations remain unknown. Each evaluation starts
after the initial partial session and closes at the first observation date
after its required span. The end date must finish before evaluation can pass.
Returns never select the window, and later pending batches cannot remove a
loss or move that boundary. Actual candidate fills stay separate from parent
fills and shadow outcomes.

Walk-forward and shadow require ten common metrics; execution-grade paper
requires all fourteen. Realized P&L, turnover, slippage, and capacity are not
invented for the earlier stages. The sample/span thresholds remain 100/120,
30/30, and 20/20. Positive confidence, comparison performance, false-positive
slack, exact execution proof, and all risk limits remain unchanged.

The reserve check found no safe large inactive test directories to remove.
No market history, user files, app sessions, or updater files were deleted.

Pre-review integration: 79 architecture/governance/experiment checks and 10
cohort/measurement checks passed. TypeScript, the focused UI suite, generated
contracts, and Ruff passed. These are focused checks, not the full release
gate. Paper drawdown uses its own observed executable marks with quote and
journal lineage; it does not reuse a shadow trade's drawdown.

## Independent review repair batch

The first Codex review inspected the frozen file manifest
`e6172dff5b13ca6243d65422afab21c516741463d9a9f44cc576652b883c0290`
and returned seven findings. The helper rejected the report's absolute file
paths, so this is repair input, not an accepted review result. Review session:
`01a079f0-dac5-7a81-8a96-efb929530231`. A corrected candidate needs a fresh
accepted review with repository-relative paths. The item states above describe
implemented scope; the affected acceptance checks remain open until this batch
and V1/V2 pass.

| Finding | Item | State | Required correction |
|---|---|---|---|
| R1 P1 | D2 | fixed; review pending | Persist each candidate's point-in-time market regime |
| R2 P1 | C1 | fixed; review pending | Count distinct real trading sessions at stock outcome production and reject false legacy maturity |
| R3 P1 | D3 | fixed; review pending | Include verified new shadow and actual paper outcomes in rollback, scoped after promotion |
| R4 P2 | D2 | fixed; review pending | Continue existing observations when automatic promotion is off |
| R5 P2 | D2 | fixed; review pending | Measure shadow drawdown from peak wealth |
| R6 P2 | D2 | fixed; review pending | Prevent blocked candidates from starving later qualified candidates |
| R7 P2 | E1 | fixed; review pending | Show versioned independent options evidence and its limits in Research |
| R8 root review | D2 | fixed; review pending | Apply confidence to the same complete trade/CASH comparison universe |
| R9 root live trace | C1 | fixed; review pending | Rotate bounded stock outcome batches by last evaluation; the scheduled limit of 25 must not repeatedly select the oldest decisions |
| R10 independent focused review | D2/D3 | fixed; review pending | Qualify shadow columns separately from actual-paper eligibility, reject nonfinite costs, and apply rollback's final 20-episode limit after source precedence |

Repair validation: 81 stock/price/outcome checks passed, 35 option lifecycle
checks passed after the initial-capital drawdown repair, and all eight rollback
variants passed after separate shadow qualification and finite-cost checks.
Research passed 47 Python and seven UI checks. Root comparison/governance
checks passed 50 tests; generated contracts, TypeScript and scoped Ruff passed.
These are focused checks. Final independent acceptance and the full release
gate remain open.


## Second independent review

The second review completed with a valid structured `patch is incorrect`
verdict against frozen manifest
`8dddf156bf843b2c902e8028c90f4df523538952f12394d3c8fd62d9678ebf49`.
Report: `/tmp/market-assistant-review-v2.json`. All four findings are retained
below. No production deployment or full release gate preceded this verdict.

| Finding | Item | State | Required correction |
|---|---|---|---|
| R11 P1 | C2 | fixed; review pending | Randomized controls must measure changed predictions and use the scheduled producer in the success check |
| R12 P1 | D2 | fixed; review pending | Retain and score the same contract set before and after promotion |
| R13 P2 | D2 | fixed; review pending | Separate entry expiry from holding exits in both actual-paper checks |
| R14 P2 | C1 | fixed; review pending | Preserve explicitly verified terminal delisting outcomes after the full horizon |
| R15 independent focused review | D2/E1 | fixed; review pending | Mark fixed windows with terminal observation gaps as blocked; stop new entries while preserving holding management and all trial evidence |

Before this review, 98 integrated architecture, governance, cohort and experiment
checks passed. Restored production-shaped data passed migration to 0003 and 15
API reads, including Research and the decision funnel. Browser checks covered
Today, Research, isolated usefulness feedback, and a fresh MSFT thesis
publication. Fixture feedback and publication were confined to the restored
copy. They are not genuine trading outcomes or user feedback.

The pre-release production backup is verified at
`/Volumes/agent/data-sources/market-mini/postgres-backups/market-before-assistant-repair-20260907.dump`.
SHA-256: `8c3c4c1ebe1eea275f9beddbc0438e939893d68bbe6eeb98a26890e4b9e941fd`.


The R15 liveness check found that later complete observations cannot repair a
terminal unknown in the original fixed window. Such a window now reports
`blocked_terminal_evidence` rather than indefinitely collecting. Its original
unknowns, losses, denominator and null return bounds remain intact. There is no
rolling window or automatic retry with the same evidence. Existing holdings
continue through their management path; new entries are gated. Research directs
the user to repair collection before a separate prospective trial.


R11 controls now measure out-of-sample Brier-loss improvement over the
control generator's null target probability: permutation prevalence for shuffled
labels and the Gaussian probability of exceeding each row's cost for white
noise. These references belong only to controls, not live model features. The economic qualification gates remain unchanged;
the actual scheduled producer passed a PostgreSQL success-path check with mixed
winning and losing observations. R14 carries verified terminal values to the
original 20-session boundary and retains source and mark clocks.

R12 uses the same retained contracts and expectancy scoring before and after
promotion. R13 keeps entry expiry at staging/fill boundaries and applies the
shared holding exits after a fill. An expired partial-entry remainder does not
discard the filled holding. R15 adds an entry-only terminal-evidence gate while
preserving existing management and all other qualification checks.

Focused evidence: 46 option experiment/paper checks and 93 shared-ticket, database and architecture checks; 66 combined stock, ticker paper and research-validation checks, including the
scheduled PostgreSQL success path and five terminal-loss checks; 42 root
cohort/governance checks; one application-role Research terminal-gap check.
Final acceptance is recorded separately after the third independent review and
full release gate. This ledger is a pre-release implementation snapshot.


## Third independent review

The third review returned a valid `patch is incorrect` verdict against manifest
`a671facb81f919552944d87187934da7dc932260ade8aeb79db590be3fa77870`.
Report: `/tmp/market-assistant-review-v3.json`. The reviewed tree remained frozen.

| Finding | Item | State | Required correction |
|---|---|---|---|
| R16 P1 | D2 | fixed; review pending | Apply duration to the full fixed comparison window; retain a separate completed-trade minimum and persist those window dates |
| R17 P1 | D2 | fixed; review pending | Bind the Radar scorecard to retained, lineage-checked incumbent observations, independent of later refreshes and private candidates |
| R18 P2 | D2 | fixed; review pending | Keep score-rejected decisions as confirmed CASH, including legacy pending shadows |
| R19 P2 | C1 | fixed; review pending | Keep delistings after a target boundary from overwriting its completed fixed-horizon mark |
| R20 P2 | D2 | fixed; review pending | Measure actual partial holdings after a recorded cancellation of the remaining entry quantity |

The v3 restored app passed all 15 API probes, with matching review labels and
schema 0003. Research took 0.197 seconds and the funnel 0.620 seconds; the
unchanged larger watchlist response took 8.214 seconds. These response times do
not change the 3,000 ms per-statement timeout. The Mac subsequently locked;
a fresh visual check is pending unlock. Prior restored browser evidence remains
preserved. No production deployment or full release gate has run.


R16 focused checks passed for a Saturday window start, twenty completed weekday
trades, and a confirmed-CASH end boundary. The complete window meets the
30-day duration while a 21-trade requirement still fails. Stored evaluation
periods use the same comparison dates; historical replay duration is unchanged.
R19 passed 57 stock/outcome checks. A broader focused run exposed one old
manual-promotion fixture without episode identity, ordered execution clocks, or
journal proof; the fixture is repaired without relaxing production checks.


R17 passed 22 focused scorecard/application-role/architecture checks. R18/R20
passed 51 experiment/paper checks and all three final canceled-partial variants.
The repaired manual-promotion fixture module passed 47 tests. R16 received a
bounded independent review with no defect; its 13 cohort checks passed. The
additional rejected-closure reader regression and transition checks passed
15 tests. Ruff and whitespace checks passed. All implementation owners are
frozen for the fourth whole-branch independent review. Final acceptance and
release evidence will be recorded in the additive GBrain repair page and
release artifacts rather than inferred from this pre-release ledger.

## Fourth independent review and production query diagnosis

The fourth review returned `patch is incorrect` for commit
`fc3f62f96d69a5c1a6e360d5e59494937184710a` (tree
`9e60d45bc7c1dadb0e3ea8883986e1092ff011d8`). Its report is preserved at
`/Users/joehu/.codex/artifacts/market-assistant-repair-20260907/market-assistant-review-v4.json`.
The branch was reopened for the following repairs. No acceptance or deployment
is implied by the first candidate commit.

| Finding | State | Required correction |
|---|---|---|
| R21 P1 | fixed; review pending | Use exact historical publication authority for both candidate and incumbent holdings; keep current publication required for new entries |
| R22 P1 | fixed; review pending | Include later actual paper losses in the fixed episode comparison; attempted but incomplete paper remains unknown |
| R23 P2 | fixed; review pending | Cancel all blocked partial-entry remainders and continue managing the actual filled holding |
| R24 P2 | fixed; review pending | Persist and display actual stock OOS sample count; preserve training count separately and missing OOS as unknown |
| R25 runtime | fixed; review pending | Select incumbent episode IDs with existing indexes before payload joins, within the unchanged three-second statement limit |
| R26 evidence | fixed; review pending | Exclude genuinely legacy observations from current cohort qualification; expose raw and independent excluded counts separately |
| R27 lifecycle | fixed; review pending | Preserve exact existing candidate holdings and entered shadows after successful promotion of that revision |
| R28 accounting | fixed; review pending | Keep episodes with multiple paper orders unknown rather than omit other losses or incomplete exposure behind one completed order |
| R29 classification | fixed; review pending | Treat the known cash-secured-put sizing exclusion as ineligible evidence, without turning missing cash into a global scorecard integrity defect |

The restored fc3f62f app returned HTTP 200 on all 18 probes, but Radar reported
`scorecard_query_timeout` and INVALID. This is a failed semantic check. A
read-only production check also reproduced the timeout with the application
role and unchanged 3,000 ms statement limit. The episode-key query repair
prototype returned the identical 2,044 rows in 0.443 seconds under that limit.
Exact query, plan and parity evidence are in
`/tmp/market-assistant-scorecard-diagnosis-fc3f62f/`.

R22 passed 16 fixed-cohort checks, including a later losing paper execution
after an initial rejection and an incomplete execution that remains unknown.
R24 passed 72 focused producer, PostgreSQL, Research and API checks. The actual
stock producer test verifies different training and OOS counts and recomputes
the reported Brier score and return bound from OOS predictions.

R21/R23/R27 passed 66 experiment/paper checks and 93 related ticket,
PostgreSQL publication and architecture checks. All six partial-entry blockers
retain filled quantities; exact promotion, rollback, future promotion and other
successor cases are covered. Independent R22 follow-up found R28 through the
real public staging owner: two orders are permitted within a ticket's quantity
limit, while the current measurement selects only one. A +20% selected return
could omit a second -80% loss or an earlier incomplete holding. The repair must
preserve the full denominator and mark that unsupported combined exposure
unknown; it must not change staging limits or invent an aggregated drawdown.

The implemented R25 read, including scope counts, took 0.783 seconds under the
production read-only application role and unchanged timeout. It retains 2,044
current episodes and separately reports 71,263 excluded legacy captures across
14,353 episodes. No legacy return is used. The remaining 136
`quality_status_sizing_blocked` rows were traced to the cash-secured-put producer:
all had `sample_eligible=false` and `missing_cash_context`. R29 keeps these
expected sizing exclusions out of return samples without treating them as a
global integrity failure. Actual cash, sizing and execution gates remain intact.

R25/R26/R29 passed 24 focused scorecard checks and five architecture checks.
The final read-only production scorecard took 0.766 seconds: `COLLECTING`, zero
resolved episodes, null expectancy, and explicit excluded legacy counts. R24
also received a bounded independent review with no actionable findings; 65
Research, producer and scheduler checks passed in that review. The complete
candidate still requires independent acceptance and the full release gate.

R28 passed 47 unit checks, 11 public staging/rollback PostgreSQL cases and six
final staging/promotion/action checks. The real staging owner creates two
permitted orders and fills/exits a winner and loser. The episode remains
unknown with its original denominator; cached winner-only evidence is rejected.
Rollback retains unknown exposure within its trailing twenty, with no older
loss substituted. Order counts are bound to each evidence cutoff. Multi-order
return and drawdown aggregation are deliberately unsupported; no staging or
risk policy changed. All repair owners are now frozen for review v5.

## Fifth independent review

Review v5 returned `patch is incorrect` for commit
`cce766b754b7833250b809f41ce8c5acf3e1df52`, tree
`6584975364de4fa64197cd92f5d4e092d683b723`. The complete report is preserved in
`/Users/joehu/.codex/artifacts/market-assistant-repair-20260907/market-assistant-review-v5.json`.

| Finding | State | Required correction |
|---|---|---|
| R30 P1 | fixed; review pending | Grant the production application role only the shadow lifecycle updates it needs; prove the scheduled writer with a non-owner login |
| R31 P2 | fixed; review pending | Keep a single incomplete paper execution unknown during rollback; do not substitute its closed shadow or an older episode |

The exact cce766b restored app passed 18 HTTP reads and semantic release/schema
checks. Radar returned in 1.566 seconds, with no data-health defects,
`COLLECTING`, zero resolved episodes and null expectancy. Research took 0.203
seconds; the funnel took 0.622 seconds. This read proof does not cover the
missing writer privilege identified by the reviewer. The full release gate,
landing and production deployment have not run.

R31 passed 14 PostgreSQL cases. Open orders, partial exits and missing exit
journal evidence stay unknown within the trailing twenty, with no older-loss
backfill; complete actual returns retain priority and zero-order shadow losses
still trigger rollback at the unchanged threshold.

R30 application-login execution reproduced the shadow lock/update denial.
After only the eight shadow column grants, the complete shadow producer/fill/
exit path passed and exposed a second denial at the existing paper-order
INSERT. The repair therefore includes only the insert/update columns used by
the current shared paper staging and management owners. Immutable intent updates,
deletion, ownership, protected policy authority and assignment/recovery
permissions remain unchanged. A complete application-login paper exit is
required before this writer repair can be accepted.

R30 passed 76 experiment, paper, migration and application-workflow checks.
Migration 0004 adds only eight shadow UPDATE, fifteen paper INSERT, thirteen
leg INSERT and eighteen paper lifecycle UPDATE column privileges. It grants
no table-wide writer privilege. A non-owner application login completes real
incumbent/candidate shadow advancement, paper staging/legs/entry/profit exit,
and partial fill/thesis invalidation/exit with actual journal fees. The API
statement timeout remains three seconds. The migration round-trip preserves
the exact schema contract, and protected mutations remain denied. All owners
are frozen for review v6 and restored migration validation.

## Sixth independent review

Review v6 returned `patch is incorrect` for commit
`e2e66832f740a3c419aab35c7e65c159137aad59`, tree
`4e68bc2f669878d67aa767a3f57546c3d76bb96c`. Its report is preserved in the same
artifact archive as `market-assistant-review-v6.json`.

| Finding | State | Required correction |
|---|---|---|
| R32 P2 | fixed; review pending | Order unresolved rollback exposure by its actual paper-attempt clock, including a later order after an older shadow closed |
| R33 P2 | fixed; review pending | Write existing paper assignment facts to mutable lifecycle evidence; keep immutable policy and current assignment permissions unchanged |

The exact e2e6683 restored database passed migration 0003→0004→0003→0004
with exact schema equality after reversal, and retained denied intent updates
and deletion. Its rebuilt app passed 18 HTTP reads and semantic checks on
schema 0004 with all release labels matching. Radar took 1.385 seconds,
Research 0.194 seconds and the funnel 0.557 seconds. The two review findings
remain software release blockers; no full gate or production deployment ran.

R32 preserves the first paper-attempt clock and adds the latest attempt clock
under the same evidence cutoff. Unknown rollback exposure uses the maximum
relevant evidence clock. Eighteen cohort checks, fifteen existing PostgreSQL
cases and two new later-order PostgreSQL cases passed. An old shadow followed
by twenty completed episodes and a later open order remains unknown in the
trailing window; a future-at-cutoff order does not enter the count or clocks.
The denominator, first-window rule and rollback threshold are unchanged.

R33 stores assignment facts in `execution_quote.assignment`, preserving all
prior quote/mark evidence and immutable policy. The remaining assigned quantity
gets one journal exit with intrinsic settlement and actual costs. Tracing that
loss exposed the same entry-price issue in ordinary exits after fills at
different prices. Both exits now reuse the existing journal-totals owner with
liquidation marks; paid entry fees are allocated by exited quantity. Missing
or future-at-cutoff journal evidence leaves P&L unknown. The obsolete fixed-fee
arithmetic helper is removed. Eighty option, execution and writer-privilege
checks passed, including application-login full/partial assignment, later batch
management, mixed-price entry fills, repeat idempotency and the shared loss
halt. No privilege, assignment configuration or recovery authority changed.

## Seventh independent review

Review v7 returned `patch is incorrect` for commit
`26bd582eb9aec4fc85782ce22eae6f5f3abea274`, tree
`c402923d0137f2f4a289031f9e005693dc14a230`. The report is archived as
`market-assistant-review-v7.json` with the prior independent reports.

| Finding | State | Required correction |
|---|---|---|
| R34 P2 | fixed; review pending | Keep unknown exit-loss accounting in the shared entry gate until reconciled; never default it to zero, while preserving holding exits |
| R35 P2 | fixed; review pending | Require the correct expiration-session close and confirmed settlement mark before paper assignment; stale spot quotes leave settlement unresolved |

The exact 26bd582 restored API passed 18 HTTP reads and semantic checks on
schema 0004. The new findings concern downstream accounting and settlement
evidence, not those read paths. No accepted complete review, full release gate,
landing, production migration or deployment has occurred.

R35 uses the existing exact-session `confirmed_daily_bars` reader and market
calendar, including early closes. It records source/fact and observation,
availability and confirmation clocks. ITM puts settle as assigned; equal/OTM
puts settle at zero intrinsic as expired unassigned. Missing, stale, early,
future, inactive, failed or nonfinite evidence keeps settlement pending. Before
close, executable holding risk exits remain available; later holdings still run
when settlement is pending. The three focused suites passed 102 tests; 35
targeted checks passed after moving shared journal totals into the existing
ledger owner to keep the execution-to-ledger import direction. No new provider,
grant, assignment configuration or authority was added.

R34 blocks new entries across all three option lanes when exit accounting is
unresolved, including across day changes. It reuses the same exact-order fill
and fee totals for read-only reconciliation; the original exit history remains
unchanged. Complete restored journals can clear the unknown block, with known
P&L attributed to its New York exit day and the configured loss halt unchanged.
The shared blocker action now directs the user to reconcile the fill/fee journal.
The combined ledger, full experiment, execution and import-guard run passed
115 tests. Actual normal partial exits block a later public stage while the
remaining holding exits; repaired journals reconcile the original unknown
exits to -62.60. Missing, malformed, nonfinite, wrong-identity, wrong-quantity,
wrong-multiplier and future evidence remain blocked. No new reconciliation
table, workflow, grant or risk limit was added.

## Eighth independent review

Review v8 returned `patch is incorrect` for commit
`52516770d0bfe56e8c74bfc8e753ff7f9bb55703`, tree
`e46d6226d5845f167180c573db05d459f550416b`. The report is archived as
`market-assistant-review-v8.json` with prior review history.

| Finding | State | Required correction |
|---|---|---|
| R36 P2 | fixed; review pending | Preserve conflicting entry/exit multipliers during reconciliation; complete fees alone cannot resolve that uncertainty |
| R37 P2 | fixed; review pending | Use the actual market-session close in daily-bar ingestion and prove early-close settlement through the real producer |

The exact restored 5251677 app passed all 18 HTTP reads and semantic checks on
schema 0004. The real experiment job also ran under `market_app` via the
non-owner login with the three-second API timeout. It skipped all 47 existing
candidates for unsupported parameters; no candidate was forced through a gate.
Writer-proof evidence is archived as `experiment-job-proof.json`. No complete
review acceptance, full release gate, landing or production deployment yet.

R36 binds each entry and exit journal to its observed contract multiplier.
Partial fills cannot replace the original filled basis; an incompatible
remainder is canceled while the holding remains managed. Every exit leg must
agree before a scalar multiplier is used. Missing legacy evidence or a recorded
conflict remains unknown in loss reconciliation, liquidation marks, learning,
rollback and stored promotion proof. One shared SQL predicate supplies these
readers. The combined run passed 190 checks; eight settlement test comparisons
needed time-zone normalization and are tracked in R37. Three final normal and
multi-leg exit checks passed after the last caller change. No R36 schema,
authority or risk-limit changes were required.

R37 uses the actual US equity/ETF session close at the daily-bar producer and
preserves stable fact identity, archived versions and original availability.
Legitimate reingestion records a corrected clock with current availability and
updates its exact materialized quote. Migration 20260907_0005 corrects only the
existing current-price function, retaining its owner, privileges and signature.
Verified early-close facts use their actual clocks; legacy winter prices retain
the prior close fallback. Premature ordinary-day 13:00 captures remain excluded.
Foreign, non-equity and daily_quote clock behavior is preserved. Exact schema,
owner and ACL equality passed for downgrade and re-upgrade. Twenty-four
producer, reader, history, migration and writer checks passed, followed by five
existing ingestion and import checks. Root then passed all 18 application-login
settlement variants against schema 0005 in 6.06 seconds. All changed Python files
pass Ruff; diff checks pass. Independent acceptance and the full release gate
remain pending. No production changes have occurred.

## Ninth independent review

Review v9 returned `patch is incorrect` for commit
`ff268a24e17c8955afbecc3bea787b600a31f463`, tree
`84c6e5f4a49dda4c921baee2e75e48275eca9edc`. The report is archived as
`market-assistant-review-v9.json` with prior review history.

| Finding | State | Required correction |
|---|---|---|
| R38 P2 | fixed; review pending | Select corrected versions by stable fact identity in remaining price readers, preserving old-cutoff availability |

The exact ff268a2 restored app passed 18 HTTP and semantic checks on schema
0005. Radar returned in 1.332 seconds with no integrity defect; Research took
0.208 seconds and the funnel 0.622 seconds. The actual experiment job ran under
the non-owner login and rejected all 47 unsupported candidates. Restored
0004/0005 migration reversal was exact; only the intended current-price
function changed. These proofs do not resolve the newly identified downstream
archived-version defect. Full release gate and production deployment have not
run. Final visual proof remains pending the locked Mac.

R38 selects each confirmed version by stable fact identity after applying its
availability and completed-ingestion cutoff. Migration 20260907_0006 adds two
scoped invoker readers and updates the two current views without changing
columns, owners or existing view privileges. Explicit historical callers use
their exact cutoffs; the bounded watchlist and portfolio unions use the same
stable identity rule. Existing current_price_at and confirmed_daily_bars have
different source/price policies, so substituting them would change more than
version selection. No separate framework or source policy was added.

Seventy-six affected checks passed in 19.13 seconds. They include actual changed
OHLCV reingestion through scoped/full watchlist technicals and portfolio prices,
old-cutoff preservation, peer-return correction with two cutoffs, the 640-peer
query under the unchanged 3000 ms limit, option-event detector and maintenance
paths, and exact migration/owner/ACL reversal. An older peer fixture was aligned
with its recorded completed-ingestion time; the future-availability exclusion
remains intact. The schema delta is exactly two views, two functions and their
two execute ACLs. Ruff and diff checks pass. Independent review and the full
release gate remain pending.

## Independent acceptance and release-check correction

Review v10 accepted clean commit `57e6af10655d842b243a0460af5a18442d738c3d`
with no findings. The restored 0006 schema and all 18 API/semantic probes passed.
The first full release-gate attempt stopped in architecture guards: the R28
regression imported a private governance helper. The test now stores the
winner-only evidence and reads the public governance readiness method. Its
control verifies that the envelope is structurally real before database proof
rejects it, so unrelated missing metrics cannot satisfy the assertion. Both the
failed architecture node and affected regression pass (2 tests, 2.01 seconds).
Only this test and the repair record changed; production source and migrations
remain identical to the independently accepted candidate. The remaining release
gate has not completed and production is unchanged.

The bounded independent follow-up accepted `ae5eeabf58d2719c5869cee00d026a6457a6c390`
against v10's accepted source. The next full release gate completed with 1,803
backend passes, 26 failures and 82.00% coverage in 793.15 seconds. Contracts,
26 guards, Ruff, 111 frontend tests, typecheck and build passed. The failures
were obsolete test fixtures; no application changes were needed:

- Promotion API evidence now has independent episodes, ordered clocks and
  complete entry/exit journals with fees and multipliers; both API tests pass.
- The recovery staging double explicitly supports the shared exit-row read;
  the journal assertion remains and its test passes.
- Stock lane fixtures use the current 20-session tactical target and complete
  lineage; positive qualification and late-OOS rejection pass (three tests).
- The generic outcome test uses the legacy generic producer it is intended to
  test, with actual post-capture availability; portfolio fixtures complete
  ingestion after writing facts. All three outcome/price tests pass.
- Baseline/adoption tests create the explicit baseline from a raw database,
  then compare adoption with the current migrated schema. Drift/permission
  refusal, rollback and secret preservation remain checked (22 tests pass).

All six changed test files pass Ruff and diff checks. The exact application and
migration source remains that accepted in review v10. These corrected fixtures
need independent follow-up and a complete passing release gate before release.
Fresh production backup was completed and its TOC/checksum verified at
2026-09-07T08:52:51Z:
`/Volumes/agent/data-sources/market-mini/postgres-backups/market-before-assistant-release-ae5eeab-20260907.dump`,
SHA256 `ebe48bcc14835f50cf5f40cc82e533f87a031cb0c1d5277e8276719edff638f3`.
The original backup and all failed-gate logs remain preserved. Production is
unchanged; no cash, outcome, qualification or trading session was invented.
