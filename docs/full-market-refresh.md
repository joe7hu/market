# Full-Market Refresh

The daily workflow refreshes independent raw-source clusters, then publishes
one atomic decision generation. PostgreSQL readers stay responsive while
provider calls run; `ops.job_run` plus a partial unique index enforce
cross-process single flight.

## Command

```bash
cd /Users/joehu/proj/market
git pull --rebase origin main
uv run market-full-refresh --config config.yaml
```

Run this on `mini1.local` from the canonical checkout. Do not schedule the full
refresh from stale worktrees such as
`/Users/joehu/proj/market-source-modularization`; their status files can look
fresh while the primary app checkout remains stale.

For LAN access after the refresh:

```bash
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
npx vite --host 0.0.0.0
```

Verify the API from another local device:

```text
http://192.168.50.197:8000/api/status
```

## Step Order

1. `arco_sources`: manifest the latest Arco source files and normalize compact
   evidence rows.
2. `market_data`: refresh daily bars and latest quotes.
3. `content_sources`: archive and normalize configured news, RSS/Substack, and X.
4. `market_events` and `disclosures`: refresh official schedules, CSVs, House
   PDFs, and SEC 13F payloads incrementally.
5. Robinhood/IBKR option and broker sources run independently; unavailable
   providers produce warnings without erasing the last good publication.
6. `options_radar`: retain actionable feature/decision rows and aggregate rejects.
7. `option_outcomes`: update one compact outcome per actionable decision.
8. `run_option_agents`: optionally run configured local external agent commands
   for open options-radar thesis and postmortem handoffs. This step is
   the daily premarket interpretation boundary; hourly refreshes must not call
   it. When enabled, Market claims bounded thesis and postmortem queues, passes
   one consolidated batch JSON over stdin, accepts only structured JSON over
   stdout, then dispatches, persists, and validates each item through the
   deterministic backend.
9. `/today` and market read models publish atomically, then reference-safe
   retention runs and `pg_dump --format=custom` writes a checksum-verified backup.

The orchestrator records step summaries in `ops.job_run`; source details live in
`ingest.run`. Provider payloads and backups are archived outside PostgreSQL and
referenced by manifest rows.

## Agent Handoff

The radar exposes open hypothesis work through `GET /api/agent-thesis-requests`
and `GET /api/agent-postmortem-requests`. Agents fulfill those requests by
posting structured JSON to the local-only endpoints:

- `POST /api/agent-thesis`: stores an `agent_thesis`, attaches it to matching
  candidate events, and immediately runs deterministic thesis validation. The
  validation checks required proofs, catalysts, invalidation, evidence backing,
  option/stock state, IV state, and red-team risk flags from source antithesis,
  candidate blockers, technical trend, liquidity, cash burn, growth, and balance
  sheet data. Validation rows are keyed by thesis, strategy version, validation
  date, and candidate event so the daily loop can compare point-in-time thesis
  state without mixing strategy versions.
- `POST /api/agent-postmortems`: stores an `agent_postmortem`, materializes any
  proposed strategy mutation, and immediately runs deterministic backtest and
  forward-test gates.

The same handoff can run as a job with `market-run-option-agents` or the
allowlisted `run_option_agents` refresh job. Configure commands under:

```yaml
agents:
  option_agent:
    enabled: true
    command: "market-codex-option-agent"
    timeout_seconds: 180
    thesis_limit: 8
    postmortem_limit: 4
    provider: codex
    model: gpt-5.6-luna
    reasoning_effort: high
    max_runs_per_day: 1
```

The unified command receives one object with `thesis` and `postmortem` arrays
plus shared guardrails. It returns matching arrays in the same order. Each
request includes its published per-ticker context and stable request id.
`MARKET_OPTION_AGENT_COMMAND` can override the configured command for local
runs. Use `market-codex-option-agent` to run through the signed-in Codex
ChatGPT OAuth session without an API key. Thesis Monitor and the pre-open
narrative use the same restricted Codex path. These commands run Codex with
shell, app, browser, plugin, computer-use, multi-agent, image generation, and
web-search tools disabled, ignore user config/rules, and pass only an allowlisted
environment to the child process. The Codex adapter timeout defaults to `90`
seconds so it
exits before the option-agent runner's default `120` second command timeout;
keep `MARKET_CODEX_TIMEOUT_SECONDS` lower than the configured runner timeout
when overriding either value. Market's configured app paths are OAuth-only and
must use the `market-codex-*` commands. The developer-only `market-openai-*`
entry points are direct Platform API clients and are not used by the app.
Market defaults its configured OAuth workflows to `gpt-5.6-luna` with high
reasoning. `MARKET_CODEX_MODEL` and `MARKET_CODEX_REASONING_EFFORT` can override
those defaults for a local run.

These endpoints are handoff boundaries, not trading commands. Agent payloads are
hypotheses and proposals only; deterministic code still owns option math,
candidate state, validation, backtests, forward tests, and human-approval gates.
Scheduled option agents are capped at `max_runs_per_day`, skip without creating
a fake run when no work is queued, and retain estimated Codex usage even when a
child command fails. Thesis monitor pre-open work also skips when its stable
decision-input fingerprint is unchanged. Agents should run once per day before
the market review window; the hourly options loop is deterministic-only to avoid
duplicate prompts and token churn.

## Freshness Contracts

- Intraday quotes, options, and news are stale after `4` market hours.
- Daily prices, technicals, SEPA, liquidity, and correlation rows are stale
  after `1` trading day.
- Fundamentals, 13F rows, and disclosure rows are stale by filing cadence, not
  daily market time.
- Arco thesis evidence is stale after `7` days unless refreshed or reinforced.
- Documentation rows are documentation. They must not count as healthy provider
  runs.

## Daily Acceptance Checks

After a successful refresh:

- `/api/source-freshness` shows no stale source as healthy.
- `/api/sources` lists enabled source families with latest run, item counts,
  ticker counts, and any failure/detail state.
- `/api/ticker-source-signals` shows source-discovered ticker evidence; rows
  missing quote/daily analysis are marked `needs_market_context`.
- `/api/decision-queue` has nonempty `Act`, `Research`, `Watch`, `Reject`, and
  `Stale` buckets when seeded data supports them.
- Top `Act` rows have current `as_of` values, nonzero source/evidence counts,
  no stale-data blocking gates, and explicit invalidation.
- Source-thin or stale opportunities are not silently promoted into the top
  ranked queue.
- `/api/tickers/{symbol}/decision-snapshot` explains action grade, source
  cluster, freshness, decision basis, blocking gates, portfolio impact, and
  invalidation.
- `/api/panel-snapshot?scope=options-radar` includes nonempty radar tables when
  option chains exist: `option_snapshot`, `option_features`, `stock_features`,
  `option_radar_opportunity`, `candidate_event`, `candidate_event_mark`,
  `candidate_event_attribution`, `shadow_trade`, `radar_state_transition`,
  `missed_winner_event`, and strategy validation/proposal tables.

## Suggested Daily Schedule

Use the existing automation runner or launchd to run deterministic options radar
refreshes during market hours, for example:

```bash
cd /Users/joehu/proj/market
uv run python -m investment_panel.jobs.hourly_options_radar --config config.yaml
```

The checked-in launchd definition is:

```text
ops/launchd/com.joehu.market.hourly-options-radar.plist
```

Install or refresh it on the machine that owns the local Market app with:

```bash
cp ops/launchd/com.joehu.market.hourly-options-radar.plist ~/Library/LaunchAgents/
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.joehu.market.hourly-options-radar.plist 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.joehu.market.hourly-options-radar.plist
launchctl print gui/$(id -u)/com.joehu.market.hourly-options-radar
```

The job uses `/tmp/market-hourly-options-radar.lock`, so a slow deterministic
run skips the next hourly tick instead of starting overlapping radar
recomputes. It writes
`/Volumes/agent/data-sources/status/mini-market-hourly-options-radar.json`.
Do not add provider refreshes back to this hourly job; long provider phases
increase API contention and can overlap the next cadence even though PostgreSQL
remains responsive. Provider ingestion belongs in `full_market_refresh`,
`update_free_sources`, or the premarket options workflow.

Run the broader agent-bearing workflow once before the local investment review
window, for example:

```bash
cd /Users/joehu/proj/market
MARKET_DATABASE_URL=postgresql:///market uv run python -m investment_panel.core.refresh_jobs \
  premarket_options_intelligence --config config.yaml
```

The checked-in weekday premarket launchd definition is:

```text
ops/launchd/com.joehu.market.premarket-options-intelligence.plist
```

Install or refresh it with:

```bash
cp ops/launchd/com.joehu.market.premarket-options-intelligence.plist ~/Library/LaunchAgents/
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.joehu.market.premarket-options-intelligence.plist 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.joehu.market.premarket-options-intelligence.plist
launchctl print gui/$(id -u)/com.joehu.market.premarket-options-intelligence
```

This job runs through the canonical refresh-job boundary, so its lifecycle is
visible in `ops.job_run` and `/api/refresh-jobs`. It composes `options_radar ->
run_option_agents -> thesis_monitor -> deterministic options_radar -> /today`
once. US market holidays are rejected before an agent can run. The pre-open
narrative receives a compact decision context capped at 20,000 characters and
records its own invocation and estimated token usage.

The premarket workflow intentionally composes already-ingested facts. Run a
separate source-plus-publication refresh after the options market opens so
`/options-radar` does not merely republish an older Robinhood snapshot. The
checked-in weekday market-open definition runs at 9:40 AM Eastern:

```text
ops/launchd/com.joehu.market.market-open-options-radar.plist
```

Install or refresh it with:

```bash
cp ops/launchd/com.joehu.market.market-open-options-radar.plist ~/Library/LaunchAgents/
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.joehu.market.market-open-options-radar.plist 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.joehu.market.market-open-options-radar.plist
launchctl print gui/$(id -u)/com.joehu.market.market-open-options-radar
```

This job runs `options_radar_hard_refresh`, which pulls Robinhood option chains
before rebuilding the visible publication. It raises the incremental batch to
the configured 80-symbol radar universe for the once-daily market-open pass;
the collector's existing time and response bounds still apply.

Keep the separate disclosure automation if it already exists; this full refresh
is the missing broad-market workflow that ensures the decision desk has current
market, evidence, event, analysis, and snapshot state.

Cross-machine freshness is checked from Arco:

```bash
cd /Users/joehu/proj/arco
node bin/arco.mjs status-gates
```

The Market gates are `mini-market-full-refresh.json` and
`mini-market-db-snapshot.json`, both fresh under 24 hours.
