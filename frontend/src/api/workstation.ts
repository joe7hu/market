import type { components } from "@/generated/apiSchema";
import { getJson } from "@/apiTransport";

export type WorkstationStatus = components["schemas"]["WorkstationStatus"];
export type PaperAccountHistory = components["schemas"]["PaperAccountHistory"];
export function loadWorkstationStatus(signal?: AbortSignal): Promise<WorkstationStatus> {
  return getJson<WorkstationStatus>("/api/workstation/status", signal);
}
export function loadPaperAccountHistory(days = 90, signal?: AbortSignal): Promise<PaperAccountHistory> {
  return getJson<PaperAccountHistory>(`/api/paper/account-history?days=${days}`, signal);
}
