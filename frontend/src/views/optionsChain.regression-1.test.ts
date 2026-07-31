import { describe, expect, it } from "vitest";

import { chainEvidenceLabel, spreadPercent } from "./optionsChain";

describe("compact option-chain evidence", () => {
  it("turns raw blocker codes into decision-readable evidence", () => {
    expect(chainEvidenceLabel({
      evidence_blockers: ["illiquid_open_interest", "insufficient_eligible_points"],
      evidence_classification: "rejected",
      quality_status: null,
      market_data_status: null,
    })).toBe("Open interest too low · Too few comparable contracts");
  });

  it("reports execution friction as spread over midpoint", () => {
    expect(spreadPercent({ bid: 9, ask: 11 })).toBe("20%");
    expect(spreadPercent({ bid: null, ask: 11 })).toBe("—");
  });
});
