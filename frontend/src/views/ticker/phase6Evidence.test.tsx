import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { components } from "@/generated/apiSchema";
import { ExecutionEvidencePanel, TickerMarketEvidence } from "@/views/ticker/panels";

describe("ExecutionEvidencePanel Phase 6 evidence", () => {
  it("renders backend-owned execution fields without rebuilding policy", () => {
    const html = renderToStaticMarkup(<ExecutionEvidencePanel executionEvidence={{
      status: "available",
      version: "execution-grade.v1",
      freshness_status: "available",
      delta: 0.42,
      gamma: 0.08,
      funding: 0.001,
      basis: 0.002,
      blockers: [],
    }} />);

    for (const text of ["available", "execution-grade.v1", "Freshness", "0.42", "0.08", "0.001", "0.002"]) {
      expect(html).toContain(text);
    }
  });

  it("renders a structured blocking state when execution evidence is absent", () => {
    const html = renderToStaticMarkup(<ExecutionEvidencePanel executionEvidence={null} />);

    expect(html).toContain("Field unavailable: execution_evidence");
    expect(html).toContain("Source: ticker_decision_snapshot");
    expect(html).toContain("Reason: execution_evidence_missing");
    expect(html).toContain("This blocks the decision.");
    expect(html).toContain("Refresh execution evidence before placing an order.");
  });

  it("renders a market assessment when omitted default lists are absent", () => {
    const decision = {
      market_evidence_assessment: {
        decision_horizon: "TACTICAL",
        expression_kind: "CALL",
        status: "advisory",
      },
    } as unknown as components["schemas"]["TickerDecisionDetailResponse"];

    const html = renderToStaticMarkup(<TickerMarketEvidence decision={decision} />);

    expect(html).toContain("CALL · TACTICAL");
    expect(html).toContain("required: none");
    expect(html).not.toContain("Blocking:");
  });
});
