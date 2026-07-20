import { describe, expect, it } from "vitest";

import type { OptionHistoryCurves } from "@/api";
import { buildOptionCurvePlotData, curveChoices } from "./optionsChainPlotData";

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
});
