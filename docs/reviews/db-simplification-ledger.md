# Database simplification ledger

Goal: preserve the personal-project simplicity preference; fix all eight review
findings; use a correct schema, efficient reads, and no needless fact copies.
Scope: PostgreSQL and paper-only controls; preserve account records and provenance.
Base: `5841a51`; worktree: `market-db-simplify`; target: `main`.

## Verified implementation

- Preference saved to GBrain and the user-requested local memory note.
- F1: application read/write grants repaired; direct read models checked as app role.
- F2: one transactional baseline adoption, exclusive runner lock, bounded timeouts,
  rollback/retry checks. No historical concurrent-index interruption path remains.
- F3/F4: latest eligible option capture selected before reading current quotes;
  empty captures remain empty; one quote per contract; metadata avoids history probes.
- F5: symbol filters apply before event limits.
- F6: native bounded keyset pages replace copied JSON snapshots and the 50k cap.
- F7: bounded transition page and indexed predecessor lookup; two supporting indexes.
- F8: percent-encoded connection strings survive Alembic configuration.
- Baseline: 131 old revisions archived in Git; exact catalog, ACL, role, owner,
  and sequence checks protect adoption. Fresh schema checked before secrets are loaded.
- Duplication: remove two indexes covered by unique constraints and transient review
  copies; deterministic content-addressed provider archives reuse identical bytes.
  Existing historical evidence remains intact.
- Independent review repairs: unsafe grants/owners, separate NOLOGIN group support,
  empty latest captures, legacy history probes, unsafe defaults, sequence drift, and disabled foreign-key triggers.

## Verification and release

- Final integrated check: 679 passed (database, options, architecture/runtime guards).
- Security/foundation checks: 36 passed; final baseline checks: 22 passed, including
  disabled partition foreign keys; latest option query checks: 8 passed.
- Ruff passed; frontend production build passed.
- Full database backup verified on NAS (2,976,025,914 bytes).
- Final integrated tests: passed. Independent review: clean (final focused pass); all accepted findings repaired.
- Final architecture/runtime guards: 26 passed; built-wheel runtime imports passed.
- Maintenance adoption: passed on live database, revision `20260906_0001`;
  exact baseline contract matched, zero invalid indexes.
- Preservation: all 10 account/research/signing-key table fingerprints unchanged.
- Live plans: transition page 233 ms (50 rows), exact count 125 ms; chain 48 ms
  (50 rows), volatility 4 ms (3 rows); zero temporary disk spill in all four.
- Canonical API restart, browser proof and landing: next release step.

Known limits: pages show current mutable values; old archive copies remain as
provenance; historical migration code is retained in Git. Generated baseline keeps
four whitespace-only lines inside an unchanged historical SQL function body.
