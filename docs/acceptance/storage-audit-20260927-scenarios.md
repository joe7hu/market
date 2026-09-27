# Storage audit regression scenarios (2026-09-27)

Base: `555de6b0b14c6d5c84c4283d5f4a6ebf285364b5`, schema `20260927_0041`.
This session changes code through a PR; it does not deploy, stop the existing
storage drain, prune production evidence, or perform physical rewrites.

Write regression tests before changing each affected implementation. Prefer
real PostgreSQL workflows using the restricted application role. These are
failure cases to investigate, not claims that every case is currently broken.

## Data and write amplification

- Repeating an identical capture or normalization must not insert duplicate
  evidence or rewrite an already-normalized large value.
- Changes to prices, information availability, source revision, exact decimal
  values, decisions, plans, or outcome lineage must not be mistaken for retries.
- Every removed transport field must have no serving, learning, replay, audit,
  or pending-outcome consumer; lack of a simple text-search hit is insufficient.
- Current publications and last valid captures survive weekends and provider
  delays. Unchanged evaluation checkpoints must remain distinct from new facts.
- Shared evidence cannot be collected while any consumer still references it.

## Maintenance and query cost

- A bounded mutation batch must not repeatedly normalize already-finished rows.
- The current option publication must not materialize projections for every
  historical bundle before filtering to the requested publication.
- New foreign-key/reference paths must have a usable, appropriately sized
  access path; an index's zero scan counter alone is not proof it is redundant.
- Duplicate-index identification must compare key order, included columns,
  operator classes, collation, predicate, uniqueness, validity, and constraints.
- Parent/partition, heap/TOAST, and index bytes must not be double-counted.
- Catalog estimates, observed counters, reusable bytes, and actual filesystem
  recovery must be separately labelled. A single sample proves no growth rate.

## Safety and acceptance

- Inventory covers every application table, column, index, and constraint,
  including tables not in the historical large-relation shortlist.
- Inspection is read-only, bounded by timeouts, and must not fetch provider
  payloads, signing secrets, credentials, or financial content into reports.
- Restricted-role visibility or missing statistics is reported as incomplete,
  never as zero storage or as permission to delete.
- Run affected PostgreSQL E2E tests, guards, check, and release gate on the
  changed candidate. Prior main's 2,528-test result is not new-candidate evidence.
- Production backlog counts, growth, space recovery, runtime identity, smoke,
  and browser checks remain unverified until measured on the Mac host.
