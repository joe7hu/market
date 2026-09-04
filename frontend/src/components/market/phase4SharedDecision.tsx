import { buildPortfolioPhase4Decision } from "@/viewModels/portfolioPhase4";
import type { PanelData, ScopeSnapshotStatus } from "@/types";
import { ScopeStatusNotice } from "./scopeStatus";

/** Shared immutable Phase 4 decision gate for every production workspace. */
export function Phase4SharedDecision({
  data,
  scope,
  status,
  onRetry,
}: {
  data: PanelData;
  scope: string;
  status?: ScopeSnapshotStatus;
  onRetry: () => void;
}) {
  const decision = buildPortfolioPhase4Decision(data);
  if (!decision || status?.state === "failed") {
    return (
      <>
        <ScopeStatusNotice status={status} onRetry={onRetry} />
        <div role="alert" className="mb-4 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-700 dark:bg-amber-950/30 dark:text-amber-100">
          Phase 4 shared decision unavailable for {scope}; no allocation or action data is shown.
        </div>
      </>
    );
  }
  return (
    <div data-phase4-allocation={decision.allocationId} className="mb-4 rounded-md border border-border/70 bg-muted/20 px-3 py-2 text-xs text-muted-foreground">
      Shared Phase 4 decision: {decision.status} · {decision.actions.length} actions · cutoff {decision.inputCutoff}
    </div>
  );
}
