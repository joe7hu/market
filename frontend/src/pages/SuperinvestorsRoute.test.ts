import { describe, expect, it } from "vitest";

import { selectPortfolio } from "./SuperinvestorsRoute";

describe("selectPortfolio", () => {
  const portfolios = [
    { investor_key: "cik:0001067983", investor: "Berkshire / Buffett" },
    { investor_key: "cik:0001536411", investor: "Stanley Druckenmiller / Duquesne" },
  ];

  it("selects with the stable backend investor key", () => {
    expect(selectPortfolio(portfolios, "cik:0001536411")?.investor).toBe(
      "Stanley Druckenmiller / Duquesne",
    );
  });

  it("falls back to the first loaded investor when no key is selected", () => {
    expect(selectPortfolio(portfolios, null)?.investor).toBe("Berkshire / Buffett");
  });
});
