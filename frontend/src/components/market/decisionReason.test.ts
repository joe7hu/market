import { describe, expect, it } from "vitest";
import { decisionReason } from "./dataFieldState";

describe("legacy missing-plan explanations", () => {
  it("does not present either legacy fallback as cash outperformance", () => {
    expect(decisionReason("Cash is selected because the current trade plan is unavailable: trade_plan_missing.")).toBe("No executable trade plan.");
    expect(decisionReason("Cash is selected because the current opportunity rank is unavailable: alpha_strategy_revision_missing.")).toBe("No qualified stock signal yet.");
  });
  it("keeps a genuine economic cash comparison intact", () => {
    expect(decisionReason("Cash offers the better after-cost return.")).toBe("Cash offers the better after-cost return.");
  });
});
