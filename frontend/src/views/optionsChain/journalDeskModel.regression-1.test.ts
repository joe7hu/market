import { describe, expect, it } from "vitest";

import type { OptionsPaperJournalRow } from "@/api/options";
import { buildJournalDeskModel } from "./journalDeskModel";

describe("learning log research preview", () => {
  it("keeps the default audit bounded when intraday observations accumulate", () => {
    const shadow = Array.from({ length: 50 }, (_, index) => ({
      shadow_id: `shadow-${index}`,
      decision_id: `decision-${index}`,
      lifecycle: "observing",
      latest_mark: 1,
      current_return: 0,
    })) as OptionsPaperJournalRow[];

    const model = buildJournalDeskModel({
      journal: [],
      journalCount: 0,
      shadow,
      shadowCount: 50,
    });

    expect(model.currentExperiments).toBe(50);
    expect(model.visibleExperiments).toHaveLength(5);
  });
});
