# PR #35 verification receipt

Date: September 19, 2026.

Engineering code reviewed and submitted for integrated verification:

- Commit: `d83d3bffe58b28890212387c8c306e7db59bc1a7`
- Tree: `2801c94ff0b4c86c561ac1a2dcc1c81412acf809`
- Schema head: `20260919_0025`

This receipt is a documentation-only addition after that code commit. The implementation and local acceptance instructions are in [the complete handoff](trader-workstation-implementation-handoff.md).

## Results established before handoff

| Check | Observed result | Scope / limitation |
|---|---|---|
| Focused backend regressions | 212 passed on Python 3.11 | Quote clocks/numerics, management fairness, collection permission, market decoding/readiness, strategy parameters, and read-only verification; these local tests do not require a running PostgreSQL server |
| Frontend Vitest | 43 files, 162 tests passed | Component/presentation regressions, not an interactive browser test against production data |
| Frontend TypeScript | Passed | Whole-project type check |
| Production frontend build | Passed | Existing bundle-size warning remains informational |
| Architecture/runtime-boundary guards | 31 passed | Existing repository guard suites |
| Generated contracts and whitespace | Passed | Panel/OpenAPI checks and `git diff --check` |
| Current integrated backend/PostgreSQL regression step | Passed | GitHub Actions with locked dependencies, Python 3.11 and PostgreSQL 18; the current run is linked below |
| Full `make release-gate` on the final code tree | Still running when this receipt was written; no passing result asserted | Includes the full slow backend suite and the existing 80% coverage requirement; check the final workflow result or rerun locally |

Current integrated run: [workstation-release-review](https://github.com/joe7hu/market/actions/runs/35461794568).

The workflow checks the exact source tree, applies the reviewed fixture correction, removes its temporary transfer workflow, commits the resulting tree, and runs verification against that resulting checkout. GitHub displays the triggering wrapper commit on the run; the tested implementation tree is the one identified above. A successful transfer step alone is not a test result.

## Failure found and corrected during verification

The first PostgreSQL run failed before reaching the collector-fairness query because its newly added `paper_order_leg` test fixture omitted required frozen bid/ask, displayed sizes, and quote time. The fixture now supplies those fields. The corrected integrated PostgreSQL regression step passes. No production constraint, freshness limit, qualification threshold, or risk gate was relaxed to fix the test.

Other continuation-review fixes are listed in the implementation handoff, including budget-limited batch starvation, rotation of failed quote-collection attempts, economic-versus-availability clocks, missing values rendered as zero, false neutral valuation, NAV interpolation across missing hours, signed comparison bars, stale input cutoffs, and superseded research requests.

## What is not established by these results

There has been no deployment, production database migration, real provider login, live-market fill verification, private-account reconciliation, or interactive browser QA in the user's local environment. No empirical positive trading edge is claimed. A green engineering test is not a forecast of profit.

The PR remains an implementation candidate for the local session. Do not treat the running full release gate as green, omit failed tests, lower the coverage threshold, or relax execution safeguards to make a demonstration trade appear.

## Local acceptance sequence

1. Check out the PR in the normal repository. Preserve local changes and review the full implementation handoff.
2. Stop the existing API/scheduler before applying the new schema. Install locked dependencies, run the migration with the normal authorized migration configuration, rebuild the frontend, and restart the API/scheduler using the normal application login. Confirm schema `20260919_0025` and matching frontend/API build identities.
3. Run the complete release gate in the supported Python 3.11/PostgreSQL 18 environment. Inspect the current CI run first; carry forward any unresolved failure rather than assuming this receipt supersedes it.
4. Run `scripts/verify_workstation.py` as described in the handoff. It makes six bounded GET-only requests and does not fund accounts, collect data, promote strategies, or stage trades. Review expected disabled/collecting states before using `--strict`.
5. Verify all reviewed screens on desktop and a narrow viewport using real data. Confirm missing required inputs have a specific recovery path and optional gaps do not erase usable baseline evidence.
6. During an eligible market session, trace one genuinely qualified immutable decision through a later admissible quote, entry, fees, marks, exit, journal reconciliation, and independent outcome evidence. A non-marketable limit may remain unfilled. Record actual transition IDs/timestamps privately.
7. Verify kill-switch behavior, slow/failed research isolation, failed order/collector fairness, account reservations, prospective NAV gaps, and collection enabled with automatic promotion disabled.
8. Report actual strategy evidence as pass/fail/inconclusive. Do not manufacture history or fills, mix research observations into funded NAV, or present sample counts as proof of edge.

Keep any screenshots, raw diagnostics, holdings, credentials, and operational reports local unless explicitly redacted and approved for sharing.
