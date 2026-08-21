import { describe, expect, it } from "vitest";

import { routeContextFacts } from "./decisionFirst";

describe("decision-first route context", () => {
  it("uses route values first and keeps regime as a fallback", () => {
    expect(routeContextFacts(
      { selected_structure: "call_debit_spread", shadow: true, trend_state: "uptrend", trend_confidence: 0.74, route_blockers: ["paper gate"], paper_quantity_authorized: false, ai_can_override: false },
      { trend_state: "sideways", volatility_state: "elevated", breadth_state: "narrow", quality_status: "good" },
    )).toEqual({
      selected: "call_debit_spread", shadow: true, trend: "uptrend", trendConfidence: 0.74,
      volatility: "elevated", breadth: "narrow", blockers: ["paper gate"],
    });
  });
});
