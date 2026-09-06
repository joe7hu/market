import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { components } from "@/generated/apiSchema";
import { EvidencePanel, OpportunityRankPanel, TickerDecisionPanel } from "@/views/ticker/panels";

const compactDecision = {
  ticker: "QQQ",
  decision_revision: "revision-1",
  capital_action: { ticker: "QQQ", action: "CASH", owned: false, rationale: "Wait for complete evidence." },
  input_manifest: { input_hash: "a".repeat(64), experiment_id: "test" },
  tactical: { stance: "NEUTRAL", action: "HOLD", conviction_tier: "LOW", confidence: 0, scenarios: [] },
  fundamental: { stance: "NEUTRAL", action: "HOLD", conviction_tier: "LOW", confidence: 0, scenarios: [] },
  expressions: {},
  portfolio_impacts: {},
} as unknown as components["schemas"]["TickerDecisionDetailResponse"];

const panelProps = {
  decision: compactDecision,
  snapshotLoading: false,
  snapshotError: null,
  onLoadSnapshot: async () => {},
  collecting: null,
  onCollect: async () => {},
};

describe("Ticker decision usability", () => {
  it("keeps linked thesis evidence visible when other evidence fills the list", () => {
    const html = renderToStaticMarkup(<EvidencePanel
      sources={{ evidence: Array.from({ length: 12 }, (_, i) => ({ title: `News ${i}` })) }}
      thesisEvidence={[{ title: "Thesis source", reference: "https://example.com/research" }]}
    />);
    expect(html).toContain('href="https://example.com/research"');
    expect(html).toContain("Thesis source");
  });
  it("shows loading without a manual load gate or identifiers", () => {
    const html = renderToStaticMarkup(<TickerDecisionPanel {...panelProps} snapshotLoading />);
    expect(html).toContain("Loading trade details");
    expect(html).not.toContain("Load decision context");
    expect(html).not.toContain("revision-1");
    expect(html).not.toContain("Field unavailable:");
  });
  it("keeps snapshot errors actionable", () => {
    const html = renderToStaticMarkup(<TickerDecisionPanel {...panelProps} snapshotError="The decision changed." />);
    expect(html).toContain("The decision changed.");
    expect(html).toContain("Retry decision details");
  });
  it("uses the matching alpha signal without showing its identity", () => {
    const signals = [{ signal_id: "wrong", horizon: "WRONG" }, { signal_id: "matched", horizon: "TACTICAL", effective_sample_size: 84 }] as components["schemas"]["AlphaSignal"][];
    const rank = { alpha_signal_id: "matched", research_rank: 2, trade_rank_unavailable_reason: "forecast_missing", instrument_state_snapshot_id: "private-id" } as components["schemas"]["OpportunityRank"];
    const html = renderToStaticMarkup(<OpportunityRankPanel signals={signals} rank={rank} />);
    expect(html).toContain("TACTICAL");
    expect(html).toContain("84");
    expect(html).toContain("within this ranking");
    expect(html).toContain("A supported return forecast is not available.");
    expect(html).not.toContain("WRONG");
    expect(html).not.toContain("private-id");
  });
  it("explains a blocked new trade using the same substantive rank blocker as Opportunities", () => {
    const decision = { ...compactDecision, capital_action: { ...compactDecision.capital_action, action: "AVOID", owned: true, rationale: "cash_comparator" }, resolution: { primary_blocker: "cash_comparator", next_action: "Review evidence" } } as unknown as components["schemas"]["TickerDecisionDetailResponse"];
    const snapshot = { ...decision, opportunity_rank: { blockers: ["cash_comparator", "alpha_strategy_revision_missing"] }, alpha_signals: [], data_requests: [], learning: {} } as unknown as components["schemas"]["TickerDecisionSnapshotResponse"];
    const html = renderToStaticMarkup(<TickerDecisionPanel {...panelProps} decision={decision} snapshot={snapshot} />);
    expect(html).toContain("The investment signal has not passed strategy validation.");
    expect(html).toContain("You hold this stock.");
    expect(html).not.toContain("Do this now");
    expect(html).not.toContain("AVOID");
    expect(html).not.toContain("Owned ticker");
  });
  it("keeps missing plans blocked and does not imply a stock impact for cash", () => {
    const decision = { ...compactDecision, selected_expression: { kind: "CASH" }, portfolio_impacts: { CASH: { availability: "available", risk_budget_consumed: 0 } } } as unknown as components["schemas"]["TickerDecisionDetailResponse"];
    const snapshot = { ...decision, alpha_signals: [], trade_plan: null, data_requests: [], learning: {} } as unknown as components["schemas"]["TickerDecisionSnapshotResponse"];
    const html = renderToStaticMarkup(<TickerDecisionPanel {...panelProps} decision={decision} snapshot={snapshot} />);
    expect(html).toContain("No new trade");
    expect(html).toContain("A complete trade plan is not available.");
    expect(html).not.toContain("proposed impact");
    expect(html).not.toContain("Field unavailable:");
  });
});
