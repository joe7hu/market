import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

const panelScope = vi.hoisted(() => vi.fn());

vi.mock("../hooks", () => ({ usePanelScope: panelScope }));
vi.mock("../marketData", () => ({
  useMarketData: () => ({ data: {}, openTicker: () => undefined }),
}));

import { SourcesRoute } from "./SourcesRoute";

describe("SourcesRoute", () => {
  it("loads only research-source evidence and omits operational diagnostics", () => {
    panelScope.mockClear();

    const html = renderToStaticMarkup(<SourcesRoute />);

    expect(panelScope.mock.calls).toEqual([["sources"]]);
    expect(html).toContain("Source-backed ticker evidence and consensus");
    expect(html).not.toContain("Market diagnostics");
    expect(html).not.toContain("Model diagnostics");
    expect(html).not.toContain("Agent research history");
  });
});
