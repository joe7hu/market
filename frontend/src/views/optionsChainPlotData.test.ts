import { describe, expect, it } from "vitest";

import type { OptionHistoryCurves, OptionHistorySurfaceGrid } from "@/api";
import { buildOptionCurvePlotData, buildProviderIVSurfaceData, curveChoices } from "./optionsChainPlotData";

const curves: OptionHistoryCurves = {
  snapshot_id: 1,
  history_state: "collecting",
  smiles: [
    { expiration: "2026-08-21", option_type: "call", points: [{ moneyness: 0.1, iv: 0.22, strike: 510 }, { moneyness: -0.1, iv: 0.24, strike: 490 }] },
    { expiration: "2026-08-21", option_type: "put", points: [{ moneyness: 0.1, iv: 0.28, strike: 510 }, { moneyness: -0.1, iv: 0.30, strike: 490 }] },
  ],
  term_structure: [
    { expiration: "2026-09-18", option_type: "put", dte: 60, atm_iv: 0.27 },
    { expiration: "2026-08-21", option_type: "call", dte: 32, atm_iv: 0.25 },
    { expiration: "2026-09-18", option_type: "call", dte: 60, atm_iv: 0.26 },
    { expiration: "2026-08-21", option_type: "put", dte: 32, atm_iv: 0.26 },
  ],
  history: [
    { slot_at: "2026-07-20T11:30:00-04:00", expiration: "2026-08-21", option_type: "put", atm_iv: 0.27 },
    { slot_at: "2026-07-20T11:15:00-04:00", expiration: "2026-08-21", option_type: "put", atm_iv: 0.26 },
    { slot_at: "2026-07-20T11:30:00-04:00", expiration: "2026-08-21", option_type: "call", atm_iv: 0.25 },
  ],
};

describe("option-curve plot data", () => {
  it("keeps call and put term structures as separate sorted traces", () => {
    const plots = buildOptionCurvePlotData(curves, "2026-08-21:put");
    expect(plots.term).toHaveLength(2);
    expect(plots.term[0]?.x).toEqual([32, 60]);
    expect(plots.term[1]?.x).toEqual([32, 60]);
  });

  it("uses one selected expiry/type for the smile and historical IV", () => {
    const plots = buildOptionCurvePlotData(curves, "2026-08-21:put");
    expect(plots.smile?.x).toEqual([-0.1, 0.1]);
    expect(plots.history?.x).toEqual(["2026-07-20T11:15:00-04:00", "2026-07-20T11:30:00-04:00"]);
    expect(curveChoices(curves).map((choice) => choice.key)).toEqual(["2026-08-21:call", "2026-08-21:put"]);
  });

  it("keeps the 3D grid's no-data gaps and exposes only selected-type provider observations", () => {
    const surface: OptionHistorySurfaceGrid = {
      snapshot_id: 1, symbol: "QQQ", x: [-0.1, 0, 0.1], y: [20],
      surfaces: { call: [[0.24, null, 0.21]], put: [[0.30, 0.31, 0.29]] },
      observed: [
        { option_type: "call", log_moneyness: -0.1, dte: 20, provider_iv: 0.24, strike: 490 },
        { option_type: "put", log_moneyness: -0.1, dte: 20, provider_iv: 0.30, strike: 490 },
      ],
    };
    const plot = buildProviderIVSurfaceData(surface, "call");
    expect(plot.z).toEqual([[0.24, null, 0.21]]);
    expect(plot.observed).toEqual([{ logMoneyness: -0.1, dte: 20, providerIV: 0.24, strike: 490 }]);
  });
});
