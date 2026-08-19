import { describe, expect, it } from "vitest";

import { candidateDecisionFacts } from "./candidateTable";
import { emptySignalBlocker, marketStateFacts } from "./signalBrief";

describe("radar market-state explanations", () => {
  it("projects the compact market state and bounded candidate changes", () => {
    const facts = marketStateFacts({
      market_state: { trend_state: "risk_on", kaufman_er_20d: 0.35, volatility_state: "contained", breadth_state: "broad", as_of: "2026-08-19T14:00:00Z", quality_status: "good" },
      candidate_changes: ["QQQ added", "SPY retained", "IWM removed", "ignored"],
    });

    expect(facts).toMatchObject({ direction: "risk_on", efficiencyRatio: "0.35", volatility: "contained", breadth: "broad", quality: "good" });
    expect(facts.changes).toEqual(["QQQ added", "SPY retained", "IWM removed"]);
  });

  it("keeps decision explanations focused on ticker, structure, blocker, and change", () => {
    expect(candidateDecisionFacts({
      why_ticker: "Relative strength improved",
      why_structure: "Defined-risk upside",
      blockers: ["Quote age must refresh"],
      candidate_change: "new",
    })).toEqual({
      whyTicker: "Relative strength improved",
      whyStructure: "Defined-risk upside",
      blocker: "Quote age must refresh",
      change: "New",
    });
  });

  it("does not attribute a different shadow route to the displayed structure", () => {
    const facts = candidateDecisionFacts({
      structure: "long_call",
      strategy_route: {
        selected_structure: "call_debit_spread",
        selection_reasons: ["High IV favors a defined-risk spread"],
      },
    });
    expect(facts.whyStructure).toBe("Candidate is Long Call; shadow route is Call Debit Spread");
  });

  it("states the qualification failure when the shortlist is empty", () => {
    expect(emptySignalBlocker([])).toBe("No contract passed the full qualification gates.");
    expect(emptySignalBlocker([{ticker: "QQQ"}])).toBeNull();
  });
});
