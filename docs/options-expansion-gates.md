# Options expansion gates

This runbook protects multi-symbol option-history expansion. It is a current
operating rule, not a historical audit. New symbol activation remains paused
until the relevant collection, publication, and cross-symbol checks pass.

## Required controls

- Failed fits, missing spot data, incomplete chains, and stale captures remain
  rejected with explicit blockers and null derived values.
- Readiness denominators count qualified regular sessions, not snapshots.
- Calibration and outcomes remain scoped by instrument.
- Shadow symbols stay below the configured publication cap until their own
  evidence qualifies them.
- The provider lease limit and scheduler capacity remain two.
- Orphaned captures are finalized as deferred evidence after lease expiry;
  reloads must not leave running capture, snapshot, or ingestion rows behind.
- Hourly symbols are due only in their configured slot window.
- Symbol-scoped health endpoints use that symbol's own cadence and denominator.

## Current expansion rule

Keep new symbols collection-only or `WATCH` until the core QQQ session gate,
the symbol's own complete-session evidence, publication freshness, and the
independent validation gates all pass. A successful shadow collection does not
waive the core gate.

The follow-latest UI control remains deferred until a product need is
confirmed. If it is added, it must preserve the existing URL-owned snapshot,
abort, and request-ordering contracts.

## Focused checks

The option-history policy, materializer, health, and publication tests are the
source of truth for these rules. Use `make test-options` and the relevant
`tests/options/history/` tests before changing cadence, symbol caps, or
publication behavior.
