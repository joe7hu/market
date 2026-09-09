import { getJson, patchJson } from "../apiTransport";

export type ContinuousAdvisorTicker = {
  symbol: string;
  verdict: {
    thesis?: string;
    countercase?: string;
    forecasts?: Array<{ claim_key?: string; statement?: string; horizon?: string; direction?: string; probability?: number }>;
    invalidations?: Array<Record<string, unknown>>;
    change_since_prior?: string;
    next_review_trigger?: string;
    next_review_at?: string | null;
    outcome_date?: string | null;
    evidence_freshness?: Record<string, string>;
    blockers?: string[];
  };
  provenance: Record<string, unknown>;
};

export type ContinuousAdvisor = {
  enabled: boolean;
  cadence_minutes: number;
  budget_usd: number;
  tickers: ContinuousAdvisorTicker[];
  strategy_health: Record<string, any>;
};

export async function loadContinuousAdvisor(): Promise<ContinuousAdvisor> {
  return getJson<ContinuousAdvisor>("/api/continuous-advisor");
}

export async function updateContinuousAdvisorSettings(payload: { enabled?: boolean; cadence_minutes?: number; budget_usd?: number }): Promise<ContinuousAdvisor> {
  return patchJson<ContinuousAdvisor>("/api/continuous-advisor/settings", payload);
}
