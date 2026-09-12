# Trading and learning workbench

The workbench uses PostgreSQL paper orders, deterministic journal fills and verified
marks. Research outcomes and shadow observations do not contribute money to the
paper book. No live order authority is added.

## Inspecting evidence

- `/portfolio/paper` shows all-revision paper history in the canonical `paper` book.
  Book/sleeve, symbol, instrument kind, lifecycle, date, lane, structure and evidence filters share
  one route-backed scope. Its header, realized curve, first 100 trades, cursor pages
  and explicit CSV export carry that scope and snapshot identity. Point selection
  opens the contributing trade. An accessible event table provides the same links.
- The primary paper view leads with verified P&L, drawdown, entry/exit/open-position
  event overlays and attribution cohorts. Empty books show the paper-learning gate
  and its next action instead of a blank dashboard. Trade details show stored decision
  rationale, forecasts, the deterministic policy, the immutable ticket, individual
  fills, fees, multipliers, mark provenance and research outcomes. An order's origin
  is unattributed unless evidence establishes it.
- Research contains Overview, Strategies, Predictions and Experiments. Sources stays
  at `/sources`. Revision details link to the canonical paper book under a strategy
  filter and expose failed trials and backend gate measurements.
- `/research/runs/:runId` displays the original provider request and output.
  New advisor requests retain the effective system prompt, exact transformed input,
  output schema and content hash before the provider call. Response foreign keys
  protect the generating task. Legacy missing artifacts remain explicitly missing.
- `/research/artifacts/:artifactId` shows known database artifact metadata and its
  bounded preview. Metadata-only records do not claim retained file contents.

## Accounting and scope limits

The chart is **verified realized net P&L**, not a whole-book NAV series. Open-position
P&L is shown separately with mark coverage. Opening paper capital and external-flow
history are not available from the present source contract; NAV and capital-normalized
returns remain unavailable. A staged limit is never treated as a fill. Missing or
conflicting historical fees and option multipliers remain unreconciled.

The paper scope includes optional sleeve metadata so future sleeves cannot be mixed
silently. The current schema constrains this workbench to the paper book; no second
ledger or live-order book is introduced. Reconciliation totals are calculated over
the full filtered order population, not just the first page.

Display sampling retains bucket endpoints, P&L extrema and drawdown troughs in at
most 2,000 points. Drawdown statistics use the full realized event series. Performance
reads are bounded at 10,000 orders and disclose that ceiling. If it is exceeded,
whole-scope net P&L, unrealized P&L and drawdown are withheld. The known realized
subtotal remains labeled partial. This ceiling is not a claim of full-population
support beyond the documented benchmark.

The workbench reads a PostgreSQL-maintained `analysis.paper_trade_projection` for
fill totals and event packets and an `analysis.paper_current_mark_projection` for
confirmed stock marks. Journal/order triggers refresh only the affected rows; detail
drilldown still reads the canonical ledger and evidence joins.

## Advisor evaluation contract

`continuous-advisor-score.v3` keeps invalidation event truth separate from classifier
correctness. A false event at probability 0.20 has Brier loss 0.04. Legacy invalidations
without event truth are excluded from numerical scoring. Explicit claim IDs cannot
fall back to a different claim with the same display key. Stored outcomes, prior
scorecards and promotion decisions are not rewritten.

Prompt proposals use resolved development error cases and change only approved
forecast instructions. Rationale-only changes share a mutation identity. Existing,
rejected, rolled-back or expired mutations cannot masquerade as new trials.

The registered forward protocol has a 30-day embargo, evaluation on issue days
30–90, holdout on days 60–90, and a decision at day 90. Only outcomes available by
the decision date enter that comparison. Statistical decisions wait for that date;
validation failures can reject immediately. Insufficient evidence at the deadline
expires the comparison and releases the shadow slot. Previously registered holdouts
are excluded from new development examples.

Comparison identity includes the frozen packet, resolvable event, horizon, target,
provider, model and reasoning effort. Wording is evidence, not target identity.
Only compatible resolved targets enter paired scoring. Uncertainty and sample floors
use disjoint observed outcome windows, at most one issue cohort per UTC day. Unknown
windows cannot supply independent evidence. Display metrics remain distinct from the
weighted promotion metric, with both bases exposed.

After activation, the previous prompt uses the existing budget-limited shadow slot
for 90 days without changing published theses. A new challenger is not drafted
until that monitoring window ends. Performance rollback uses its matched monitoring
cohort; deterministic validation rollback is separate. PostgreSQL serializes
promotion writes, returns the same receipt for retries, and rejects a changed active
revision. Receipts record the database actor and unchanged execution controls.

## Measured scale

Run `uv run --extra test python -m pytest tests/postgres/test_workbench_scale.py
--run-slow -s -q` in a source checkout. The test creates a disposable PostgreSQL
database with 10,000 filled equity paper orders and 1,000,000 confirmed quote facts
across 100 symbols. It verifies $15,000 of fee-adjusted unrealized P&L. This is a
labeled deterministic fixture, not production trading performance.

On macOS 26.6.2, Apple Silicon, September 12, 2026:

| Warm read | Before indexes | With maintained workbench projections |
| --- | ---: | ---: |
| Summary | 7.5701 s | 0.4352 s |
| 100-row page | 5.8277 s | 0.0126 s |
| Trade detail | 0.0327 s | 0.0021 s |

The representative sub-second summary/page target is met in this fixture. The
projection is disposable derived state; the paper order and trade journal remain
authoritative. If a source is enabled after its facts were confirmed, the reader
falls back to the canonical point-in-time selector until the next confirmation
refreshes the projection.

## Live observations

The live database inspected during this release had no paper orders and no advisor
forecast history. Browser checks verified explicit empty paper states, an existing
strategy revision with failed trials, desktop rendering, and a 390-pixel mobile
layout without horizontal overflow or console errors. Financial fixtures remain in
disposable test databases; no demonstration trade was inserted into production.
