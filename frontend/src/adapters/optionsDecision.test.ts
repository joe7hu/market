import { describe, expect, it } from "vitest";

import { adaptOptionDecision } from "./optionsDecision";

describe("adaptOptionDecision", () => {
  it("normalizes the backend cash-secured-put contract once", () => {
    expect(adaptOptionDecision({
      decision_id: "decision-1",
      ticker: "NVDA",
      structure: "cash_secured_put",
      entry_price: "2.5",
      secured_cash: 12000,
      probability_assignment: "0.31",
    })).toMatchObject({
      key: "decision-1",
      symbol: "NVDA",
      structure: "cash secured put",
      entryPrice: 2.5,
      securedCash: 12000,
      probabilityAssignment: 0.31,
      cashSecured: true,
    });
  });
});
