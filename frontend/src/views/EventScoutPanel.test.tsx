import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { RowRecord } from "@/types";
import { EventScoutPanel } from "./EventScoutPanel";

describe("EventScoutPanel", () => {
  it("translates stored blocker and action codes at the display boundary", () => {
    const html = renderToStaticMarkup(<EventScoutPanel truths={[{
      lane: "event_scout",
      symbol: "MRNA",
      event_id: "event-1",
      as_of: "2026-09-12T14:00:00Z",
      candidate_state: "SETUP",
      route_verdict: "NO_TRADE",
      primary_blocker: "max_loss_required",
      execution_state: "DISABLED",
      next_action: "wait_for_price_discovery_and_complete_liquidity_inputs",
    } as RowRecord]} />);

    expect(html).toContain("A defined maximum loss is required before this can be considered.");
    expect(html).toContain("Wait for price discovery and complete liquidity inputs before considering a trade.");
    expect(html).toContain("Paused");
    expect(html).not.toContain("max_loss_required");
    expect(html).not.toContain("wait_for_price_discovery_and_complete_liquidity_inputs");
  });
});
