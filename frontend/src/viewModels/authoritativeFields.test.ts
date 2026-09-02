import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { components } from "@/generated/apiSchema";
import { TradePlanCard } from "@/views/TradePlanCard";

describe("authoritative decision fields", () => {
  it("renders stored plan terms instead of deriving replacement values", () => {
    const plan = {
      action: "BUY",
      authorization_mode: "PAPER",
      eligibility: "ACTIONABLE",
      ticker: "NVDA",
      selected_expression_kind: "STOCK",
      selected_expression_identity: "stock:NVDA",
      selected_expression: { kind: "STOCK", identity: "stock:NVDA" },
      entry: { low: 101, high: 102 },
      entry_limit: 101.5,
      cutoff: "2026-08-30T15:42:04Z",
      expiry: "2026-09-30T15:42:04Z",
      quantity: 4,
      max_loss_per_unit: 12.25,
      planned_loss: 49,
      invalidation: { kind: "price", statement: "Close below support", value: 95 },
      profit_exit: { low: 120, high: 125 },
      rationale: "Stored rationale",
    } as unknown as components["schemas"]["TradePlan"];

    const html = renderToStaticMarkup(createElement(TradePlanCard, { plan }));

    expect(html).toContain("NVDA");
    expect(html).toContain("$101.00");
    expect(html).toContain("4");
    expect(html).toContain("Stored rationale");
  });
});
