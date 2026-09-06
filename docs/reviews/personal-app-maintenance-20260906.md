# Personal app maintenance — 2026-09-06

Implemented the approved plan for faster iteration within the existing folders.
Validated runtime commit: `1f04f7707c7b523a2396d4306fda2089aac88843`.
Base: `68ab929f98dd5e12d44b5dcd8045be66145a7485`.
The implementation changes 64 files: 1,111 added lines, 3,421 removed lines
(2,310 net lines removed).

## Result

- Frontend scope responses merge once into current state. Concurrent refreshes
  preserve newer data and keep loading active until all requests finish.
- Today queue composition lives in `app/actions/today.py`; shared rank identity
  rules live in the existing decision module.
- Portfolio, thesis, and options routes use their concrete owners. Removed
  forwarding methods, duplicate dependency wiring, and signature inspection.
- Job enqueue logic has one owner. Disclosure configuration is parsed once per
  run. The existing full-refresh CLI points directly to its implementation.
- Removed unused frontend pages and their private helpers. Existing redirects
  and active feature pages remain available.
- Updated architecture guidance and kept the fast TypeScript check separate
  from the production bundle build.

No dependencies or schema changes. PostgreSQL authority, paper execution,
validation, and release safeguards remain in place.

## Validation and review

- Final `make release-gate`: **1,531 passed**, coverage **80.60%**;
  488.60 seconds wall time, including checks and build.
- Frontend suite: **102 passed** across 33 files.
- Static checks, API contract check, and extracted wheel imports passed,
  including all 24 CLI entry points.
- Independent review: **REVIEW_PASS** after fixing the configured options
  health mode and binding the ticket fallback to `RecoveryReadRepository`.
- The first full gate exposed the ticket fallback defect. The final full gate
  above includes its fix and the existing integration regression.

Evidence logs are in `/tmp/market-maintenance-evidence/`, including
`release-gate-final.log`, `frontend-tests.log`, and `api-main-proof.json`.
These temporary logs are local evidence, not permanent storage.

## Live proof

Rebuilt main and restarted the existing launchd services on ports 8000 and
5173. Both processes use `/Users/joehu/proj/market`. Backend, frontend, and
scheduler release labels match the validated runtime commit. The production
asset is `index-BXywlTm9.js`.

Eleven read-only API probes passed, covering Today, status, portfolio,
opportunities, sources, option history, inbox, settings, and ticker detail.
The recovery ticket fallback returned HTTP 200 with ticket version 4.
The candidate also passed these checks with the restricted app database login.

Browser checks covered 14 active views and seven legacy redirects on the
candidate. After restart, Today loaded on port 8000; Portfolio and Opportunities
loaded on port 5173 with no loading state left behind or horizontal overflow.

GitHub CI remains manually disabled as before. This release uses the local
gate and independent review recorded above; no remote CI success is claimed.
