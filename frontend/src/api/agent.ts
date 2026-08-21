/** Advisory agent control-plane requests. */

import type { components } from "../generated/apiSchema";
import { getJson, sendJson } from "../apiTransport";

type ApiSchema = components["schemas"];
type RequiredFields<T> = { [Key in keyof T]-?: T[Key] };

export type AgentOverview = ApiSchema["AgentOverviewResponse"] & {
  queue: NonNullable<ApiSchema["AgentOverviewResponse"]["queue"]>;
  runs: NonNullable<ApiSchema["AgentOverviewResponse"]["runs"]>;
  materialization: NonNullable<ApiSchema["AgentOverviewResponse"]["materialization"]>;
  cost: Omit<NonNullable<ApiSchema["AgentOverviewResponse"]["cost"]>, "today" | "last_7d"> & {
    today: RequiredFields<NonNullable<NonNullable<ApiSchema["AgentOverviewResponse"]["cost"]>["today"]>>;
    last_7d: RequiredFields<NonNullable<NonNullable<ApiSchema["AgentOverviewResponse"]["cost"]>["last_7d"]>>;
  };
};
export type AgentRun = NonNullable<AgentOverview["runs"]>[number];
export type DailyResearchPrompt = ApiSchema["AgentResearchPromptResponse"] & {
  coverage: NonNullable<ApiSchema["AgentResearchPromptResponse"]["coverage"]>;
  freshness: NonNullable<ApiSchema["AgentResearchPromptResponse"]["freshness"]>;
};
export type OptionAgentSettingsInput = ApiSchema["OptionAgentSettingsInput"];

export async function loadAgent(): Promise<AgentOverview> {
  return getJson<AgentOverview>("/api/agent");
}

export async function loadAgentResearchPrompt(): Promise<DailyResearchPrompt> {
  return getJson<DailyResearchPrompt>("/api/agent/research-prompt");
}

export async function analyzeTicker(
  ticker: string,
  prompt?: string,
): Promise<ApiSchema["AgentAnalyzeResponse"]> {
  return sendJson<ApiSchema["AgentAnalyzeResponse"]>("/api/agent/analyze", "POST", { ticker, prompt });
}
