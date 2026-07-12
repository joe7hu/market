// Page-level union/filter types and constants for the options radar view.

export type OptionThesisAgentRuntime = {
  active: boolean;
  enabled: boolean;
  configured: boolean;
  status: string;
  limit: number;
  requestCap: number;
};

export type CandidateSort = "conviction-desc" | "ticker-asc" | "move-asc" | "premium-asc" | "expiry-asc" | "state";
export type CandidateStateFilter = "all" | "READY" | "SETUP" | "WATCH";
export type CandidateFocus = "top25" | "top-per-ticker" | "all";
export type ThesisFilter = "all" | "needs" | "requested" | "attached";
export type QualityFilter = "all" | "ok" | "caution" | "bad";
export type FamilyFilter = "all" | string;

export const CANDIDATE_PAGE_SIZE = 25;
