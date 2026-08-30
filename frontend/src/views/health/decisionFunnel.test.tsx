import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { DecisionFunnel } from "@/api/panel";
import { DecisionFunnelPanel } from "@/views/health/decisionFunnel";

describe("DecisionFunnelPanel", () => {
  it("renders the backend policy, counts, blocker, owner, and retry", () => {
    const funnel = {
      policy_version: "ticker-opportunity-ranking.v1",
      generated_at: "2026-08-29T14:00:00Z",
      published_at: "2026-08-29T13:59:00Z",
      age_seconds: 60,
      total: 2,
      actionable: 1,
      stages: [{
        stage: "qualified_stock_alpha",
        count: 1,
        total: 2,
        percentage: 0.5,
        unavailable_count: 1,
        affected_symbols: ["BBB"],
        top_blockers: [{ reason: "alpha_oos_evaluation_missing", count: 1, affected_symbols: ["BBB"] }],
        owner: "strategy-governance",
        retry: "Publish a passed OOS ticker-stock-alpha revision.",
      }],
    } satisfies DecisionFunnel;

    const html = renderToStaticMarkup(<DecisionFunnelPanel funnel={funnel} />);

    expect(html).toContain("Backend policy ticker-opportunity-ranking.v1");
    expect(html).toContain("1/2 actionable");
    expect(html).toContain("alpha_oos_evaluation_missing (1)");
    expect(html).toContain("strategy-governance");
    expect(html).toContain("Publish a passed OOS ticker-stock-alpha revision.");
  });
});
