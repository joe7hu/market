import { describe, expect, it } from "vitest";

import { criticalDataCoverageTone, navItems } from "./workstation";

describe("primary navigation", () => {
  it("keeps the primary Command Center destinations on desktop and mobile", () => {
    expect(navItems.map(({ label, to }) => [label, to])).toEqual([
      ["Today", "/today"],
      ["Market", "/market"],
      ["Opportunities", "/opportunities"],
      ["Portfolio", "/portfolio/paper"],
      ["Research", "/research"],
    ]);
  });
});

describe("critical data coverage", () => {
  it("does not show an empty source set as good coverage", () => {
    expect(criticalDataCoverageTone([], false)).toBe("warn");
  });
});
