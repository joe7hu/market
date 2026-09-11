import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { ResearchSummary } from "@/api/panel";
import { ResearchResults } from "./researchSummary";
import { InboxUsefulnessControls } from "./decisionInbox";

const summary: ResearchSummary = {
  as_of: "2026-09-01T12:00:00Z", paper_only: true, strategy_count: 1,
  strategies: [{ strategy_revision_id: 1, strategy_key: "stock", revision: 3,
    name: "Stock counterfactual", status: "draft", automatic_paper_tuning: true,
    evaluations: [{ stage: "out_of_sample", verdict: "incomplete", independent_sample_count: null,
      net_return_lower_bound: null, brier_score: null, evidence_basis: "obsolete_stock_target", failed_gates: ["sample_size"] }],
    failed_gates: ["sample_size"], included_count: 18, excluded_count: 6, expected_count: 24,
    next_observation: "Collect independent episodes with matured outcomes." }],
  ideas: [{ ticker: "TEST", owned: true, as_of: "2026-09-01T12:00:00Z", thesis: "Demand can recover.",
    countercase: "Demand is still weak.", catalyst: "Next earnings report", invalidation: "Sales decline again.",
    holding_weight_pct: null, next_action: "Review the new earnings evidence." }],
  review_activity: { window_days: 30, total: 10, acknowledged: 8, completed: 2, rated: 0, helpful: 0, not_helpful: 0, helpful_rate: null },
};

describe("Research results", () => {
  it("accepts omitted optional collections from the API contract", () => {
    const withoutCollections = { ...summary, strategies: undefined, ideas: undefined, strategy_count: 0 };
    expect(renderToStaticMarkup(<ResearchResults data={withoutCollections} onOpenTicker={() => undefined} />)).toContain("No strategy evidence is recorded");
    const withoutStages = { ...summary, strategies: (summary.strategies ?? []).map((strategy) => ({ ...strategy, evaluations: undefined, failed_gates: undefined })) };
    expect(renderToStaticMarkup(<ResearchResults data={withoutStages} onOpenTicker={() => undefined} />)).toContain("No evaluation is recorded. Returns and sample size are unknown.");
  });

  it("shows missing evidence without zero returns or implied useful acknowledgments", () => {
    const html = renderToStaticMarkup(<ResearchResults data={summary} onOpenTicker={() => undefined} />);
    expect(html).toContain("Independent observations: unknown");
    expect(html).toContain("Return after modeled costs, lower estimate: unknown");
    expect(html).toContain("Earlier target: cannot validate this stock strategy.");
    expect(html).toContain("18 included · 6 excluded · 24 expected");
    expect(html).toContain("8 acknowledged · 2 reviewed");
    expect(html).toContain("Helpful rate: unknown until feedback is recorded");
    expect(html).toContain("usefulness ratings do not measure profit");
    expect(html).toContain("Demand can recover.");
    expect(html).toContain("Demand is still weak.");
    expect(html).toContain("Sales decline again.");
    expect(html).toContain("weight unavailable");
    expect(html).not.toContain("0.0%");
  });

  it("uses the rated denominator for helpfulness", () => {
    const data = { ...summary, review_activity: { ...summary.review_activity, rated: 2, helpful: 1, not_helpful: 1, helpful_rate: 0.5 } };
    const html = renderToStaticMarkup(<ResearchResults data={data} onOpenTicker={() => undefined} />);
    expect(html).toContain("50.0% of rated items");
  });

  it("shows known shadow samples while comparison returns and Brier remain unknown", () => {
    const data: ResearchSummary = { ...summary, strategies: (summary.strategies ?? []).map((strategy) => ({
      ...strategy, evaluations: [{ stage: "shadow", verdict: "collecting_data", evidence_basis: "independent_options_shadow",
        independent_sample_count: 12, net_return_lower_bound: null, brier_score: null,
        comparison_denominator: 20, unmatched_episodes: 3, comparison_window_complete: false }],
    })) };
    const html = renderToStaticMarkup(<ResearchResults data={data} onOpenTicker={() => undefined} />);
    expect(html).toContain("Independent observations: 12");
    expect(html).toContain("Return after modeled costs, lower estimate: unknown");
    expect(html).toContain("Probability error (Brier): unknown");
    expect(html).toContain("20 episodes · window incomplete · 3 unknown outcomes");
    expect(html).toContain("Prospective candidate shadow outcomes with modeled costs");
    expect(html).not.toContain("Return after actual paper costs");
  });

  it("shows the persisted regime for an available strategy signal", () => {
    const data: ResearchSummary = { ...summary, strategies: (summary.strategies ?? []).map((strategy) => ({
      ...strategy, evaluations: [{ stage: "strategy_signal", verdict: "available", evidence_basis: "independence_unconfirmed",
        signal_regime: "gap_up", signal_direction: "continuation", signal_value: 0.02 }],
    })) };
    const html = renderToStaticMarkup(<ResearchResults data={data} onOpenTicker={() => undefined} />);
    expect(html).toContain("Signal regime: Gap Up");
  });

  it("labels an actual paper comparison separately from modeled shadow outcomes", () => {
    const data: ResearchSummary = { ...summary, strategies: (summary.strategies ?? []).map((strategy) => ({
      ...strategy, evaluations: [{ stage: "execution_grade_paper", verdict: "pass", evidence_basis: "independent_options_paper",
        independent_sample_count: 12, net_return_lower_bound: 0.03, brier_score: 0.2,
        comparison_denominator: 20, unmatched_episodes: 0, comparison_window_complete: true }],
    })) };
    const html = renderToStaticMarkup(<ResearchResults data={data} onOpenTicker={() => undefined} />);
    expect(html).toContain("Return after actual paper costs, lower estimate: +3.00%");
    expect(html).toContain("20 episodes · window complete · 0 unknown outcomes");
    expect(html).toContain("Completed candidate paper orders with recorded fills and costs");
    expect(html).toContain("Probability error (Brier): 0.200");
  });

  it("leaves an unrated review unknown and preserves explicit negative feedback", () => {
    const unrated = renderToStaticMarkup(<InboxUsefulnessControls itemId="test" />);
    expect(unrated).toContain('aria-label="Review usefulness"');
    expect(unrated.match(/aria-pressed="false"/g)).toHaveLength(2);
    expect(unrated).not.toContain("Feedback saved");
    const negative = renderToStaticMarkup(<InboxUsefulnessControls itemId="test" useful={false} />);
    expect(negative).toMatch(/aria-pressed="true"[^>]*>Not helpful/);
    expect(negative).toContain("Feedback saved");
  });
});
