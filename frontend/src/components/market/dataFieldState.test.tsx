import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { DataFieldStateNotice, missingFieldState } from "./dataFieldState";

describe("decision evidence notice", () => {
  it("explains a blocking mismatch without exposing diagnostics", () => {
    const state = missingFieldState({ field: "trade_plan", source: "trade_plan", reason: "trade_plan_identity_mismatch", availabilityStatus: "conflicted", nextAction: "Refresh the ticker decision." });
    const markup = renderToStaticMarkup(<DataFieldStateNotice state={state} />);
    expect(markup).toContain("refer to different evidence");
    expect(markup).toContain("A new trade cannot be assessed");
    expect(markup).toContain("Refresh the ticker decision.");
    expect(markup).not.toMatch(/Field unavailable|Status:|Source:|trade_plan/);
    for (const availability_status of ["available", "not_applicable"] as const) {
      expect(renderToStaticMarkup(<DataFieldStateNotice state={{ ...state, availability_status }} />)).toBe("");
    }
  });
});
