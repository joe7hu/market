import { describe, expect, it } from "vitest";

import { criticalDataCoverageTone, navItems } from "./workstation";
import { workerStateLabel } from "./WorkflowReadiness";

describe("primary navigation", () => {
  it("keeps the primary Command Center destinations on desktop and mobile", () => {
    expect(navItems.map(({ label, to }) => [label, to])).toEqual([
      ["Today", "/today"],
      ["Market", "/market"],
      ["Opportunities", "/opportunities"],
      ["Real portfolio", "/portfolio"],
      ["Paper trading", "/portfolio/paper"],
      ["Research", "/research"],
    ]);
  });
});

describe("workflow state labels", () => {
  it("keeps expected no-work runs distinct from failures", () => {
    expect(workerStateLabel({ status: "skipped", summary: { reason: "no_qualified_candidate" } })).toBe("No eligible work");
    expect(workerStateLabel({ status: "skipped", summary: { reason: "provider_capacity_busy" } })).toBe("Skipped");
  });
});

describe("critical data coverage", () => {
  it("does not show an empty source set as good coverage", () => {
    expect(criticalDataCoverageTone([], false)).toBe("warn");
  });
});
