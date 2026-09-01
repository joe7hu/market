import { describe, expect, it } from "vitest";

import { expressionLabel, type ExpressionKind } from "./expression";

describe("expression labels", () => {
  it("maps every backend enum exactly", () => {
    const golden: Record<ExpressionKind, string> = {
      STOCK: "Stock", CALL: "Call", PUT: "Put", DEBIT_SPREAD: "Debit spread",
      CASH_SECURED_PUT: "Cash-secured put", CRYPTO_SPOT: "Crypto spot",
      CRYPTO_PERPETUAL: "Crypto perpetual", CASH: "Cash",
    };
    expect(Object.fromEntries(Object.keys(golden).map((kind) => [kind, expressionLabel(kind as ExpressionKind)]))).toEqual(golden);
  });
});
