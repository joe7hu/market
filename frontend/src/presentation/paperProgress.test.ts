import { describe, expect, it } from "vitest";
import { paperObservationProgress } from "./paperProgress";

describe("paper observation progress", () => {
  it("explains the screenshot's all-terminal population without claiming a worker diagnosis", () => {
    const result = paperObservationProgress({ unfilled: 423, rejected: 569 });
    expect(result.title).toBe("No completed observations");
    expect(result.detail).toContain("423 unfilled · 569 rejected");
    expect(result.detail).toContain("do not establish whether");
    expect(result.attention).toBe(true);
  });
  it("does not call an outstanding population stalled", () => {
    expect(paperObservationProgress({ pending: 2, rejected: 569 }).title).toContain("2 awaiting entry");
  });
  it("shows open observations without claiming completed outcomes", () => {
    expect(paperObservationProgress({ entered: 3 }).title).toContain("3 open observations");
  });
  it("keeps completed observations separate from funded performance", () => {
    const result = paperObservationProgress({ closed: 8, unmeasurable: 1 });
    expect(result.title).toBe("8 completed observations");
    expect(result.detail).toContain("excluded from funded paper-account P&L");
    expect(result.attention).toBe(true);
  });
  it("does not hide unmeasurable observations", () => {
    expect(paperObservationProgress({ unmeasurable: 4 }).detail).toContain("4 unmeasurable");
  });
  it("distinguishes an empty population", () => {
    expect(paperObservationProgress({}).title).toBe("No observations recorded");
  });
  it.each([NaN, Infinity, -1, 0.5])("rejects malformed counts: %s", (value) => {
    expect(paperObservationProgress({ closed: value }).title).toBe("Observation counts need attention");
  });
});


import { paperValuationDescription } from "./paperProgress";
it("labels retained last-session prices without claiming a live quote or changing their timestamp", () => {
  const text = paperValuationDescription({ mark_status: "verified", mark_stale: false, mark_observed_at: "2026-09-18T20:00:00Z", mark: { session_state: "last_completed_session" } });
  expect(text).toContain("not a live quote or fill authorization");
  expect(text).toContain("2026-09-18T20:00:00Z");
  expect(paperValuationDescription({ mark_status: "verified", mark_stale: true })).toContain("unavailable");
});
