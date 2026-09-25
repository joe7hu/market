# Runtime recovery — September 25, 2026

Base: `d781f008`. Production findings supplied by the operator are observations,
not reproduced production proof. This branch must not lower evidence thresholds,
invent fills, reset publication history, or bypass the 30 GiB release reserve.

## Failure cases to reproduce before implementation

1. A background outcome task and a long, nominally priority market-data task
   occupy both slots; the paper manager cannot run. Cancellation of a waiter
   must not leak slots or leave queue health stale. Total concurrency stays two.
2. A publication payload larger than the ordinary 8 MiB pack prevents retention
   forever. A separately bounded, lossless, typed restore path must verify on
   disk before deletion. A corrupt archive, missing NAS, full disk, failed
   scratch restore, or over-budget row must leave the source intact. Normal
   row packs and idempotent content-addressed retries must continue to work.
3. Incomplete 252-session horizons block publication of otherwise valid
   shorter-horizon evidence. An immature observation is not a failed producer,
   and it is not an eligible resolved sample. Publication must retain lineage
   and execution-proof validation rather than force every outcome to resolved.
4. Re-discovering an entire option chain omits already-owned contracts. Native
   provider identity, deliverable identity, pagination, omitted replies, and
   post-persistence coverage must be traceable per requested contract. Never
   substitute an adjusted contract or a quote from another session.
5. Completed outcomes consume the finite refresh batch while mature unchecked
   outcomes wait. Select due unresolved work fairly, with a bounded retry
   cadence, without creating future observations or look-ahead evidence.
6. Research loading must not claim an empty authority during a pending fetch.
   Draft dossiers and insufficient out-of-sample evidence remain explicit;
   neither a fake candidate nor a forced stock-alpha promotion is a repair.

## Deployment acceptance (not yet established)

Verify a usable NAS archive and backup; recover disk reserve without VACUUM FULL
or unverified deletion. Observe queue waits and heartbeats through a long outcome
run. During a regular session, reconcile all active contract IDs against complete
persisted quotes and inspect every exception. Verify real paper entries, marks,
exits and annotated P&L, then reconcile payoff/settlement/attribution into research.
Check Arco after its next four-hour due time. Run E2E, guards, check, release-gate,
release-smoke, matching API/frontend/scheduler release IDs, and browser evidence.
Insufficient statistical history is a research state, not a reason to bypass
qualification or a claim that production is fully recovered.

## Repairs in this change

The scheduler reserves capacity for paper management, inbox synchronization and
bounded active-contract quotes, not for long market-data/decision jobs. The total
budget remains two. Runtime queue diagnostics identify the job, lane, wait reason,
wait duration and due time; cancellation releases capacity and removes waiters.

Oversized rows use a separate content-addressed PostgreSQL COPY envelope on the
archive filesystem. The original PostgreSQL JSON text and schema are preserved;
checksums, read-back and a typed scratch restore must succeed before the caller
may delete a source row. The ordinary 8 MiB pack limit remains unchanged. The
large-row path is bounded by the existing 64 MiB COPY budget; larger rows fail
closed. This is not a claim that PostgreSQL files shrink after a row DELETE.

Active paper contracts use the persisted Robinhood instrument ID before chain
rediscovery. Missing native IDs use bounded paginated discovery with exact
contract terms. Omissions, duplicate identities, repeated cursors, missing provider
timestamps, wrong deliverables, prior-session quotes and persistence mismatches
remain per-contract failures. A provider request/row count alone is not coverage.
The regular-session guard and provider lease limit are unchanged.

Outcome refresh reads compact immutable decisions, skips fully resolved work,
prioritizes overdue observations and reuses a decision's confirmed price path
across its six horizons. A 120-second soft budget yields between complete decision
sets; it does not interrupt a transaction or declare an immature horizon resolved.
The canonical publisher validates each entire six-unit TradePlan set independently.
An incomplete/invalid independent plan cannot suppress valid plans or leak five
partial units. Such publication reports partial coverage and exclusions. Duplicate
stable-unit authority is still a global fail-closed condition. Existing exact
lineage, fill, exit, timing and promotion validators remain in force.

Postmortem requests now include identity-bound original thesis, recorded revision,
outcome/attribution, paper fills, paid-fee reconciliation and recorded settlement
state. Missing evidence is explicit, not zero P&L. Legacy reviews are preserved;
new evidence fingerprints can open a new advisory review. Timestamp-only refreshes
cannot manufacture new reviews or candidates. Unchanged model proposals still do
not create a challenger. Research Authority renders pending/failed/ready-empty
states distinctly and preserves existing rows during refresh.

## Repeatable fixture verification

Use the locked repository toolchain and PostgreSQL 18, then run:

```sh
uv run --extra test python -m pytest -q \
  tests/test_runtime_recovery.py tests/test_robinhood_options.py \
  tests/postgres/test_hot_storage_lifecycle.py \
  tests/postgres/test_outcome_publication_recovery.py \
  tests/postgres/test_postmortem_evidence_recovery.py \
  tests/postgres/test_ticker_paper_execution.py \
  tests/postgres/test_phase2_stock_alpha.py \
  tests/application_api/test_release_smoke.py \
  tests/postgres/test_workstation_completion.py
make guards
make check
make release-gate
```

The PostgreSQL tests use disposable fixture databases and a temporary archive,
not production. The publication-isolation regression stubs the authority/evidence
validators to exercise publisher grouping; the existing domain and stock-alpha
suites independently exercise those unchanged validators. The workstation fixture
exercises collection, persisted quotes, paper entry/mark and ledger history.

A passing fixture/CI run is not deployment acceptance. Before deployment/backfill,
the operator must still establish the 30 GiB local reserve. During a regular
session verify all selected active contracts through `contract_diagnostics` and
`contracts_captured`, including the six previously stale IDs. Observe the next
Arco cadence, scheduler timing, real paper-ledger cash flows, and browser P&L.
Re-run `make release-smoke` on the deployed matching release. More independent
out-of-sample cohorts may still be required; this change does not assert that
Qualified Stock Alpha passes or that a profitable trade must always exist.
