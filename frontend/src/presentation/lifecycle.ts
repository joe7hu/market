import { humanize, statusMessage } from "./labels";

export type LearningProgress = {
  completed: number;
  required: number;
  remaining: number;
  percent: number;
  label: string;
  detail: string;
  state: "complete" | "collecting" | "unavailable";
};

export function learningProgress(completed: unknown, required: unknown, fallbackRequired = 0): LearningProgress {
  const numericDone = completed == null || completed === "" || typeof completed === "boolean" ? NaN : Number(completed);
  const numericRequired = typeof required === "boolean" ? NaN : Number(required);
  const done = Number.isFinite(numericDone) ? Math.max(0, Math.floor(numericDone)) : 0;
  const floor = Number.isFinite(numericRequired) && numericRequired > 0 ? Math.floor(numericRequired) :
    Number.isFinite(fallbackRequired) ? Math.max(0, Math.floor(fallbackRequired)) : 0;
  const remaining = Math.max(0, floor - done);
  const percent = floor ? Math.min(100, Math.round((done / floor) * 100)) : 0;
  if (!Number.isFinite(numericDone) || numericDone < 0) return { completed: 0, required: floor, remaining: floor, percent: 0, label: "Evidence count unavailable", detail: "A recorded independent outcome count is required; run counts are not evidence.", state: "unavailable" };
  if (!floor) return { completed: done, required: floor, remaining, percent, label: "No evidence yet", detail: "The collection target is not available.", state: "unavailable" };
  if (remaining === 0) return { completed: done, required: floor, remaining, percent: 100, label: `${done} of ${floor} complete`, detail: "The sample-count floor is met; statistical, safety and policy checks still apply.", state: "complete" };
  return { completed: done, required: floor, remaining, percent, label: `${done} of ${floor} complete`, detail: `${remaining} more independent outcome${remaining === 1 ? "" : "s"} needed.`, state: "collecting" };
}

export function laneBlockers(lane: Record<string, any>): string[] {
  const blockers = Array.isArray(lane.blockers) ? lane.blockers : [];
  return blockers.map((blocker) => statusMessage(blocker));
}

export function lifecycleHeadline(lane: Record<string, any>, fallback = "Evidence collection is ready to begin"): string {
  const status = String(lane.status ?? "");
  if (status === "misconfigured") return "Configuration must be fixed before learning can progress";
  if (status === "disabled") return "Learning is paused";
  if (status === "monitoring") return "Monitoring the latest change";
  if (status === "awaiting_human_review") return "Ready for human review";
  if (status === "no_paper_fills") return "Waiting for the first paper fill";
  if (status) return humanize(status);
  return fallback;
}
