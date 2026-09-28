# Storage schema audit and regression fixes — 2026-09-27

## Scope and evidence boundary

PR #45 starts from `555de6b0b14c6d5c84c4283d5f4a6ebf285364b5`, schema
`20260927_0041`. GitHub main matched that handoff. The Mac's database, disk,
launchctl storage drain, NAS and GBrain were not accessible in this session.
Consequently this report does not certify their current health or counters.
The user's current instruction is a PR with code changes, **not** deployment,
physical rewrite, archive deletion or a second maintenance worker.

The old production acceptance (2,528 tests, 84.33% coverage) applies to the old
candidate only. Fresh restricted-role PostgreSQL tests and release gates are
run for this PR; their final SHA, counts and logs belong to the PR verification
comment and Actions artifacts. Independent Codex review results and any correction verification are recorded in the PR discussion.

A clean PostgreSQL 18 catalog at the intermediate candidate exposed **154
managed relations and 2,171 columns, including 209 JSONB columns**. Partition
parents and the default partition are included in that relation count; live
monthly partitions will increase it. This is schema coverage, not a production
size or usage sample. Empty-fixture column statistics say nothing about whether
a production field is used. The final audit also discovers non-system schemas
outside the six managed schemas, including forgotten tables in `public`.

## Implemented findings

| Finding | Correction | Regression proof |
| --- | --- | --- |
| Repeating an identical option capture rewrote its snapshot, quotes, contract metadata and run counters. | Null-safe comparisons guard every assigned field; unchanged conflicts resolve the existing snapshot ID without generating a replacement tuple. | Compare `tableoid`, `ctid` and `xmin` before/after a real app-role retry; keep actual corrections and new observations. |
| Unchanged source registration and fundamental retries also rewrote persisted metadata/JSON. | Update only when the resulting stored values differ. | Identical retry, changed name or filing values, changed filing date, and distinct observation time. |
| An unchanged decision evaluation retry rewrote both its decision and daily checkpoint. | Skip writes when neither the semantic reference nor evaluation time changes; checkpoint bounds still advance for new evaluations. | Exact retry preserves tuple versions; a later evaluation advances time/count but preserves the decision ID. |
| Current option projection performed windowing over historical bundles before selecting the current publication. | A parameterized, stable, security-invoker SQL function places the bundle predicate inside the materialized/windowed projection; current and history views call it through a bounded lateral join. | Eight bundles of twelve candidates made the original query window over **96 rows for 12 current results**; the new path invokes the requested bundle projection only. Existing JSON subset contracts remain intact. |
| New shared evidence references lacked reverse lookup paths for reference checks/garbage collection. | Five narrow non-NULL B-tree indexes, listed below. | Restricted-role catalog/access-path tests and existing archive/retention workflows. |
| Capacity reporting presented a fallback growth estimate as measured and allocated relation files as logical evidence. | Separate observed growth from the provisional forecast; name allocated bytes accurately and disclose backlog-count semantics. | No-sample accounting must return null measured growth, an explicitly labeled fallback, and no invented recovered/reusable bytes. |
| The former storage shortlist could miss small or unmanaged relations and their indexes. | `market-storage audit` inventories all non-system schemas and every table, column, index and constraint visible in the catalog. | A custom schema with unreadable private values is discovered without reading its contents; index lookalikes are distinguished. |

Original-main red tests reproduced the option/source/index/projection failures;
the checkpoint failure was separately reproduced on original main. Later audit
and fundamental scenarios are exercised against the pre-refinement candidate
before their implementation. Final green evidence must identify the exact
post-refinement commit, not merely the workflow's triggering commit.

### Independent-review correction: cross-run fundamentals

The first no-op guard compared ingestion run IDs, so an identical observation
in a new successful run still rewrote its JSON and moved its availability
provenance. The independent Codex review identified this gap. The final guard
compares filing values and date; an identical observation retains its original
completed successful/partial run. Failed, skipped or unfinished provenance is
replaceable by the retry, and real corrections still acquire the new run.
Five additional restricted-role scenarios exercise both usable statuses and
all three recovery statuses, including a later failed retry and real changes.

### Independent-review correction: option snapshot provenance

An identical option replay could likewise attach a previously valid snapshot
to a new running run; a later failure hid it from the current option panel.
Identical headers and quote facts now retain usable completed provenance.
Failed, skipped or unfinished provenance is repaired by the incoming run.
Crucially, a changed or newly inserted quote still attaches the snapshot to
the incoming run even when its header is unchanged: changed facts must not
be presented with the old successful run's earlier availability timestamp.
Nine additional PostgreSQL scenarios exercise the actual current-option
panel query, usable/recovery statuses, failed replays, quote corrections,
provider-payload revisions, new observations and snapshot metadata changes.

### Bounded storage-health reads

The storage-health endpoint previously called full maintenance accounting,
including row-by-row historical evidence sizing under a 15-minute job
statement timeout. Health now requests capacity/forecast accounting without
that scan, with the normal three-second per-statement API budget. Historical
row counts/sizes are explicitly not measured in that response, never zeroed
or mislabeled. The explicit accounting command retains detailed measurements.
A real restricted-role PostgreSQL regression observes executed queries and
read profiles, preserves capacity decisions, and verifies that detailed
maintenance accounting remains available. Filesystem/mount latency is not
bounded by the PostgreSQL statement timeout.

### Index changes and deliberate non-changes

Migration `20260927_0042` adds:

- `app.publication_bundle_item(decision_payload_hash)` where non-NULL;
- `app.current_publication_item(decision_payload_hash)` where non-NULL;
- `app.current_publication_item(content_hash)` where non-NULL;
- `analysis.ticker_decision(market_state_context_hash)` where non-NULL;
- `analysis.ticker_decision(risk_policy_context_hash)` where non-NULL.

The existing `ix_app_publication_bundle_item_content_hash` already supports the
bundle content-hash lookup. A proposed partial copy was caught during review
and removed **before finalization**, rather than adding redundant storage.
No existing production index is dropped by this upgrade.

The raw option indexes are not interchangeable: contract/time history reads,
snapshot/contract reads with included quote values, and generation uniqueness
serve different paths. A prefix resemblance or zero scan count does not prove
redundancy. Likewise, the catalog's many foreign-key access-path suggestions
are advisory, not an instruction to create an index on every immutable lineage
column. Doing that would add storage and ingestion cost without a demonstrated
query or parent-deletion workload.

The audit compares key order, INCLUDE columns, predicates, operator classes,
collations, uniqueness/null treatment, validity and readiness when finding
index equivalents. It separately flags identical layouts with full versus
non-NULL-only predicates. It never drops them. Foreign-key suggestions check
leading B-tree columns, not every operator-family or planner-selectivity detail;
confirm candidates with the actual query plan and workload.

## Table and column disposition

The policy is to retain facts with an identified serving, learning, replay,
execution or audit consumer; use references for immutable shared content; and
retire transport redundancy through the existing verified lifecycle. The schema
is not certified globally minimal by a code-only audit.

| Data family / representative relations | Purpose and storage decision |
| --- | --- |
| `catalog.instrument`, `instrument_alias`, `option_contract` | Shared identity and current contract metadata. Keep natural/unique identity constraints and normalize references. Repeated captures must not rewrite unchanged contract rows. |
| `ingest.source`, `source_lifecycle_history`, `run`, `payload`; `ops.job_run`, `provider_lease` | Availability, retry and operational provenance, distinct from market facts. Keep failure/authority evidence; use existing run/payload retention rather than a competing cleanup process. Source registration no-op writes are removed. |
| `raw.quote`, `price_bar`, their history, confirmation and fact-availability tables | Price facts, corrections and point-in-time availability are different records. Existing writers already distinguish unchanged price facts from revised observations. Preserve correction history and availability; do not replace it with the latest value. |
| `raw.option_snapshot`, `option_capture_generation`, `option_quote` and partitions | Captures and typed options feed decisions, surfaces, verification, replay and outcomes. Retain required observations and pinned contracts, not endless provider envelopes. Existing hot-retention and verified partition archive/drop policies remain authoritative. |
| `raw.fundamental_observation` | Financial observations and revisions feed ranking, valuation and history reads. Exact retries become no-ops. A new observation time or changed filed date/value remains a fact, even when much of its JSON repeats. Larger cross-observation history sharing requires its own exact availability/replay proof. |
| `raw.content_item`, `content_item_instrument`, `disclosure`, `market_event`, `market_event_version`, `market_observation` | News, disclosures, event revisions and macro inputs support changing decisions outside exchange hours. Do not treat a weekend as evidence that these rows are useless. |
| `raw.broker_account_snapshot`, `broker_position_snapshot`, `broker_activity` | Broker provenance and reconciliation are not paper-book outputs. Preserve identity and cash/position lineage. |
| `analysis.ticker_decision`, `decision_context`, `decision_input_payload`, `ticker_decision_checkpoint`, `ticker_outcome` | Immutable evidence is already interned and hydrated through references. Checkpoints represent unchanged evaluations. This PR removes retry-only writes and supplies reverse context lookup paths; it does not delete unresolved outcomes or partially normalized historical evidence. |
| `analysis.decision`, `decision_evidence`, `option_decision`, `option_feature`, `option_relative_value`, surface/gate/reject/outcome tables | Features, rejection reasons, candidate denominators and execution evidence are consumed by selection, calibration and learning. Rejected candidates are not automatically useless; keeping only winners would change the research population. Existing bounded hot/archive lifecycle decides eligibility. |
| `analysis.option_discovery_*`, `option_event_*`, `option_history_*`, `option_recovery_*`, `option_opportunity_observation`, `option_liquidity_sla` | Capture coverage, recovery and prospective event evaluation. Preserve the exact cohorts/session-quality lineage needed to distinguish no opportunity from missing data. Do not collapse these into one success counter. |
| `analysis.run`, strategy revision/manifest/evaluation/comparison/monitoring/forecast/P&L, experiment/trial/evidence/validation tables | Training, strategy selection and experiment reproducibility. Distinct trial assignments and validation outcomes must remain separate even when their inputs share a hash. Prefer shared immutable input content, not removal of result lineage. |
| `analysis.continuous_advisor_*`, `agent_*`, hypothesis/event-packet/state/scenario/source-signal/symbol-decision tables | Prompt versions, forecasts, resolution attempts and outcomes are distinct learning evidence. Existing immutable version and cohort contracts remain; storage reduction must not erase failed or unresolved trials. |
| Allocation, attribution, execution-model, drift and portfolio scenario tables | Risk/execution decisions and their signed evidence. Historical settings cannot be reconstructed from a mutable current setting. |
| `analysis.paper_*_projection`; `app.paper_*`, `portfolio_position`, `portfolio_transaction`, `manual_account_snapshot`, `trade_journal` | Bounded current projections accelerate serving; orders, legs, fills, marks, NAV and cash movements are different facts. Do not remove a serving projection simply because its values are derived, or mistake research marks for funded paper NAV. |
| `app.publication`, `publication_bundle`, `publication_bundle_item`, `publication_payload`, `current_publication_item` | Shared publication content, membership and current pointers already avoid full JSON duplication. Option subsets are projected only when their equivalence is established. The new function fixes historical read amplification without changing publication identity. |
| `app.alert`, `notification_outbox`, decision inbox/truth/sync, settings/policy, watchlist, catalyst and thesis tables | User state, actionable workflow and delivery idempotency. Preserve pending delivery and exact thesis/review history; small current-state tables are not the main growth problem. |
| `ops.storage_archive_*`, `option_quote_partition_policy`, `storage_daily_accounting` | Verified archive coverage, eligibility and durable cursors. Never remove archive references ahead of the protected data or start a duplicate drain. Accounting samples stay small; physical bytes and semantic evidence counts have separate meanings. |

### Fields that look redundant but have active consumers

`raw.option_quote.provider_payload['instrument']` is read by option verification
in `infrastructure/postgres/options_decision_system.py`. The historical capture
path in `core/robinhood_options/history.py` also keeps original instrument,
quote, underlying and status information for replay. Replacing every provider
payload with `{}` at ingestion would break those consumers.

Quote-level style, settlement, deliverable verification and provider status
are point-in-time evidence; the current `catalog.option_contract` is mutable.
They cannot all be replaced by a join to today's metadata without changing
historical interpretation. `observed_at`, `available_at`, provider timestamps
and capture timestamps have different information-availability meanings.
Ticker `learning_history` is used by decision construction, persistence and
API/read-model payloads, not a dead field established by this review.

Current `HotRetention` defaults are **30 days** for ordinary option hot data,
**730 days** for typed `history_full`, and **365 days** for typed `event_strip`.
Provider-envelope trimming is a separate eligibility path, with protection and
archive checks. Older acceptance text mentioning a seven-day window is not the
current default. Preserve last valid captures, current publications, pins and
pending outcomes even when old enough to pass an age-only filter.

### Legacy and apparently unreferenced relations

- `analysis.ticker_input_manifest_legacy`: the duplicate writer was retired in
  earlier work. `manifest_archive.py` owns verified archive/cutover/drop. Its
  continued existence on a fresh migration does not mean a writer restarted.
- `app.publication_item`: still reads legacy publications without bundles.
  Removing it requires proof that all such rows have been migrated/archived.
- `analysis.symbol_decision`: only its baseline table declaration was found; the
  active `analysis.symbol_decision_outcome` references canonical `analysis.decision`,
  not this legacy extension. Retire only after checking live legacy data and
  archive coverage, not merely because the current source has no writer.
- `app.research_report`: no active application producer/consumer was found in
  the code search; it is a retirement candidate. That does **not** prove the
  live table is empty or that any legacy rows can be destroyed. With no active
  writer it is not an identified continuing growth source.
- Signing-secret tables and `raw.option_quote_default` have SQL-function or
  partition-parent consumers. Lack of a direct Python reference is not a valid
  deletion test. Secrets are never sampled by the inventory command.

No blanket column/table drops are included. Legacy inline evidence columns are
still needed while the handoff's normalization backlog may be nonzero. A future
drop must prove zero applicable rows, no pending consumer and restoration
coverage; a hash match alone is not enough to discard IDs or time semantics.

## Repeatable inventory and capacity measurements

Run using the normal configured application role after reviewing this PR:

```sh
uv run market-storage audit --config config.yaml > storage-audit.json
```

This command is catalog-only and read-only. `--execute` is rejected. Queries
have 30-second statement and two-second lock timeouts. It reports missing
statistics and unreadable tables explicitly rather than interpreting them as
zero rows, and discovers unmanaged schemas. It does not read table contents,
column default expressions, common-value arrays, histograms or signing keys.

Partition parents contribute zero allocated bytes; physical children are
counted once. TOAST bytes include TOAST indexes; main-table indexes and auxiliary
forks are separately reported. Tuple counts/widths/distinctness are estimates.
Allocated bytes include reusable pages: neither these totals nor dead-tuple
counts measure filesystem recovery or the compacted size of a rewrite.

`account()` now uses `tracked_evidence_allocated_bytes` for the historical
large-evidence shortlist and reserves `measured_growth_bytes_per_day` for the
observed multi-day estimate. The provisional 0.7 GiB/day forecast is exposed
separately, not asserted as this installation's measured growth. The existing
stored column `logical_evidence_bytes` is retained for compatibility and
explicitly described as a legacy name, avoiding a duplicate replacement column.
The existing backlog sample counts old local rows, **not archive-eligible rows**.

## Remaining scalability and production work

The new indexes prevent avoidable reverse-reference searches, but do not make
all garbage collection constant-cost. The existing archived-decision payload
collector still expands JSON references across live/archived evidence. Shared
reference materialization or a reverse-reference ledger is a possible further
normalization, but its correctness and storage benefit have not been proven in
this session. It is not silently substituted into a live drain.

Minimum cross-store replication, sustained daily growth, archive throughput and
a steady-state capacity bound still require production measurements. Identical
JSON component sharing is not by itself proof of those properties. Do not call
the overall storage lifecycle complete on the strength of synthetic fixtures.

For the separate Mac session:

1. Read the existing launchctl runner/log and live API identity; do not create a
   second worker. Refresh the verified backup when its actual freshness expires.
   Preserve the tested five-decision / ten-scan limits and ten-second idle rate.
2. Record exact eligible counts at phase boundaries; the historical 22,484
   decision backlog, zero ranking backlogs and about 21 GiB free are not fresh
   measurements. Collect catalog samples and enough daily observations to
   distinguish normal writes, archive drain, retained WAL and unrelated disk use.
3. Before migration, measure index headroom and use a quiet writer window. This
   migration builds five normal indexes transactionally, with two-second lock
   acquisition and 60-second statement limits; a failure rolls back. It does not
   run a rewrite or change retention eligibility. Upgrade/downgrade tests preserve
   existing view types and app-role read permissions.
4. Before a separate physical rewrite, measure one candidate's heap, TOAST,
   indexes, reusable space and compacted size plus peak table/index/WAL/temp
   allocations. Verify exact IDs/JSON/outcome reads and runtime/browser identity
   after maintenance. Report PostgreSQL-reusable bytes separately from actual
   filesystem bytes reclaimed.

**Production filesystem bytes recovered by this PR session: zero (no physical
maintenance performed). PostgreSQL reusable bytes and final eligible backlog
counts: not measured.** Deployment smoke, live browser checks, capacity proof,
remaining drain phases and the durable GBrain update remain local follow-up
work. The repository report and PR preserve this boundary instead of claiming
those steps were completed remotely.
