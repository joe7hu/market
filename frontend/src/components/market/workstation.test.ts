import { describe, expect, it } from "vitest";

import { navItems } from "./workstation";

describe("primary navigation", () => {
  it("keeps the five Command Center destinations on desktop and mobile", () => {
    expect(navItems.map(({ label, to }) => [label, to])).toEqual([
      ["Command Center", "/today"],
      ["Opportunities", "/watchlist"],
      ["Portfolio", "/portfolio"],
      ["Research", "/sources"],
      ["System", "/health"],
    ]);
  });
});
