import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { MarketStateProjection } from "./panels";

describe("market state evidence projection", () => {
  it("renders backend-owned uncertainty, comparison, and coverage impact fields", () => {
    const html = renderToStaticMarkup(<MarketStateProjection
      snapshotRows={[{
        horizons: {},
        regime_distributions: {
          "1-5 trading days": {
            status: "advisory", method: "unavailable", version: "unavailable",
            sample_count: null, uncertainty: "insufficient point-in-time evidence", distribution: {},
          },
        },
        baseline_challenger: {
          "1-5 trading days": {
            baseline: { status: "unavailable", method: null, version: null, sample_count: null },
            challenger: { status: "unavailable", method: null, version: null, sample_count: null },
          },
        },
      }]}
      coverageRows={[{
        horizon: "1-5 trading days",
        dimension: "equity internals",
        current_status: "available",
        decision_impact: "market_context",
        selected_source: "confirmed_daily_prices",
      }]}
    />);

    expect(html).toContain("insufficient point-in-time evidence");
    expect(html).toContain("baseline: unavailable");
    expect(html).toContain("market_context");
  });
});
