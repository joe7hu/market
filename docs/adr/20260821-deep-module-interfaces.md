# ADR: Deep Module Interfaces

- Status: accepted
- Date: 2026-08-21
- Scope: Market application, HTTP, options, provider, and frontend seams

## Context

Market is PostgreSQL-first and behaviorally healthy, but its navigation cost is
high. A broad application facade, table-only HTTP aliases, re-export adapters,
private cross-module imports, and a large frontend transport module spread
ownership across many shallow modules.

The architecture cleanup uses the deletion test: delete a suspected shallow
module and keep the change only when complexity becomes local instead of
moving to many callers. File length is inventory data, not an architecture
rule. A deep module may be longer than 700 lines; a short pass-through module
still fails the deletion test.

## Decisions

1. PostgreSQL remains the only runtime authority.
2. `/api/panel-snapshot` is the canonical Read Model interface.
3. Domain detail and mutation routes remain separate.
4. Compatibility routes and import facades are not retained.
5. A package interface must use explicit exports.
6. Internal implementation modules must not become public seams.
7. Existing dirty `main` work is preserved. The architecture branch starts at
   `17cd4c1` and does not change the six dirty files until their owner commits
   them and the architecture branch is rebased onto that commit.

## Baseline at `17cd4c1`

The supplied architecture review recorded these baseline measures:

- 79,015 non-generated production source lines.
- 293 production Python modules.
- 162 OpenAPI paths and 169 operations.
- 75 HTTP handlers that only forward to the generic table reader.
- One local Python import cycle across Event Scout, Event Scout runtime, and
  replay fixtures.
- 19 Python modules in the 650–700 line range; five are at 690–700 lines.
- 80 production Python modules under 60 lines.
- `app.deps` exposes about 100 names and connects unrelated owners.
- Production code imports private names across module seams.
- `frontend/src/api.ts` owns transport, domain types, and about 46 requests.

The Phase 0 inventory reports the current measurable shape in fewer than 200
lines. It records subsystem line counts, route categories, owner exports,
cycles, private imports, router/database violations, re-export-only modules,
console entry points, generated-contract presence, and compatibility markers.

## Guard policy

Architecture guards fail on new import cycles, new private cross-module imports,
router imports from `investment_panel.database`, implicit or dynamic facade
exports, unregistered compatibility files or routes, stale generated contracts,
and console scripts without a registered callable owner. The starting Event
Scout cycle and private-import edges are grandfathered only as a Phase 0
ratchet; Phase 5 must reduce both sets to zero.

The inventory is descriptive. It does not make the 700-line threshold a hard
failure. Ruff, OpenAPI generation, TypeScript, Vitest, and pytest remain the
verification tools; no dependency-analysis or code-generation dependency is
added.

## Consequences

- HTTP callers converge on panel snapshot, explicit domain detail, mutation,
  and refresh-job interfaces.
- Domain owners absorb shallow adapters when the deletion test says they add
  no locality or leverage.
- First-party callers must migrate in the same change as a removed route,
  facade, command, or import seam. Temporary redirects and compatibility
  payloads are not part of the interface.
- Configuration and generated-contract changes remain gated until the current
  dirty work is committed and the branch is rebased.
