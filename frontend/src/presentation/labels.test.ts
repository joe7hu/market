import { describe, expect, it } from "vitest";

import { gateLabel, humanize, money, statusMessage, statusLabel } from "./labels";

describe("semantic presentation labels", () => {
  it("translates internal lifecycle values into trader language", () => {
    expect(statusLabel("matched_outcomes_below_promotion_floor")).toBe("Collecting evidence");
    expect(statusLabel("shadow")).toBe("Forward test");
    expect(statusMessage("evidence_validation_failed")).toContain("could not be verified");
    expect(humanize("paper_exit_partial")).toBe("Paper Exit Partial");
    expect(gateLabel("oos_predictive_validity")).toBe("Out-of-sample validity");
  });

  it("keeps signed money readable without changing missing values into zero", () => {
    expect(money(8421, true)).toBe("+$8,421.00");
    expect(money(-340, true)).toBe("-$340.00");
    expect(money(null)).toBe("—");
  });
});
