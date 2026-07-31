import { describe, expect, it } from "vitest";

import { optionsViewSearch } from "./decisionFirst";

describe("options trade desk navigation", () => {
  it("drops the retired research-lens state when changing views", () => {
    const search = optionsViewSearch(
      new URLSearchParams("symbol=SPY&lane=anomaly&tab=desk"),
      "record",
    );

    expect(search.get("tab")).toBe("record");
    expect(search.has("lane")).toBe(false);
    expect(search.get("symbol")).toBe("QQQ");
  });
});
