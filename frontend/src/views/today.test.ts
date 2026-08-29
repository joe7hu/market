import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";

import type { TodayResponse } from "@/api/panel";
import type { components } from "@/generated/apiSchema";
import { TradePlanCard } from "./TradePlanCard";
import { tradePlanForAction } from "./today";

type TradePlan = components["schemas"]["TradePlan"];

const response: TodayResponse = {
  status: { ready: true, message: "test", source: "test" },
  count: 1,
  actions: [{
    projection_identity: "capital:ticker-decision:decision:AAA",
    source_authority: "ticker-decision:decision:AAA",
    source: "capital_action",
    title: "AAA capital action",
    lifecycle_state: "blocked",
    primary_blocker: "trade_plan_missing",
    next_action: "Refresh the ticker decision and trade plan.",
    ticker: "AAA",
    action: "NO_TRADE",
    owned: false,
    rationale: "Cash is selected.",
    policy_version: "risk-policy.v2:legacy",
    selected_expression: "CASH",
  }],
};

describe("Today Action Queue", () => {
  it("uses the generated backend item and its stable identity", () => {
    expect(response.actions?.[0]).toMatchObject({
      projection_identity: "capital:ticker-decision:decision:AAA",
      source_authority: "ticker-decision:decision:AAA",
      action: "NO_TRADE",
      selected_expression: "CASH",
      primary_blocker: "trade_plan_missing",
    });
  });

  it("renders stored paper terms through the canonical card", () => {
    const markup = renderToStaticMarkup(createElement(TradePlanCard, { plan: plan({ authorization_mode: "PAPER" }) }));

    expect(markup).toContain("PAPER ONLY");
    expect(markup).toContain("$100.00–$101.00");
    expect(markup).toContain("$100.50");
    expect(markup).toContain("2026-09-19");
    expect(markup).toContain("Close below 90");
    expect(markup).toContain("BUY");
    expect(markup).toContain("AAA");
    expect(markup).toContain("contract-1");
  });

  it("labels advisory terms without implying paper authorization", () => {
    const markup = renderToStaticMarkup(createElement(TradePlanCard, { plan: plan({ authorization_mode: "ADVISORY" }) }));

    expect(markup).toContain("ADVISORY");
    expect(markup).not.toContain("PAPER ONLY");
  });

  it("fails closed for blocked and missing plans", () => {
    const blocked = renderToStaticMarkup(createElement(TradePlanCard, { plan: plan({
      action: "NO_TRADE",
      authorization_mode: "NONE",
      blockers: ["rank_missing"],
      eligibility: "BLOCKED",
      primary_blocker: "rank_missing",
      selected_expression_kind: "CASH",
      next_action: "Refresh the ticker decision.",
      quantity: null,
      entry: null,
      entry_limit: null,
      max_loss_per_unit: null,
      planned_loss: null,
      invalidation: null,
      profit_exit: null,
      portfolio_impact: null,
    }) }));
    const missing = renderToStaticMarkup(createElement(TradePlanCard));

    expect(blocked).toContain("NO TRADE");
    expect(blocked).toContain("CASH");
    expect(blocked).toContain("rank_missing");
    expect(blocked).toContain("Refresh the ticker decision.");
    expect(blocked).not.toContain("BUY");
    expect(blocked).not.toContain("contract-1");
    expect(missing).toContain("NO TRADE");
    expect(missing).toContain("CASH");
    expect(missing).toContain("Unavailable");
    expect(missing).not.toContain("BUY");
  });

  it("gives only capital actions a trade-plan presentation", () => {
    expect(tradePlanForAction({ ...response.actions![0], source: "capital_action" })).toBeNull();
    expect(tradePlanForAction({ ...response.actions![0], source: "inbox_transition" })).toBeUndefined();
  });
});

const plan = (overrides: Partial<TradePlan> = {}): TradePlan => ({
  action: "BUY",
  alpha_signal_id: "signal-1",
  authorization_mode: "PAPER",
  blockers: [],
  contract_version: "trade-plan.v1",
  cutoff: "2026-08-28T13:30:00Z",
  data_quality: "FRESH",
  decision_revision: "decision-1",
  eligibility: "ACTIONABLE",
  entry: { low: 100, high: 101 },
  entry_limit: 100.5,
  expiry: "2026-09-19",
  input_lineage: [{
    available_at: "2026-08-28T13:00:00Z",
    field: "price",
    source_id: "source-1",
  }],
  invalidation: { kind: "price", statement: "Close below 90", value: 90 },
  market_snapshot_id: "snapshot-1",
  market_state_publication_id: "market-publication-1",
  max_loss_per_unit: 25,
  next_action: "Review the stored terms.",
  opportunity_episode_id: "episode-1",
  planned_loss: 50,
  policy_version: "risk-policy.v2",
  portfolio_impact: {
    availability: "available",
    blockers: [],
    contract_version: "portfolio-impact.v1",
    cutoff: "2026-08-28T13:30:00Z",
    decision_revision: "decision-1",
    expression_identity: "CALL:AAA:1",
    expression_kind: "CALL",
    impact_id: "impact-1",
    input_lineage: [],
    market_snapshot_id: "snapshot-1",
    opportunity_episode_id: "episode-1",
    positions_most_correlated: ["MSFT"],
    risk_policy_version: "risk-policy.v2",
    marginal_risk: 0.2,
    risk_budget_consumed: 0.1,
    diversification_benefit: 0.05,
  },
  portfolio_impact_id: "impact-1",
  primary_blocker: null,
  profit_exit: { low: 120, high: 125 },
  quantity: 2,
  rank_id: "rank-1",
  rationale: "Stored rationale.",
  selected_expression: {
    horizon: "TACTICAL",
    kind: "CALL",
    legs: [{ contract_id: "contract-1", option_type: "call", side: "long", strike: 105, expiration: "2026-09-19" }],
    rationale: "Stored expression rationale.",
    selected: true,
    stance: "BULLISH",
    status: "eligible",
    thesis_revision: "thesis-1",
    ticker: "AAA",
  },
  selected_expression_identity: "CALL:AAA:1",
  selected_expression_kind: "CALL",
  ticker: "AAA",
  trade_plan_id: "plan-1",
  publication_id: "publication-1",
  ...overrides,
});
