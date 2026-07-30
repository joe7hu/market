import { describe, expect, it } from "vitest";

import type { PanelData } from "@/types";
import { buildThesisMonitorViewModel } from "./thesisMonitor";

describe("thesis monitor view model", () => {
  it("keeps core options-underwriting gaps in their own visible lane", () => {
    const data = {
      thesisMonitor: {
        count: 1,
        rows: [{
          symbol: "QQQ",
          priority_lane: "Options Underwriting Gaps",
          options_underwriting: true,
          needs_review: true,
        }],
      },
    } as unknown as PanelData;

    const viewModel = buildThesisMonitorViewModel(data);

    expect(viewModel.optionsUnderwriting.map((row) => row.symbol)).toEqual(["QQQ"]);
  });
});
