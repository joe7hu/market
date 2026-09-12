import type { components } from "../generated/apiSchema";
import { getJson } from "../apiTransport";

type ApiSchema = components["schemas"];

export type ResearchStrategyPage = ApiSchema["ResearchStrategyPage"] & { rows: Array<Record<string, any>> };
export type ResearchClaimPage = ApiSchema["ResearchClaimPage"] & { rows: Array<Record<string, any>>; quality?: Record<string, any> };
export type ResearchPromptPage = ApiSchema["ResearchPromptPage"] & { rows: Array<Record<string, any>> };
export type ResearchExperimentPage = ApiSchema["ResearchExperimentPage"] & { rows: Array<Record<string, any>> };
export type ResearchEventPage = ApiSchema["ResearchEventPage"] & { rows: Array<Record<string, any>> };

export function loadResearchStrategies(signal?: AbortSignal, cursor?: string | null): Promise<ResearchStrategyPage> {
  const suffix = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
  return getJson<ResearchStrategyPage>(`/api/research/strategies${suffix}`, signal);
}

export function loadResearchStrategy(revisionId: string, signal?: AbortSignal): Promise<Record<string, any>> {
  return getJson<Record<string, any>>(`/api/research/strategies/${encodeURIComponent(revisionId)}`, signal);
}

export function loadResearchPredictions(signal?: AbortSignal, cursor?: string | null): Promise<ResearchClaimPage> {
  const suffix = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
  return getJson<ResearchClaimPage>(`/api/research/predictions${suffix}`, signal);
}

export function loadResearchPrediction(claimId: string, signal?: AbortSignal): Promise<Record<string, any>> {
  return getJson<Record<string, any>>(`/api/research/predictions/${encodeURIComponent(claimId)}`, signal);
}

export function loadResearchPrompts(signal?: AbortSignal): Promise<ResearchPromptPage> {
  return getJson<ResearchPromptPage>("/api/research/prompts", signal);
}

export function loadResearchPrompt(version: string, signal?: AbortSignal): Promise<Record<string, any>> {
  return getJson<Record<string, any>>(`/api/research/prompts/${encodeURIComponent(version)}`, signal);
}

export function loadResearchExperiments(signal?: AbortSignal, cursor?: string | null): Promise<ResearchExperimentPage> {
  const suffix = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
  return getJson<ResearchExperimentPage>(`/api/research/experiments${suffix}`, signal);
}

export function loadResearchExperiment(experimentId: string, signal?: AbortSignal): Promise<Record<string, any>> {
  return getJson<Record<string, any>>(`/api/research/experiments/${encodeURIComponent(experimentId)}`, signal);
}

export function loadResearchEvents(signal?: AbortSignal): Promise<ResearchEventPage> {
  return getJson<ResearchEventPage>("/api/research/events", signal);
}
