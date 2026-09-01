# Recovery query measurements — 2026-08-31

These measurements use `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON, SUMMARY)` against
the live PostgreSQL database. The Funnel plans were recaptured with the API
role's configured three-second statement timeout; the other plans use a
30-second EXPLAIN session because EXPLAIN analysis adds instrumentation
overhead. Runtime API statements remain bounded at three seconds. Portfolio
correlation uses a local 8 MB `work_mem` setting for its bounded de-duplication
sort; it does not change the database-wide setting.

| Read path | Bound | Planning | Execution | Shared hit | Shared read | Temp read/write |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Screener | 120 candidates | 2.033 ms | 697.250 ms | 3,115,440 | 4,309,753 | 0 / 0 |
| QQQ liquidity | 24 rows | 0.418 ms | 54.230 ms | 2,653,901 | 54,043 | 0 / 0 |
| QQQ payoff scenarios | 24 rows | 0.631 ms | 48.924 ms | 592,054 | 354,091 | 0 / 0 |
| QQQ technicals | one symbol | 0.343 ms | 14.633 ms | 385,261 | 89,695 | 0 / 0 |
| QQQ portfolio bars | one symbol | 0.155 ms | 8.688 ms | 210,782 | 0 | 0 / 0 |
| Decision Funnel current rows | 795 current rows | 3.862 ms | 1,130.745 ms | 212,219 | 0 | 0 / 0 |
| Decision Funnel publications | 3,180 compact current rows | 1.813 ms | 399.201 ms | 145,475 | 2,833 | 0 / 0 |
| Today authority | three-row action page | 0.383 ms | 1,194.489 ms | 889,255 | 1,264 | 0 / 0 |
| Portfolio correlation | eight current holdings, 365 calendar days | 3.741 ms | 91.733 ms | 55,800 | 3,776 | 0 / 0 |

The screener option-count branch uses `ix_analysis_decision_instrument` for
each bounded candidate. Liquidity selects the latest history snapshot through
`ix_raw_option_snapshot_history_lookup` before quote reads. Payoff, technical,
and portfolio reads push the ticker or instrument filter into the raw-fact
selection before dense joins and window operations.

All eight plans completed without statement cancellation or temporary-file
spill. The full SQL and buffer plans were captured directly from the repaired
query owners; no production timeout or `VACUUM FULL` was used. The Funnel
runtime also completed below three seconds on the cold and concurrent route
probes.
