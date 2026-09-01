import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";

import type { TodayResponse } from "@/api/panel";
import { emptyPanelData } from "@/apiPanelData";
import type { components } from "@/generated/apiSchema";
import { buildModel } from "@/model";
import { PortfolioImpactCard, TradePlanCard } from "./TradePlanCard";
import { ActionQueueCard, TodayPage, tradePlanForAction } from "./today";

type TradePlan = components["schemas"]["TradePlan"];

const response: TodayResponse = {
  status: { ready: true, message: "test", source: "test" },
  count: 1,
  missing_plan_count: 0,
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
    field_states: [{
      field: "trade_plan",
      availability_status: "missing",
      source: "trade_plan",
      reason: "trade_plan_missing",
      blocking: true,
      next_action: "Refresh the ticker decision and publish its canonical TradePlan.",
    }],
  }],
};

describe("Today Action Queue", () => {
  it("uses the generated backend item and exact required field names", () => {
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
    expect(missing).toContain("trade_plan_missing");
    expect(missing).toContain("ticker decision");
    expect(missing).not.toContain("BUY");
  });

  it("does not leak queue terms when a capital plan is missing", () => {
    const markup = renderToStaticMarkup(createElement(ActionQueueCard, {
      item: {
        ...response.actions![0],
        action: "BUY",
        lifecycle_state: "actionable",
        rationale: "queue rationale leak",
        primary_blocker: "queue blocker leak",
        next_action: "queue next action leak",
        expires_at: "2026-09-19T13:30:00Z",
        trade_plan: null,
      },
      onOpenTicker: () => undefined,
    }));

    expect(markup).toContain("NO TRADE");
    expect(markup).toContain("CASH");
    expect(markup).toContain("Field unavailable: trade_plan");
    expect(markup).toContain("Source: trade_plan");
    expect(markup).toContain("Reason: trade_plan_missing");
    expect(markup).toContain("This blocks the decision.");
    expect(markup).not.toContain("BUY");
    expect(markup).not.toContain("queue rationale leak");
    expect(markup).not.toContain("queue blocker leak");
    expect(markup).not.toContain("queue next action leak");
    expect(markup).not.toContain("2026-09-19");
  });

  it("keeps the three-column queue summary compact", () => {
    const markup = renderToStaticMarkup(createElement(ActionQueueCard, {
      item: { ...response.actions![0], action: "BUY", lifecycle_state: "actionable", trade_plan: plan() },
      onOpenTicker: () => undefined,
    }));

    expect(markup).toContain("Action:");
    expect(markup).toContain("Stored rationale.");
    expect(markup).not.toContain("Canonical trade plan");
  });

  it("does not render a second capital ranking outside the canonical queue", () => {
    const base = response.actions![0];
    const data = emptyPanelData();
    const ranked = Array.from({ length: 4 }, (_, index) => ({
      ...base,
      projection_identity: `capital:ranked:${index + 1}`,
      source: "capital_action",
      ticker: `RANKED${index + 1}`,
      title: `Ranked capital action ${index + 1}`,
      trade_rank: index + 1,
    }));
    const markup = renderToStaticMarkup(createElement(TodayPage, {
      data,
      model: buildModel(data),
      lastRefresh: null,
      actionQueue: {
        ...response,
        actions: [],
        book_actions: [...ranked, {
          ...base,
          projection_identity: "capital:unranked",
          source: "capital_action",
          ticker: "UNRANKED",
          title: "Unranked capital action must stay hidden",
          trade_rank: null,
        }, {
          ...base,
          projection_identity: "capital:book:CASH",
          source: "cash",
          ticker: null,
          title: "Cash",
          action: "CASH",
          lifecycle_state: "current",
          next_action: "Hold cash until a ranked opportunity appears.",
          primary_blocker: null,
          trade_rank: null,
        }],
      },
      actionQueueLoading: false,
      actionQueueError: null,
      loading: false,
      onRefresh: () => undefined,
      onOpenTicker: () => undefined,
    }));

    expect(markup).not.toContain("Top three ranked capital actions");
    expect(markup).not.toContain(">RANKED1<");
    expect(markup).not.toContain("Hold cash until a ranked opportunity appears.");
    expect(markup).not.toContain(">UNRANKED<");
    expect(markup).not.toContain("Unranked capital action must stay hidden");
  });

  it("keeps numeric terms at stored precision", () => {
    const base = plan();
    const markup = renderToStaticMarkup(createElement(TradePlanCard, {
      plan: {
        ...base,
        entry_limit: 1234.56,
        max_loss_per_unit: 1234.56,
        planned_loss: 1234.56,
        portfolio_impact: { ...base.portfolio_impact!, risk_budget_consumed: 1234.56789 },
      },
    }));

    expect(markup).toContain("1,234.56");
    expect(markup).toContain("1,234.56789");
    expect(markup).not.toContain("1,235");
  });

  it("renders every Phase 1 impact group from the stored contract", () => {
    const impact = {
      ...plan().portfolio_impact!,
      contract_version: "portfolio-impact.v1-review",
      opportunity_episode_id: "episode-impact-1",
      expression_identity: "CALL:AAA:impact",
      cutoff: "2026-08-28T13:29:00Z",
      input_lineage: [{ available_at: "2026-08-28T13:00:00Z", field: "portfolio", source_id: "impact-source" }],
      greeks: { delta: 0.42 },
      gross_exposure_before: 0.7,
      gross_exposure_after: 0.8,
      net_exposure_before: 0.5,
      net_exposure_after: 0.6,
      position_weight_before: 0.1,
      position_weight_after: 0.12,
      portfolio_before: { beta: 1.01 },
      portfolio_after: { beta: 1.03 },
      symbol_concentration_delta: 0.02,
      sector_concentration_delta: 0.01,
      beta_delta: 0.02,
      correlation_cluster_delta: 0.03,
      factor_exposure: { growth: 0.4 },
      planned_loss: 250,
      tail_risk_penalty: 0.04,
      adv_participation: 0.005,
      days_to_exit: 1.5,
      expected_transaction_costs: 3.25,
      liquidity: { average_daily_dollar_volume: 500000000 },
      scenario_pnl: { market_down_20: -1200 },
      cash_comparator: { planned_loss: 0 },
      top_alternative: "MSFT",
      funding_source_or_position_to_trim: "Trim QQQ",
    };

    const markup = renderToStaticMarkup(createElement(PortfolioImpactCard, { impact }));

    for (const text of [
      "Before and after exposure", "Concentration and shared risk", "Loss and risk budget",
      "Liquidity", "Stress and alternatives", "Core stress scenarios", "Cash comparator",
      "market_down_20", "average_daily_dollar_volume", "Trim QQQ", "MSFT",
      "portfolio-impact.v1-review", "episode-impact-1", "CALL:AAA:impact",
      "2026-08-28T13:29:00Z", "impact-source", "delta", "0.42",
    ]) expect(markup).toContain(text);
  });

  it("does not infer a missing primary blocker from the blocker list", () => {
    const markup = renderToStaticMarkup(createElement(TradePlanCard, { plan: plan({
      action: "NO_TRADE",
      authorization_mode: "NONE",
      blockers: ["list_only_blocker"],
      eligibility: "BLOCKED",
      primary_blocker: null,
      selected_expression_kind: "CASH",
    }) }));

    expect(markup).toContain("Not supplied");
    expect(markup).not.toContain("list_only_blocker");
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
  availability_status: "available",
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
    availability_status: "available",
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
    ticker: "AAA",
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
    availability_status: "available",
    blockers: [],
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
