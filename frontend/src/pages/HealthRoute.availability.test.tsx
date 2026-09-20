import type { ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { PanelData, ScopeSnapshotStatus } from "@/types";
import { HealthRoute } from "./HealthRoute";

const fixture = vi.hoisted(() => ({
  data: { dashboard: {} } as PanelData,
  scopeStatus: { health: { state: "ready" } } as Record<string, ScopeSnapshotStatus>,
  loadScope: vi.fn().mockResolvedValue(undefined),
}));
vi.mock("@/marketData", () => ({ useMarketData: () => fixture }));
vi.mock("@/hooks", () => ({ usePanelScope: vi.fn() }));
vi.mock("@/views/workspacePage", () => ({
  WorkspacePage: ({ children, actions }: { children: ReactNode; actions: ReactNode }) => <main>{actions}{children}</main>,
}));
vi.mock("@/views/health/useRefreshJobs", () => ({ useRefreshJobs: () => ({ rows: [], refresh: vi.fn() }) }));
vi.mock("@/views/health/dataFlow", () => ({ buildFlowStages: () => [], DataFlowDiagram: () => <section /> }));
vi.mock("@/views/health/decisionFunnel", () => ({ DecisionFunnelPanel: () => <section /> }));
vi.mock("@/views/health/catalogPanels", () => ({ SourceHealthControlPlane: () => <section /> }));
vi.mock("@/views/health/categoryPanels", () => ({ TopErrorsPanel: () => <section /> }));
vi.mock("@/views/health/tables", () => ({ RefreshHistoryTable: () => <table /> }));
vi.mock("@/views/health/triggerPanels", () => ({ TriggerPanel: () => <section /> }));
vi.mock("@/components/market/WorkflowReadiness", () => ({ WorkflowReadiness: () => <section /> }));
vi.mock("@/components/market/BuildIdentity", () => ({ BuildIdentityCard: () => <section /> }));
vi.mock("@/components/market/scopeStatus", () => ({ ScopeStatusNotice: () => null }));
vi.mock("@/components/market/phase4SharedDecision", () => ({ Phase4SharedDecision: () => <p>Portfolio allocation context unavailable for health</p> }));
vi.mock("./SourcesRoute", () => ({ ResearchAuthorityTable: () => <section /> }));

describe("Health route availability", () => {
  it("does not make Health depend on a portfolio allocation snapshot", () => {
    const html = renderToStaticMarkup(<MemoryRouter><HealthRoute /></MemoryRouter>);
    expect(html).not.toContain("Portfolio allocation context unavailable for health");
  });
});
