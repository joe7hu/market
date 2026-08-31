# Full application audit — 2026-08-31

## Outcome

The audit covered security boundaries, point-in-time data authority, decision conclusiveness, API performance, browser behavior, architecture, dependencies, tests, and dead code. All confirmed code findings through P2 are fixed. The final operational gate is a full ticker-decision republish for eight safely blocked owned-ticker rows.

## Findings and repairs

### Security

- Contained SPA fallback paths so encoded traversal cannot expose project files.
- Applied the LAN and Tailscale request guard to every API route, including docs.
- Rejected untrusted Host values and public forwarded-client addresses without trusting spoofable forwarding headers.
- Validated content-source URLs, resolved and pinned public addresses, and revalidated every redirect to stop SSRF, DNS rebinding, and redirect escape.
- Replaced unsafe XML parsing with hardened parsing.
- Rejected malformed persisted settings and made credential-file replacement atomic.
- Updated vulnerable Python and frontend dependencies.
- Added locked npm and Python advisory workflows plus native Dependabot coverage for both ecosystems.

### Data integrity and decision authority

- Made PostgreSQL the enforced runtime authority at API and test seams.
- Preserved point-in-time daily bars, benchmark membership, historical holdings, RSS publication times, and MarketState lineage.
- Required exact MarketState publication identity, cutoff equality, benchmark authority, and superseded-publication lineage.
- Kept scoped refreshes from publishing a global market state.
- Preserved the complete paper impact book and selected-expression identity checks.
- Restored exact CASH comparison and kept missing or mismatched rank, plan, utility, lineage, or impact evidence at CASH or NO_TRADE.
- Validated full opportunity episodes, compact funnel artifacts, and exact non-empty lineage before a decision can become actionable.
- Counted the complete missing-plan backlog before queue limits and kept independent Today decision, rank, and plan prefixes.
- Replayed historical portfolio positions at the requested cutoff instead of using current holdings.

### API correctness and performance

- Added bounded, exact scoped pagination and preserved total counts.
- Bounded the panel context cache and added single-flight loading with invalidation-safe wake-up behavior.
- Replaced the wide Today sort with a narrow authority query and primary-key hydration. PostgreSQL execution fell from repeated external sorts with about 200 MB of spill to about 1.24 seconds with no temporary blocks.
- Replaced unbounded Today backlog hydration with a compact PostgreSQL authority and paper-safety result. Only the three bounded action plans receive full Pydantic validation and full JSON hydration.
- Reduced Portfolio from 17.9 MB to 1.28 MB while keeping every current held-ticker decision and its selected impact.
- Reduced Health from 1.75 MB to 215 KB while keeping exact provider counts and rendered recovery health.
- Reduced QQQ detail from 5.05 MB to 38 KB with an explicit compact contract. The 4.84 MB immutable audit artifact remains available at `/api/tickers/QQQ/decision-snapshot`.
- Added native gzip; the 215 KB Health payload transfers in about 21 KB.
- Kept deep ticker option evidence optional so a localized timeout cannot invalidate the core ticker decision.

### UI and conclusive decisions

- Kept Today bounded, source ordered, and explicit about CASH.
- Removed false or permanently unavailable cards from Health and Portfolio.
- Corrected provider activity labels and exact tracked-count language.
- Kept complete audit evidence on its dedicated endpoint while using a generated compact ticker-detail type in the browser.
- Preserved decision-bound market evidence, selected portfolio impact, rank, plan, and data-request surfaces without rebuilding policy in the frontend.
- Verified final desktop and mobile layouts with no console errors, no horizontal overflow, and no false unavailable state.

### Architecture and technical debt

- Removed duplicate coercion and retired configuration-mutation code.
- Reused the existing panel contract, publication repositories, Pydantic models, Starlette middleware, and PostgreSQL snapshot boundary instead of adding new frameworks.
- Added generated OpenAPI checks, panel-contract checks, an 80% backend coverage gate, and dependency advisory automation.
- Added regression coverage at every changed security, money, lineage, cache, and point-in-time boundary.

## Verification

- Fast gate: `make check` passed with 26 architecture guards, 72 frontend tests, Ruff, generated contracts, and TypeScript.
- Focused integrated backend gate: 148 tests passed.
- Full backend coverage gate: 1,214 tests passed at 81.22% against the 80% minimum.
- Production build: 2,628 modules built. The existing large ECharts/vendor chunk warnings remain non-failing; ECharts is already route-lazy and no duplicate chart bundle was found.
- Security: npm found 0 vulnerabilities; pip-audit found no known vulnerability in 72 locked packages; Bandit found no high-severity issue; `uv pip check` found 66 compatible packages.
- Live QA evidence is in `.gstack/qa-reports/qa-report-2026-08-31-full-app-audit.md`.

## Final operational gate

The current database contains eight owned-ticker decisions with a selected-expression versus stored portfolio-impact identity mismatch. They fail closed as BLOCKED and AVOID. After this branch lands, run a full market refresh and ticker-decision republish, then prove that all eight current rows validate and retain exact publication/cutoff lineage.
