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
  const done = Math.max(0, Number(completed) || 0);
  const floor = Math.max(0, Number(required) || fallbackRequired);
  const remaining = Math.max(0, floor - done);
  const percent = floor ? Math.min(100, Math.round((done / floor) * 100)) : 0;
  if (!floor) return { completed: done, required: floor, remaining, percent, label: "No evidence yet", detail: "The collection target is not available.", state: "unavailable" };
  if (remaining === 0) return { completed: done, required: floor, remaining, percent: 100, label: `${done} of ${floor} complete`, detail: "This evidence gate has enough observations.", state: "complete" };
  return { completed: done, required: floor, remaining, percent, label: `${done} of ${floor} complete`, detail: `${remaining} more independent outcome${remaining === 1 ? "" : "s"} needed.`, state: "collecting" };
}

export function laneBlockers(lane: Record<string, any>): string[] {
  const blockers = Array.isArray(lane.blockers) ? lane.blockers : [];
  return blockers.map((blocker) => statusMessage(blocker));
}

export function lifecycleHeadline(lane: Record<string, any>, fallback = "Evidence collection is ready to begin"): string {
  const status = String(lane.status ?? "");
  if (status === "disabled") return "Learning is paused";
  if (status === "monitoring") return "Monitoring the latest change";
  if (status === "awaiting_human_review") return "Ready for human review";
  if (status === "no_paper_fills") return "Waiting for the first paper fill";
  if (status) return humanize(status);
  return fallback;
}
