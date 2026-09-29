# Health evidence pipeline repair: failure cases and acceptance

The supplied September 28 report is the incident input. Its `/tmp` reproduction
files and the production database are not available in this workspace. These
cases are specified before implementation. PostgreSQL tests exercise normal
source writers and readers; provider transport is the only simulated boundary.
No test fixture is production evidence.

1. A late daily-bar confirmation must not replace a fresher cross-source quote.
   Same-observation corrections still require confirmed availability; future and
   failed corrections cannot hide the last confirmed fact. Crypto daily bars
   close at the following UTC midnight, retaining their original trading date.
   A legacy incomplete crypto bar must not become a completed close by arithmetic.
2. At UTC rollover, frequent assessment collection must repair the missing
   completed crypto candle before rebuilding features and publishing decisions.
   Only genuine OHLCV candles are accepted, no quotes-as-candles, no backdated
   availability, and failures remain partial. Existing completed candles avoid
   further provider calls and duplicate writes.
3. The daily reference collector and recovery detector must use the same bounded
   universe, including active events and discovered option underlyings. Dependency
   collection does not alter watchlists. Fetch only the exact missing completed
   sessions; do not collect full chains or hundreds of bars for dependency-only
   symbols. Missing references remain visible to detector coverage gates.
4. A cohort with outcomes first available after every test decision has no
   causal training set. Empty-control work is avoided, with explicit timing
   diagnostics. A genuinely later, independently resolved cohort can use those
   labels. No outcome, feature, revision, authorization, or plan is backdated.
   Canonical attribution still refuses unqualified or incomplete plans, while
   counterfactual observations remain research-only.
5. Enable the already-approved, bounded option-agent producer cadence. Generated
   proposals must reference the current immutable implementation; no old
   superseded revision is rebound, and candidate experiments/promotion retain
   every existing gate. Provider A/B experiments are not strategy challengers.
6. Option quote selection is bounded by both observation and availability clocks
   and prioritizes observation freshness. A wide spread remains non-executable.
   Rejected quote witnesses and policy reasons are persisted separately from
   accepted P&L, making a missed quote window diagnosable without inventing fills.
   Subsequent valid quotes clear the current blocker and preserve gap history.
7. Forecasting must use elapsed sample time, not calendar-date subtraction that
   can turn minutes across midnight into days. Three distinct, sufficiently
   separated production samples are needed for a measured forecast. A recent
   growth burst must not be hidden by a flat long-period endpoint. Free-space
   recovery is not inferred from logical deletes, and the 15 GiB reserve remains.

Run focused PostgreSQL source/feature/price, detector, options experiment,
stock-alpha and storage tests; then guards, lint and the integrated release gate.
Live acceptance still requires deployment, real producer runs, later causal
stock outcomes, qualified option sessions, and observed disk accounting. A PR
cannot retroactively create those production facts.

## Current-baseline proposal recovery

A new postmortem may review an old decision without reusing its obsolete code
binding as the target of a new experiment. Its immutable request must include
both the historical strategy and an explicit, still-active proposal baseline.
The agent must see that distinction. Test that a proposal creates a new current-
implementation candidate, retains the original decision/revision, and cannot
claim historical performance for its new parent. A target promoted/superseded
between queueing and submission must be rejected, not silently retargeted. Old
requests without this explicit target retain their old lineage and exclusions.
