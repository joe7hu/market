import { AlertTriangle, Clock3, RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";
import type { ScopeSnapshotStatus } from "@/types";

export function ScopeStatusNotice({ status, onRetry }: { status?: ScopeSnapshotStatus; onRetry: () => void }) {
  if (!status || status.state === "ready" || status.state === "loading") return null;
  const stale = status.state === "stale";
  const timestamp = status.lastGoodAt ? new Date(status.lastGoodAt).toLocaleString() : null;
  return (
    <div role="alert" className={`mb-4 flex flex-wrap items-center justify-between gap-3 rounded-md border p-3 text-sm ${stale ? "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-700 dark:bg-amber-950/30 dark:text-amber-100" : "border-red-300 bg-red-50 text-red-900 dark:border-red-700 dark:bg-red-950/30 dark:text-red-100"}`}>
      <span className="flex items-center gap-2"><>{stale ? <Clock3 className="size-4" /> : <AlertTriangle className="size-4" />}</>{stale ? `Showing stale data${timestamp ? ` from ${timestamp}` : ""}. ${status.error ?? status.message ?? ""}` : `Data unavailable: ${status.error ?? status.message ?? "No valid snapshot exists."}`}</span>
      <Button type="button" size="sm" variant="outline" onClick={onRetry}><RefreshCw className="size-3.5" />Retry</Button>
    </div>
  );
}
