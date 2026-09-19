import type { ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PanelData, ScopeSnapshotStatus } from "@/types";
import { MarketRoute } from "./MarketRoute";

const fixture = vi.hoisted(() => ({
  data: { dashboard: {} } as PanelData,
  scopeStatus: {} as Record<string, ScopeSnapshotStatus>,
  loadScope: vi.fn().mockResolvedValue(undefined),
}));
vi.mock("@/marketData", () => ({ useMarketData: () => fixture }));
vi.mock("@/hooks", () => ({ usePanelScope: vi.fn() }));
vi.mock("@/views/workspacePage", () => ({
  WorkspacePage: ({ children, actions }: { children: ReactNode; actions: ReactNode }) => <main>{actions}{children}</main>,
}));
vi.mock("@/views/market/panels", () => ({
  MarketEnvironmentPanel: () => <section>Published market evidence</section>,
  ReferenceValuationCharts: () => <section>Published valuation chart</section>,
  MarketAssetMatrix: () => <section>Published asset matrix</section>,
}));
const render = () => renderToStaticMarkup(<MemoryRouter><MarketRoute /></MemoryRouter>);

beforeEach(() => {
  fixture.data = { dashboard: {} } as PanelData;
  fixture.scopeStatus = {};
  fixture.loadScope.mockClear();
});

describe("Market route availability", () => {
  it("shows loading instead of diagnosing missing history before the response", () => {
    const html = render();
    expect(html).toContain("Loading market snapshot");
    expect(html).not.toContain("MISSING_HISTORY");
    expect(html).not.toContain("Published market evidence");
  });
  it("exposes failed reads and a recovery path", () => {
    fixture.scopeStatus.market = { state: "failed", error: "Publication read failed" };
    const html = render();
    expect(html).toContain("Publication read failed");
    expect(html).toContain("Retry snapshot");
    expect(html).toContain('href="/health"');
  });
  it("does not present a successful empty response as a market stance", () => {
    fixture.scopeStatus.market = { state: "ready" };
    const html = render();
    expect(html).toContain("Market snapshot is unavailable");
    expect(html).toContain("not a market stance");
    expect(html).not.toContain("Published valuation chart");
    expect(html).not.toContain("Published asset matrix");
  });
  it("retains available evidence and explicitly identifies partial coverage", () => {
    fixture.scopeStatus.market = { state: "ready" };
    fixture.data.marketEnvironmentAssets = { rows: [{ symbol: "SPY" }] } as PanelData["marketEnvironmentAssets"];
    const html = render();
    expect(html).toContain("Partial market coverage");
    expect(html).toContain("Published asset matrix");
    expect(html).not.toContain("Published valuation chart");
  });
  it("labels retained last-good data rather than making it look current", () => {
    fixture.scopeStatus.market = { state: "stale", error: "Refresh failed", lastGoodAt: "2026-09-18T20:00:00Z" };
    fixture.data.marketEnvironmentAssets = { rows: [{ symbol: "SPY" }] } as PanelData["marketEnvironmentAssets"];
    const html = render();
    expect(html).toContain("Showing stale data");
    expect(html).toContain("Refresh failed");
    expect(html).toContain("Published asset matrix");
  });
});
