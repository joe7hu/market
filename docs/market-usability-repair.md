# Market usability repair

Goal: one usable daily decision flow for Joe, using existing pages, sources and
contracts. Base: `4f35edf`. Worktree: `market-usability-repair`. Target: main.
Keep PostgreSQL authority, paper-only execution, revision checks and real account
inputs. No new engine, provider, framework or speculative schema.

## Fixed completion checklist

| ID | Requirement | State | Proof |
| --- | --- | --- | --- |
| U1 | Shared allocation and field-state messages are local, clear and non-redundant | done | Shared notice renders only missing actionable information; allocation diagnostics removed from investment routes; frontend checks. |
| U2 | Opportunities open the existing ticker brief; no raw IDs or placeholder comparison | done | Decision brief opens the ticker dossier; thesis and countercase use stored decision evidence; browser AVGO. |
| U3 | Screener uses real fields, units, dates, filtering, sorting and pagination | done | Browser pagination 120 → 240 → 320; units, null/zero and episode identity regressions pass. |
| U4 | Ticker snapshot loads with revision checks; reasons, evidence and next action lead | done | Automatic revision-checked snapshot; source links prioritized; ticker tests and three-ticker API proof. |
| U5 | Portfolio shows real impacts; CASH and empty blockers do not create false warnings | done | CASH impact cards omitted; missing cash remains an explicit account input; loaded Portfolio browser check. |
| U6 | Research shows ticker evidence; diagnostic authority stays in System | done | Research uses sources scope: 318 ticker rankings; authority table in System details only. |
| U7 | Command Center prioritizes current holding risks and a bounded daily brief | done | Holding risks sort first; bounded action queue; loaded mobile Command Center check. |
| U8 | Supported missing data repaired; genuine external gaps identified | done | Current PostgreSQL metric_set/values and nested episode identity repaired; see genuine gaps below. |
| U9 | AVGO, MSFT and TSLA agree across API and rendered pages | open | AVGO, MSFT, TSLA API checks: matching decision/action identity, fundamentals restored, thesis and countercase present. Live release check pending. |
| U10 | Focused checks, independent review, release gate and deployed browser proof | open | Independent REVIEW_PASS; 105 frontend tests and build pass; 1530 backend tests pass with 80.47% coverage. Live release check pending. |

Stop when all rows have evidence and no repairable defect remains open. Genuine
account/history limits must be explicit; do not infer completion of older
campaign observation gates. Batch review findings before the final release gate.

## Validation and review

- `make check` passed locally. Final frontend delta: `npm run test:frontend` (105 tests), `npm run build` passed.
- `make release-gate` passed on miniTs (1530 backend tests, 80.47% coverage). Frozen tree `cd66bd77600263babbacf8dbd8addbbfa349f034`; backend is unchanged since this tree. Later frontend changes were checked locally.
- Independent reviewer fixed-point verdict: REVIEW_PASS. Both findings repaired: linked thesis evidence crowding and same-ticker opportunity pagination identity. Regression checks cover both.
- CLI autoreview wrapper rejected an absolute finding path and produced no valid verdict. The independent full-diff review replaced that failed run; it is not counted as a pass.
- CI remains manually disabled. No CI result is claimed.

## Genuine data gaps

- Manual cash has not been reconciled. The app requires Joe's actual balance; it must not infer one from holdings.
- Some providers do not report metrics for every company or asset (for example KLRA). Negative forward P/E is not presented as a meaningful valuation.
- Missing qualified forecasts and validation observations still prevent new trades. Existing research remains readable.
- A dated reference close is not an executable quote; execution freshness rules remain unchanged.

No new provider, engine, schema, or dependency was added. PostgreSQL and paper-only controls are unchanged.
