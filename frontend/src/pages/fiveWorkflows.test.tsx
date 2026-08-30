import { describe, expect, it } from "vitest";

import { navItems } from "@/components/market/workstation";

describe("five primary workflows", () => {
  it("exposes the intended workflow destinations", () => {
    expect(navItems.map(({ label, to }) => ({ label, to }))).toEqual([
      { label: "Command Center", to: "/today" },
      { label: "Opportunities", to: "/opportunities" },
      { label: "Portfolio", to: "/portfolio" },
      { label: "Research", to: "/sources" },
      { label: "System", to: "/health" },
    ]);
  });
});
