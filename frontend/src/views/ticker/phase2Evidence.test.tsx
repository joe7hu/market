import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { components } from "@/generated/apiSchema";
import { OpportunityRankPanel, TickerDecisionPanel } from "@/views/ticker/panels";

const compactDecision = {
  ticker: "QQQ",
  decision_revision: "revision-1",
  capital_action: { ticker: "QQQ", action: "CASH", owned: false, rationale: "Wait for complete evidence." },
  input_manifest: { input_hash: "a".repeat(64), experiment_id: "test" },
  tactical: { stance: "NEUTRAL", action: "HOLD", conviction_tier: "LOW", confidence: 0, scenarios: [] },
  fundamental: { stance: "NEUTRAL", action: "HOLD", conviction_tier: "LOW", confidence: 0, scenarios: [] },
  expressions: {},
  portfolio_impacts: {},
} as never;

const panelProps = {
  decision: compactDecision,
  snapshotLoading: false,
  snapshotError: null,
  onLoadSnapshot: async () => {},
  collecting: null,
  onCollect: async () => {},
};

describe("OpportunityRankPanel Phase 2 evidence", () => {
  it("keeps the compact ticker page bounded and loads heavy context on demand", () => {
    const compactHtml = renderToStaticMarkup(<TickerDecisionPanel {...panelProps} />);
    expect(compactHtml).toContain("Load decision context");
    expect(compactHtml).not.toContain("Book opportunity rank");

    const snapshotHtml = renderToStaticMarkup(<TickerDecisionPanel {...panelProps} snapshot={{
      ...compactDecision,
      alpha_signals: [],
      opportunity_rank: { trade_rank: 1 },
      trade_plan: null,
      data_requests: [],
      learning: {},
    }} />);
    expect(snapshotHtml).toContain("Book opportunity rank");
  });

  it("uses rank alpha identity and renders all qualification evidence", () => {
    const signals = [{
      signal_id: "not-ranked",
      target: "wrong-target",
      forecast_value: 99,
    }, {
      signal_id: "ranked-signal",
      target: "positive_return_after_costs",
      horizon: "TACTICAL",
      model_version: "ticker-stock-alpha.v2",
      feature_version: "stock-research-features.v1",
      oos_period_start: "2026-01-01T00:00:00Z",
      oos_period_end: "2026-06-30T00:00:00Z",
      cohort_path: ["cohort:large-liquid", "horizon:TACTICAL"],
      fallback_parent: "global",
      effective_sample_size: 84,
      calibration_state: "calibrated_hierarchical",
      calibration_metrics: { brier_score: 0.17, calibration_error: 0.04 },
      research_score: 0.72,
      cost_model_version: "stock-cost-slippage.v1",
      lower_confidence_net_utility_after_costs: 0.03,
      promotion_stage: "paper",
    }] as unknown as components["schemas"]["AlphaSignal"][];
    const rank = {
      alpha_signal_id: "ranked-signal",
      research_rank: 2,
      trade_rank: 1,
      trade_utility: 0.03,
      instrument_state_snapshot_id: "snapshot:AAA",
    } as components["schemas"]["OpportunityRank"];

    const html = renderToStaticMarkup(<OpportunityRankPanel signals={signals} rank={rank} />);

    expect(html).not.toContain("wrong-target");
    for (const text of [
      "positive_return_after_costs", "TACTICAL", "ticker-stock-alpha.v2",
      "stock-research-features.v1", "2026-01-01", "2026-06-30",
      "cohort:large-liquid", "horizon:TACTICAL", "global", "84", "0.17",
      "0.72", "stock-cost-slippage.v1", "0.03", "paper",
      "snapshot:AAA",
    ]) expect(html).toContain(text);
  });
});
