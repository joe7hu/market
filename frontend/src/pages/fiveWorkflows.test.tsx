import { describe, expect, it } from "vitest";

import { navItems } from "@/components/market/workstation";

describe("primary workflows", () => {
  it("exposes the intended workflow destinations", () => {
    expect(navItems.map(({ label, to }) => ({ label, to }))).toEqual([
      { label: "Today", to: "/today" },
      { label: "Market", to: "/market" },
      { label: "Opportunities", to: "/opportunities" },
      { label: "Portfolio", to: "/portfolio/paper" },
      { label: "Research", to: "/research" },
    ]);
  });
});
