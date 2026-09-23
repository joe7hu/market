import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type { PaperPerformance } from "@/api/paper";
import { PaperPerformancePanel } from "./PaperPerformancePanel";

vi.mock("./PaperPerformanceChart", () => ({
  PaperPerformanceChart: ({ points, events, mode }: { points: unknown[]; events: unknown[]; mode: string }) =>
    <div data-testid="paper-chart" data-points={points.length} data-events={events.length} data-mode={mode} />,
}));

const noop = () => undefined;
const render = (performance: PaperPerformance, chart = "cumulative_net_pnl") => renderToStaticMarkup(
  <PaperPerformancePanel performance={performance} chart={chart} points={[]} drawdownPoints={[]}
    onChart={noop} onSelect={noop} onRangeChange={noop} />,
);

describe("paper activity is visible before a realized exit", () => {
  it("passes an entry with unknown P&L to the chart instead of hiding it", () => {
    const html = render({ series: { event_markers: [{ at: "2026-09-22T16:00:00Z", kind: "entry", symbol: "RBLX", trade_id: "trade-1", price: 3.25, quantity: 1, pnl: null }] } } as unknown as PaperPerformance);
    expect(html).toContain('data-testid="paper-chart"');
    expect(html).toContain('data-points="0"');
    expect(html).toContain('data-events="1"');
    expect(html).not.toContain("curve will appear after");
    expect(html).not.toContain("Event list");
  });
  it("still supplies activity in drawdown mode", () => {
    const html = render({ series: { event_markers: [{ at: "2026-09-22T16:00:00Z", kind: "open_position", pnl: null }] } } as unknown as PaperPerformance, "drawdown");
    expect(html).toContain('data-mode="drawdown"');
    expect(html).toContain('data-events="1"');
  });
});
