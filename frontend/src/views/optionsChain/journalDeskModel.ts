import type { OptionsPaperJournalRow } from "@/api/options";

type JournalDeskInput = {
  journal: OptionsPaperJournalRow[];
  journalCount: number;
  shadow: OptionsPaperJournalRow[];
  shadowCount: number;
};

export type JournalDeskModel = {
  paperStatus: string;
  maturePaperOutcomes: number;
  openPaperTrades: number;
  missingPaperMarks: number;
  currentExperiments: number;
  tracking: number;
  awaitingEntry: number;
  marked: number;
  visibleExperiments: OptionsPaperJournalRow[];
};

export function buildJournalDeskModel(input: JournalDeskInput): JournalDeskModel {
  const maturePaperOutcomes = input.journal.filter((row) => ["mature", "expired"].includes(row.lifecycle)).length;
  return {
    paperStatus: input.journalCount ? `${input.journalCount} staged paper trade${input.journalCount === 1 ? "" : "s"}` : "No paper track record yet",
    maturePaperOutcomes,
    openPaperTrades: Math.max(0, input.journalCount - maturePaperOutcomes),
    missingPaperMarks: input.journal.filter((row) => row.missing_mark_gap).length,
    currentExperiments: input.shadowCount,
    tracking: input.shadow.filter((row) => ["entered", "observing"].includes(row.lifecycle)).length,
    awaitingEntry: input.shadow.filter((row) => row.lifecycle === "pending").length,
    marked: input.shadow.filter((row) => row.latest_mark !== null || row.current_return !== null).length,
    visibleExperiments: input.shadow.slice(0, 5),
  };
}

export function observationLabel(row: OptionsPaperJournalRow): string {
  if (row.lifecycle === "pending") return "Awaiting next quote";
  if (["entered", "observing"].includes(row.lifecycle)) return "Tracking path";
  if (["mature", "expired"].includes(row.lifecycle)) return "Outcome recorded";
  if (row.lifecycle === "unfilled") return "Not filled";
  return sentence(row.lifecycle || "research");
}

export function researchBlockerLabel(blocker: string): string {
  if (blocker === "thesis_direction_required") return "Neutral thesis — no directional trade";
  if (blocker === "thesis_upgrade_required") return "Thesis revision required";
  return sentence(blocker);
}

function sentence(value: string): string {
  const text = value.replaceAll("_", " ").trim();
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : "Unknown";
}
