# System health audit, 28 September 2026

The audit traced the health page through its producer jobs, PostgreSQL writes,
publication reads, API, and browser. Health labels alone were not acceptance.
All execution remains paper only.

## Root causes and repairs

| Failure | Evidence | Repair |
| --- | --- | --- |
| Active-contract quotes age out | 152 active contracts across 44 symbols were served by a 20-symbol batch every minute. A full rotation exceeded the 120-second execution limit. | Refresh every owned native provider ID in bounded quote batches. Keep the 20-symbol cap for chain discovery and the existing collector deadline. Mixed batches use the owner's least-recently attempted contract to choose which phase goes first. |
| Selecting active contracts times out | The correlated maximum read each active contract's full quote history. Live worker traces identify this query as the timeout site. | Use the existing descending contract/time index to fetch the latest eligible row. Keep source, snapshot completeness, availability, and observation-time filters. The revised query took 173 ms on the live data. |
| Quote and current mark writes fail | Live ingestion failures include duplicate primary keys and deadlocks in both paper mark projections. Concurrent transactions deleted and inserted the same projection rows. | Reuse the existing option snapshot writer lock. Acquire it before fact, ingestion run, and capture recovery row locks. Both mark functions use a new clock after the wait. Keep the existing confirmed-fact selection rules. |
| Recovery strategy binding fails | Revision 1 already has immutable `unavailable/1` bindings. The producer tried to assign `options_recovery/2` to those same revisions. | Create revision 2. Preserve old bindings, parameters, and history. |
| Hot option retention times out | A bounded 500-quote lookup repeatedly scanned about 1.16 million option decisions. Its estimated plan cost was about 2.5 billion. | Add the exact `(snapshot_id, quote_observed_at, contract_id)` access index. Preserve archive and evidence pin rules. |
| Trading signals time out | Reads called `option_bundle_projection` across historical option bundles for unrelated models or one unrelated bundle. | Expose the two option model names before the opaque function in the shared current and historical views. Expose the base bundle ID in the option projection view so bundle filters apply before the function. Preserve all payloads. |
| Quote writes fail during normal serialized work | A live quote capture failed with the inherited 2-second lock timeout while other writer work ran. The concurrency regression reproduces this with a valid 2.25-second writer. | Scheduled jobs use a bounded 10-second lock wait. Interactive reads retain their original timeout; retention keeps its maintenance profile. Deadlock and statement limits remain active. |

## Checks

Regression checks reproduce missing-row mark races, a mark refresh waiting for
newly committed prices, run and capture lock order, native quote coverage beyond
20 symbols, immutable recovery bindings, and unrelated publication reads that
must never call the option projection. The wider storage checks also exercise
archive pins, publication payload equivalence, and migration downgrade.

Independent review found additional lock-order faults. Each accepted finding was
reproduced and repaired before the final candidate was tested.

The final follow-up passed 112 focused tests, `make guards`, `make check`, and
`make release-gate`: 2,581 tests and 84.42% coverage. The final independent review
reported no actionable findings.

Raw audit evidence is in `/tmp/market-health-audit-20260928`. Deployment uses a
verified NAS backup receipt and a fresh schema-only backup. This migration adds
an index, changes function/view definitions, and adds writer guards; it does not
delete evidence or rewrite partitions.

## Valid constraints

`no_qualified_candidate` and insufficient repeated control observations are
valid research results. They do not authorize a candidate or a strategy claim.
Legacy recovery cohorts keep their original poor coverage. A provider strip with
no executable contracts stays non-executable. Full-chain history remains subject
to the storage forecast and the configured 15 GiB reserve. Historical agent
failure totals are not evidence that the current worker is failing.

## Deployment and live proof

Run `make guards`, `make check`, and `make release-gate` on the final candidate.
Stop managed writers before the explicit schema upgrade, then rebuild and restart
the managed API, scheduler, and frontend. Run `make release-smoke` and inspect the
health page in the browser. Verify a new quote capture against the active contract
set, a completed decision publication, new immutable recovery bindings, and a
bounded retention batch with an advancing checkpoint. Record the live release
identity and any remaining valid constraints with the audit evidence.
