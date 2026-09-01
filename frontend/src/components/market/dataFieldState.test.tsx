import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { DataFieldStateNotice, missingFieldState } from "./dataFieldState";

describe("DataFieldStateNotice", () => {
  it("shows source, reason, blocking state, and next action", () => {
    const state = missingFieldState({
      field: "trade_plan",
      source: "trade_plan",
      reason: "trade_plan_identity_mismatch",
      availabilityStatus: "conflicted",
      nextAction: "Refresh the ticker decision.",
    });

    const markup = renderToStaticMarkup(<DataFieldStateNotice state={state} />);

    expect(markup).toContain("Field unavailable: trade_plan");
    expect(markup).toContain("Status: conflicted");
    expect(markup).toContain("Source: trade_plan");
    expect(markup).toContain("Reason: trade_plan_identity_mismatch");
    expect(markup).toContain("This blocks the decision.");
    expect(markup).toContain("Next: Refresh the ticker decision.");
  });
});
