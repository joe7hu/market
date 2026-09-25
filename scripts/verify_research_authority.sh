#!/bin/sh
set -eu

base_url=${1:-http://localhost:8010}
curl -fsS --max-time 15 "$base_url/api/panel-snapshot?scope=research-authority" |
  jq -e '.status.ready == true and (.tables.research_hypotheses.rows | length) > 0 and (.tables.research_trials.rows | length) > 0 and (.tables.research_strategy_forecasts.rows | length) <= 30 and (.tables.research_universe_observations.rows | length) <= 30'
