# Options Chain Live Capacity Report - 2026-07-23

Scope: live PostgreSQL option-history collection for QQQ core 15-minute cadence and NVDA hourly shadow collection.

## Summary

QQQ collection reached the official close with complete captures after the 11:00 scheduler-reload defect was fixed, but the day does not pass the Landing 2 full-session gate because completed QQQ slot coverage was 25/27, below the 95% threshold. NVDA shadow collection passed its one-day collection evidence check with 8/8 expected hourly/close slots captured and no publication-cap breach.

## QQQ Evidence

- Expected slots: 27, from 09:30 through 16:00 ET.
- Complete slots: 25.
- Explicit deferred slots: 1, at 11:00 ET with `collector_orphaned_after_shutdown`.
- Missing slots: 1, at 09:30 ET before the live parity observation started.
- Completed-slot coverage: 25/27 = 92.59%.
- Recorded-slot coverage including explicit deferral evidence: 26/27 = 96.30%.
- Contract persistence for completed captures: 11,556/11,556 for every completed QQQ slot.
- Completed capture duration: p95 3.98 minutes, max 4.00 minutes.
- Duplicate slot generations: none observed for 2026-07-23.

Gate result: fail for Landing 2 full-session parity because completed-slot coverage is below 95%. The next full regular session must be observed without reload interruption or missed opening slot before QQQ planner parity is considered passed.

## NVDA Shadow Evidence

- Expected slots: 8, at 09:30, 10:30, 11:30, 12:30, 13:30, 14:30, 15:30, and 16:00 ET.
- Complete slots: 8.
- Contract persistence: 3,834/3,834 for every completed NVDA slot.
- Completed capture duration: p95 1.36 minutes, max 1.41 minutes.
- Publication cap: live decisions remained WATCH-only during validation.
- Symbol-scoped health: `/api/options/history/health?symbol=NVDA` reports 8/8 slots and a qualified 2026-07-23 session.

Gate result: backend shadow collection evidence is good, but NVDA remains collection-only and does not waive the QQQ five-qualified-session gate.

## Shared Capacity And Radar

- Active provider leases after close: none.
- Live spot checks during the day showed at most one active option-history lease at a time.
- QQQ and NVDA were collected sequentially inside the history job when both were due.
- Options Radar API returned 10 published opportunities, but the latest opportunity payload still referenced 2026-07-22 evidence. This report does not close the Radar freshness regression gate; it only confirms no active provider lease contention remained after option-history runs.

## API Checks

- `/api/status`: ready, PostgreSQL, latest complete option-history slot `2026-07-23T16:00:00-04:00`.
- `/api/options/history/symbols`: QQQ active/PAPER_READY; NVDA shadow/WATCH.
- `/api/options/history/health?symbol=QQQ`: QQQ-scoped 25/27 complete-slot health for 2026-07-23.
- `/api/options/history/health?symbol=NVDA`: NVDA-scoped 8/8 hourly-slot health for 2026-07-23.

## Follow-Ups

- Observe the next full regular session for QQQ Landing 2 parity.
- Keep NVDA paused at shadow/WATCH until QQQ Landing 2 and the approved expansion gates pass.
- Add historical lease-event telemetry if the two-provider-collector maximum must be proven from persisted evidence rather than live spot checks.
- Validate Options Radar freshness separately from option-history collection.
