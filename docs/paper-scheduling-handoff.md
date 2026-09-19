# Paper scheduling implementation handoff

This implements the scheduling/collection part of the trader-workstation audit.
It does **not** declare all four audit phases complete. Market-data publication
repair, dual-clock quote validation, order-batch fairness, wider UI redesign, and
strategy-edge validation remain separate work.

## Implemented

- The frequent `process_options_paper_orders` job manages the funded book and
  advances bounded existing shadow observations. It no longer invokes candidate
  resolution or Radar recomputation.
- Candidate research has a separate `run_option_paper_experiments` job, a default
  300-second interval, and a 900-second subprocess timeout. Its result remains
  paper-only. Existing entry, risk, and promotion gates remain in force.
- `strategy_experiment_collection_enabled` independently authorizes candidate
  collection. It defaults to false and requires a literal boolean true. The
  checked-in config explicitly enables it to preserve collection that was
  previously enabled through the auto-promotion switch. Switching auto-promotion
  off no longer prevents separately authorized candidate collection.
- The scheduler retains total capacity two and allows at most one slow job at a
  time. Slow work waits for its own slot before taking total capacity, leaving
  room for a due fast paper/inbox tick. Cancellation releases both permits.
- Existing paper positions are managed before new orders are staged. Management
  failure prevents new staging. Staging failure is reported as a partial result
  without hiding the already completed management results.

New orders are first managed on a subsequent tick, rather than being staged and
managed in the same invocation. Subsequent quote and immutable-ticket validation
remain authoritative; this change does not invent immediate fills.

## Local verification

Restart the scheduler after changing collection configuration or its cadence.
`MARKET_PAPER_EXPERIMENT_REFRESH_SECONDS=0` disables scheduled candidate research;
it does not disable the fast manager or lifecycle handling of existing shadows.

Run the focused tests, then the normal project gates:

```sh
uv run --extra test python -m pytest \
  tests/test_paper_tick_ordering.py \
  tests/test_paper_execution_scheduling.py \
  tests/test_paper_signal_config.py \
  tests/test_scheduler.py \
  tests/options/paper_execution/test_options_paper_execution.py \
  tests/options/paper_execution/test_options_experiments.py -q
make guards
make check
make release-gate
```

In the real database, verify the two jobs have separate run records and runtimes;
check that a deliberately slow research run does not hold both scheduler slots;
check existing positions continue to be managed with entry switches off; and
check a later admissible quote can progress an eligible staged order. Keep
research observations separate from funded-account P&L. A valid limit that is
never marketable must remain unfilled.

## Review-environment results

- 23 focused tick, collection-policy, concurrency, and config tests passed.
- A broader 99-test scheduling and paper-owner set passed with the unavailable
  PostgreSQL settings read explicitly stubbed to an empty override. One database
  integration case was excluded. This is not a live-data or integration result.
- The broader architecture/runtime guard attempt did not finish within the local
  execution budget; no passing guard or release-gate result is claimed.
- PostgreSQL integration, locked-runtime CI, and deployed verification remain
  required. The review host uses Python 3.13 with available dependencies and
  pure-Python dependency fallbacks, not the production Python 3.11 runtime.

The ZIP review bundles are not application releases and are not needed to apply
this PR. Use the repository checkout and its ordinary locked dependency setup.
