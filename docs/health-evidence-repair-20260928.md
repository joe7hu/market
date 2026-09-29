# Health evidence pipeline repair — 28 September 2026

Base: `ac95270d061fed5884f6fcdd9ace744a32586813`.
Incident source: the seven findings supplied in the repair request. The original
`/tmp/market-health-analysis-20260928` report and production database were not
available in this workspace. This change does not claim production recovery.
All trade execution remains paper-only and all qualification gates remain.

## Repairs

**Crypto and shared prices.** Migration `20260928_0045` retains bounded indexed
source-tip reads but ranks confirmed candidates by observation time before
confirmation time. A late replay cannot replace a fresher cross-source quote.
Crypto daily candles keep their stored following-UTC-midnight close and original
trading date; a prematurely observed legacy candle cannot become a completed
close merely by arithmetic. Portfolio daily-history reads preserve that clock.
Assessment collection now repairs missing completed crypto candles, then runs
feature construction before decision publication. Its recurrence anticipates
UTC rollover, including weekends. Terminal-bar publication checks include crypto
and recheck UTC-day changes as well as stock-session changes. Failed collection
remains partial; there is no grace period pretending an absent candle exists.

**Recovery references.** Detector and collector share one bounded universe
resolver, including active events and discovered option underlyings. The daily
collector repairs the last three completed reference sessions for symbols not
already owned by the full monitored-universe collection. It requests a small
30-day provider window, admits only the exact missing genuine OHLCV candles, and
uses the existing source/fact identity. Four workers bound concurrent transport.
Retries are idempotent. No watchlist expansion or full-chain accumulation occurs.
Scoped dependency receipts cannot reset the full daily-source success clock.
This repairs reference availability prospectively; it does not manufacture past
slot, contract, continuity, or recovery-canary coverage.

**Stock validation and attribution.** The temporal preflight exposes independent
resolved observations, eligible training counts, actual label availability and
the earliest clock at which a new decision can have enough training evidence.
When no historical test decision can see even one training label, control
construction returns its genuine empty result without eight futile repeats.
A regression then adds truly later decisions and separately matured labels and
checks that causal controls can be produced without changing the original cohort.
Canonical attribution rules are deliberately unchanged: missing revisions,
signal/ranking lineage, and cash comparators cannot become approved stock plans.
Counterfactual outcomes are not relabeled as strategy performance.

**Candidate production.** The shipped option-agent cadence is one day, retaining
the existing one-run-per-day budget. New postmortem requests contain both the
historical decision strategy and a distinct current proposal baseline. The agent
is instructed to propose only evidence-backed changes against that supplied
baseline. A new candidate must match the baseline actually reviewed, which must
still be active on submission. Stale requests are rejected, not silently
retargeted. Historical decisions, bindings, proposals and results remain intact.
The versioned evidence request and baseline-aware requeue permit a fresh review
after a baseline transition. Provider A/B agent experiments remain disabled;
that switch is not the strategy-challenger experiment job.

**Research marks.** Option-leg reads enforce both observation and availability
cutoffs, choose the freshest observation and exclude future-observed quotes.
Research observations retain a bounded latest-check record and rejected quote
witnesses in their existing gap-event stream. Rejected quotes do not replace
accepted marks, P&L, exit evidence or funded-book fills. Gap identity ignores
changing rejected tick payloads within the existing time bucket to avoid storage
inflation. The workbench explains the execution-policy blocker separately from
its last accepted valuation. A later valid quote clears the current rejection,
while prior gap evidence remains. The original single missed close window cannot
be reconstructed without its production captures and scheduler timeline.

**Storage forecast.** Growth uses actual elapsed sample time, three distinct
sample days spanning at least 48 hours, and a recent sample. The rate takes the
larger whole-window/recent-window growth and considers both database allocation
and filesystem free-space loss. Negative growth is not a future reclamation
credit. Insufficient samples remain explicitly provisional at 0.7 GiB/day. The
15 GiB reserve, archive verification and physical-recovery-unknown status stay
intact. No live data was deleted or historical canary count changed.

## Verification

Failure cases were recorded before implementation in
`docs/acceptance/health-evidence-pipelines-20260928-scenarios.md`.
Provider transport is simulated; PostgreSQL 18 ingestion, migrations, readers,
research observations and proposal materialization use real database fixtures.

Against the unmodified base, the two crypto reproductions fail with (1) the old
daily price replacing the fresh quote and (2) 16:00 UTC instead of following
00:00 UTC. The repaired broader regression group passed 153 tests. The new
candidate-target regression first failed because the baseline was absent, then
passed after the producer repair. Generated panel-contract and high-signal Ruff
checks passed. The final pull request/CI receipt records the integrated results;
these are isolated tests, not production evidence.

## Deployment and evidence acceptance

Use the repository's managed migration/restart procedure with a verified backup;
apply schema `20260928_0045`, rebuild and restart all runtime components. Do not
run the deployed application against an older schema. Verify matching release
identities with `make release-smoke` and exercise the Health, Today, portfolio
and research pages. The price migration replaces a function; it does not rewrite
price history or relax availability. A downgrade restores the earlier selector
and therefore restores the incident behavior.

1. Run the regular daily collection and assessment/feature/decision chain. Check
   the bounded recovery-reference receipt, zero missing required reference dates,
   and the same actual detector denominator. At the next UTC midnight, confirm
   genuine completed crypto candles become available before feature publication;
   late daily confirmation must leave newer live quotes selected.
2. Inspect **effective** `option_agent.auto_run_seconds` and
   `MARKET_AGENT_REFRESH_SECONDS`. An explicit stored/environment zero can still
   pause the producer and must be deliberately changed in the deployment; this
   PR does not override production settings behind the operator's back. Confirm
   a new request contains `proposal_base`, its candidate uses the reviewed active
   parent, and the ordinary experiment job evaluates it without automatic approval.
3. Preserve 0/30 qualification while causal evidence is insufficient. Verify new
   point-in-time decisions continue to be recorded, their real outcomes mature,
   and training eligibility increases at the recorded clocks. Only qualified,
   fully lineage-backed future plans may enter canonical attribution or funding.
   Existing invalid historical plans are not repaired by inserting invented IDs.
4. Check `last_mark_check` and `mark_gap` evidence for overdue research positions.
   Wide spreads must retain the rejection and last accepted P&L. Confirm a later
   valid quote advances the mark, without creating an order in the funded book.
   For the incident's missed valid quote, compare its actual capture completion,
   observation/availability clocks and manager scheduling against the same policy.
5. Collect real daily accounting samples, verify elapsed span and measured rates,
   and measure filesystem space before claiming physical recovery. Full-history
   admission stays blocked whenever the reserve forecast fails. Recovery and
   full-history canaries require their remaining genuinely qualified sessions.

A successful merge cannot retroactively produce training labels, executable
liquidity, historical coverage, approved plans or physical free space. Those
acceptance conditions must remain visible after the software defects are fixed.
