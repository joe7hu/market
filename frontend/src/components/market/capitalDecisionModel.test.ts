import { describe, expect, it } from "vitest";
import { capitalDecisionHeadline, capitalDecisionState, type CapitalDecisionProps } from "./capitalDecisionModel";

const props = (plan: Record<string, unknown>) => ({ plan } as unknown as CapitalDecisionProps);

describe("capital decision authority and concise verdicts", () => {
  it("makes a paper-authorized action explicit", () => {
    expect(capitalDecisionHeadline(props({ eligibility: "ACTIONABLE", authorization_mode: "PAPER", action: "BUY" }))).toBe("PAPER — BUY");
  });
  it("does not upgrade advisory authorization to paper", () => {
    expect(capitalDecisionHeadline(props({ eligibility: "ACTIONABLE", authorization_mode: "ADVISORY", action: "EXIT" }))).toBe("ADVISORY — EXIT");
  });
  it("does not present unqualified alpha as an economic decision for cash", () => {
    expect(capitalDecisionHeadline(props({ eligibility: "BLOCKED", authorization_mode: "NONE", primary_blocker: "alpha_strategy_revision_missing" }))).toBe("WAIT — strategy qualification");
  });
  it("retains a real publication outage", () => {
    expect(capitalDecisionHeadline({ missingIsFailure: true })).toBe("TRADING PAUSED — decision publication failed");
  });
  it("does not infer an order from an incomplete plan", () => {
    expect(capitalDecisionHeadline(props({ eligibility: "PENDING", authorization_mode: "NONE" }))).toBe("WAIT — no executable plan");
  });
  it("keeps an unmet entry condition distinct from a missing plan", () => {
    expect(capitalDecisionHeadline(props({ eligibility: "PENDING", authorization_mode: "ADVISORY", action: "WAIT_FOR_PRICE" }))).toBe("WAIT — entry condition not met");
  });
  it("fails closed on inconsistent actionable authorization", () => {
    for (const authorization_mode of ["NONE", "LIVE", "unknown"]) {
      expect(capitalDecisionState(props({ eligibility: "ACTIONABLE", authorization_mode, action: "BUY" }))).toBe("failed");
    }
    expect(capitalDecisionState(props({ eligibility: "ACTIONABLE", authorization_mode: "PAPER", action: "BUY", primary_blocker: "current_price" }))).toBe("failed");
    expect(capitalDecisionState(props({ eligibility: "ACTIONABLE", authorization_mode: "PAPER" }))).toBe("failed");
  });
});
