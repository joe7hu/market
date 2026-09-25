import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

const panelScope = vi.hoisted(() => vi.fn());

vi.mock("../hooks", () => ({ usePanelScope: panelScope }));
vi.mock("../marketData", () => ({
  useMarketData: () => ({ data: {}, openTicker: () => undefined }),
}));

import { SourcesRoute, ResearchAuthorityTable } from "./SourcesRoute";
import type { PanelData } from "@/types";

describe("SourcesRoute", () => {
  it("loads only research-source evidence and omits operational diagnostics", () => {
    panelScope.mockClear();

    const html = renderToStaticMarkup(<SourcesRoute />);

    expect(panelScope.mock.calls).toEqual([["sources"]]);
    expect(html).toContain("Source-backed ticker evidence and consensus");
    expect(html).not.toContain("Research Authority (read-only)");
    expect(html).not.toContain("Market diagnostics");
    expect(html).not.toContain("Model diagnostics");
    expect(html).not.toContain("Agent research history");
  });
});

describe("Research authority request lifecycle", () => {
  it.each([undefined, { state: "loading" as const }])("does not turn an unfinished request into empty authority", (status) => {
    const html = renderToStaticMarkup(<ResearchAuthorityTable data={{} as PanelData} status={status} />);
    expect(html).toContain("Loading research authority");
    expect(html).not.toContain("No research authority rows");
  });
  it("distinguishes a failed fetch from a successful empty response", () => {
    const failed = renderToStaticMarkup(<ResearchAuthorityTable data={{} as PanelData} status={{ state: "failed" }} />);
    expect(failed).toContain("Research authority could not be loaded");
    expect(failed).not.toContain("No research authority rows");
    const empty = renderToStaticMarkup(<ResearchAuthorityTable data={{} as PanelData} status={{ state: "ready" }} />);
    expect(empty).toContain("No research authority rows");
  });
  it("retains loaded records during a refresh while draft gates remain empty", () => {
    const data = { researchHypotheses: { rows: [{ hypothesis_key: "verified-hypothesis", status: "draft" }] }, researchValidationGates: { rows: [] } } as unknown as PanelData;
    const html = renderToStaticMarkup(<ResearchAuthorityTable data={data} status={{ state: "loading" }} />);
    expect(html).toContain("verified-hypothesis");
    expect(html).not.toContain("No research authority rows");
  });
});
