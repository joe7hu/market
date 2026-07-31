import { describe, expect, it } from "vitest";

import {
  CHAIN_PAGE_SIZE,
  SURFACE_MAX_DTE,
  SURFACE_MONEYNESS_BOUND,
  chainSelectionReady,
  chainEvidenceLabel,
  spreadPercent,
} from "./optionsChain";

describe("compact option-chain evidence", () => {
  it("keeps the default drill-down to one scannable page", () => {
    expect(CHAIN_PAGE_SIZE).toBe(10);
  });

  it("bounds the default volatility surface to the tradable decision zone", () => {
    expect(SURFACE_MONEYNESS_BOUND).toBe(0.06);
    expect(SURFACE_MAX_DTE).toBe(90);
  });

  it("does not fetch a broad chain before expiry and type resolve", () => {
    expect(chainSelectionReady(876, "", "")).toBe(false);
    expect(chainSelectionReady(876, "2026-08-28", "call")).toBe(true);
  });

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
