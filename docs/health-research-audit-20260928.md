# System health audit — 2026-09-28

Candidate base: `680dcd9`. Live baseline captured at 16:56 UTC from
`/api/workstation/status` and `/api/panel-snapshot?scope=health`.

## Failure cases and evidence

- Social refresh finishes, waits its full 1,800-second cadence, then queues.
  Its freshness limit is also 1,800 seconds. A successful collector therefore
  becomes stale before its next run can finish. Observed successful starts:
  09:30, 10:04, 10:43, 11:17, 11:33, 11:54, 12:21, 12:54 ET.
  The current curated X source was healthy after the 12:54 run; earlier yellow
  intervals were real freshness gaps, not failed imports or archived X handles.
- Candidate research found zero `options-radar-core` challenger revisions with
  an active parent. PostgreSQL held one active baseline and 51 superseded
  revisions. A retry cannot produce a qualified challenger. No candidate,
  parameter change, promotion, or paper order may be fabricated to clear this.
  All 48 prior mutation proposals report `implementation_version_mismatch`:
  their preserved revisions are bound to `unavailable@1`, while active revision
  1830 uses `options_radar@option-professional-v3-ticket`. They remain excluded,
  rather than being rebound to code they never evaluated. The agent is enabled,
  but its automatic cadence is zero and no agent job is scheduled; this repair
  does not invent new parameter proposals or change the agent run policy.
- Historical validation loaded 28 independent, resolved 20-session observations:
  21 decisions from August 23, one from August 25, six from August 26.
  Their outcomes became available September 22–25. Both control generators
  produced zero out-of-sample predictions. Training labels did not exist at
  those test decision clocks. More overlapping observations from those dates
  cannot cure this; later test decisions must mature after training evidence
  became available. Historical prices cannot establish past feature availability.
- Outcome resolution processed six decisions / 36 horizon marks / 12 resolved
  horizons in the observed pass. Canonical attribution was blocked because all
  53,824 plan-bearing decisions were ineligible (812 legacy decisions had no
  plan). The most recent 100 plans all reported
  `alpha_strategy_revision_missing`. This is downstream of stock qualification,
  not an independent collector failure. Counterfactual stock outcomes remain
  available for research without pretending that a tradable plan existed.
- A skipped or partial producer's precise cause was hidden in a hover title or
  omitted from its summary. Failed, partial, skipped, and overdue stages must
  show their recorded cause without changing their state or success clock.

## Repair

Social collection now defaults to 900 seconds, leaving 15 minutes of its
unchanged 30-minute freshness budget for scheduler waits and collection.
Explicit cadence overrides remain supported; a delay beyond the evidence
budget must still show stale. Research jobs report their measured sample and
candidate counts, and the attribution publisher retains its actual plan
blockers. Health rows show those producer details. All qualification,
point-in-time, execution, and promotion gates remain in force.

## Repeatable acceptance

Run the new PostgreSQL regression cases for blocked attribution, overlapping
stock outcomes, and no eligible challenger. Run the scheduler, job policy,
workstation readiness, and runtime recovery checks; then `make guards`,
`make check`, and `make release-gate`. Run structured autoreview on the frozen
candidate. After landing and managed restart, check matching release identity,
new social cadence, current source health, the three job summaries, and the
rendered `/health` rows. Preserve skips and partial attribution when evidence
remains insufficient. A green source or HTTP response is not qualification.

## Verification receipt

- 100 focused scheduler, readiness, runtime, and PostgreSQL checks passed.
  An additional 27 stock-alpha/runtime checks passed.
- `make guards`, `make check`, and the final `make release-gate` passed:
  2,587 backend tests; 84.42% API/PostgreSQL coverage; 235 frontend checks;
  TypeScript, generated contracts, production build, and whitespace checks.
- The first full run found one stale exact-dictionary assertion for the added
  diagnostic field. It was updated; the full final run passed.
- Structured Codex review, via `autoreview --mode local --engine codex`, reported
  no actionable findings. Raw final receipts:
  `/tmp/market-health-release-final.log`, `/tmp/market-health-review-final.json`.
