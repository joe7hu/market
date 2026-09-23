import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type { components } from "@/generated/apiSchema";
import { ReferenceSignalCard, visibleReferenceSignal } from "./ReferenceSignalCard";

type Signal = components["schemas"]["ReferenceSignal"];
const signal: Signal = {
  version: "daily-trend-conditions.v1", evidence_kind: "measured_trend_not_validated_alpha",
  horizon: "Tactical · next 1–20 sessions",
  signal_id: "recorded-trend-1", ticker: "XYZ", action: "BUY_SETUP",
  summary: "Uptrend supported by the completed session.", condition: "Enter only inside the published band.",
  as_of: "2026-09-20T20:00:00Z", expires_at: "2026-09-21T13:30:00Z", quote_state: "closed_reference",
  quote_observed_at: "2026-09-18T20:00:00Z", feature_session: "2026-09-18", source_revision: "feature-1",
  entry_low: 99, entry_high: 101, stop_price: 95, target_price: 112, risk_per_unit: 6,
  order_authorized: false,
};
afterEach(() => vi.useRealTimers());

describe("published trading conditions", () => {
  it("shows explicit conditions without requiring navigation context or inventing an allocation", () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date("2026-09-20T20:01:00Z"));
    const html = renderToStaticMarkup(<ReferenceSignalCard signal={signal} />);
    expect(html).toContain("Conditional long setup");
    for (const label of ["Entry condition", "Price invalidation", "Price objective", "Planned risk / unit"]) expect(html).toContain(label);
    expect(html).toContain("US venue closed");
    expect(html).toContain("WAIT — no executable plan");
    expect(html).not.toContain("Unknown");
    expect(html).not.toContain("Review the evidence");
  });
  it("revokes expired cached terms instead of waiting for a server reload", () => {
    const current = visibleReferenceSignal(signal, Date.parse(signal.expires_at));
    expect(current?.action).toBe("SERVICE_FAILURE");
    expect(current?.entry_low).toBeNull(); expect(current?.target_price).toBeNull();
    expect(signal.entry_low).toBe(99); // immutable evidence is not rewritten
  });
  it("rejects future publication clocks and invalid clocks", () => {
    expect(visibleReferenceSignal(signal, Date.parse(signal.as_of) - 1)?.action).toBe("SERVICE_FAILURE");
    expect(visibleReferenceSignal({ ...signal, expires_at: "bad" }, Date.parse(signal.as_of))?.action).toBe("SERVICE_FAILURE");
  });
  it("names a failed producer and routes repair to Health rather than a no-trade signal", () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date("2026-09-20T20:01:00Z"));
    const html = renderToStaticMarkup(<ReferenceSignalCard signal={{ ...signal, action: "SERVICE_FAILURE", entry_low: null,
      entry_high: null, stop_price: null, target_price: null, risk_per_unit: null,
      failure_code: "history_incomplete", owner_job: "update_market_data", summary: "Completed candle missing." }} />);
    expect(html).toContain("SIGNAL SERVICE FAILED"); expect(html).toContain("update_market_data");
    expect(html).toContain('href="/health"'); expect(html).not.toContain("Entry condition");
  });
  it("only displays paper qualification from the authoritative trade plan", () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date("2026-09-20T20:01:00Z"));
    const html = renderToStaticMarkup(<ReferenceSignalCard signal={signal} plan={{ eligibility: "ACTIONABLE", authorization_mode: "PAPER", action: "BUY" }} />);
    expect(html).toContain("PAPER — BUY"); expect(html).toContain("Fill-time quote and risk checks still apply.");
    expect(signal.order_authorized).toBe(false);
  });
  it("removes cached actionable headlines when the signal service expires", () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date(signal.expires_at));
    const html = renderToStaticMarkup(<ReferenceSignalCard signal={signal} plan={{ eligibility: "ACTIONABLE", authorization_mode: "PAPER", action: "BUY" }} />);
    expect(html).toContain("SIGNAL SERVICE FAILED");
    expect(html).toContain("New allocations paused");
    expect(html).not.toContain("PAPER — BUY");
    expect(html).not.toContain("Paper-qualified terms published");
    expect(html).not.toContain("Entry condition");
  });

});
