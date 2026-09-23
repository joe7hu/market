import { describe, expect, it } from "vitest";
import { eventCurveValue, financialLineData, normalizeFinancialPoints, paperEventTooltip, timeExtent, visibleTimeWindow } from "./financialChartModel";

const point = (at: string, value: number | null = 0) => ({ at, value, trade_id: "trade-1" });
const start = Date.parse("2026-09-01T00:00:00Z"), end = Date.parse("2026-09-11T00:00:00Z");

describe("financial chart evidence and time coordinates", () => {
  it("sorts a copy without converting missing or nonfinite P&L to zero", () => {
    const input = [point("2026-09-03", NaN), point("2026-09-01", null), point("2026-09-02", 0)];
    expect(normalizeFinancialPoints(input).map(item => item.value)).toEqual([null, 0, null]);
    expect(input[0].at).toBe("2026-09-03");
  });
  it("rejects invalid time coordinates", () => {
    expect(normalizeFinancialPoints([point("not-a-date"), point("2026-09-01")])).toHaveLength(1);
    expect(timeExtent([point("not-a-date")])).toBeNull();
  });
  it("preserves explicit valuation discontinuities", () => {
    const data = financialLineData([point("2026-09-01", 100), { ...point("2026-09-03", 105), gapBefore: true }]);
    expect(data.map(item => item.value[1])).toEqual([100, null, 105]);
  });
  it("does not reconnect out-of-order NAV segments after sorting", () => {
    const data = financialLineData(normalizeFinancialPoints([
      { ...point("2026-09-03", 100), segment: 0 }, { ...point("2026-09-01", 99), segment: 1 },
    ]));
    expect(data.map(item => item.value[1])).toEqual([99, null, 100]);
  });
  it("maps zoom percentages to elapsed time, not the nearest trade index", () => {
    expect(visibleTimeWindow({ start: 50, end: 75 }, [start, end])).toEqual({ start: start + (end - start) / 2, end: start + (end - start) * .75, from: "2026-09-06", to: "2026-09-08" });
  });
  it("does not confuse a zero-percent end with a full-range end", () => {
    expect(visibleTimeWindow({ start: 0, end: 0 }, [start, end])?.to).toBe("2026-09-01");
  });
  it("uses percentage fallback for null endpoint values, not Unix epoch", () => {
    expect(visibleTimeWindow({ startValue: null, endValue: null, start: 50, end: 100 }, [start, end])?.from).toBe("2026-09-06");
  });
  it("accepts batched timestamp zoom and clamps it to actual history", () => {
    expect(visibleTimeWindow({ batch: [{ startValue: "2026-08-01", endValue: "2026-10-01" }] }, [start, end])).toEqual({ start, end, from: "2026-09-01", to: "2026-09-11" });
  });
  it("rejects inverted windows and handles an empty history", () => {
    expect(visibleTimeWindow({ start: 80, end: 20 }, [start, end])).toBeNull();
    expect(visibleTimeWindow({}, null)).toBeNull();
  });
  it("retains single-observation histories", () => {
    expect(visibleTimeWindow({ start: 20, end: 80 }, [start, start])?.start).toBe(start);
  });
  it("keeps unvalued events off the financial line", () => {
    expect(eventCurveValue({ at: "2026-09-01", kind: "entry", value: 999 }, "cumulative")).toBeNull();
    expect(eventCurveValue({ at: "2026-09-01", kind: "exit", cumulative_net_pnl: 0 }, "cumulative")).toBe(0);
    expect(eventCurveValue({ at: "2026-09-01", kind: "exit", drawdown: NaN }, "drawdown")).toBeNull();
  });
  it("includes symbol, strategy, entry time, price, quantity and P&L in event detail", () => {
    const text = paperEventTooltip({ at: "2026-09-01T15:30:00Z", kind: "entry", symbol: "RBLX", strategy: "Long put", price: 3.25, quantity: 2, pnl: null });
    expect(text).toContain("RBLX · Long put");
    expect(text).toContain("Price: $3.25 · Quantity: 2");
    expect(text).toContain("P&L: not verified");
  });
});
