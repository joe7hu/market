import { Bell, Check } from "lucide-react";
import { useMemo, useState } from "react";

import { acknowledgeRadarAlert } from "@/api";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { RowRecord } from "@/types";
import { textField } from "./rowFormat";

type AlertBellProps = {
  alerts: RowRecord[];
  onAcknowledged?: () => Promise<void> | void;
};

const SEVERITY_TONE: Record<string, string> = {
  high: "text-rose-300",
  medium: "text-amber-300",
  low: "text-slate-300",
};

/**
 * Radar alert bell + feed (Phase 4). Surfaces unacknowledged alerts emitted by the
 * fast pass (premium inside buy-under, exceptional-conviction FIRE, OI-flow spikes)
 * and lets the trader acknowledge them inline.
 */
export function AlertBell({ alerts, onAcknowledged }: AlertBellProps) {
  const [open, setOpen] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);

  const openAlerts = useMemo(
    () => alerts.filter((alert) => !textField(alert, ["acknowledged_at"])),
    [alerts],
  );

  async function acknowledge(alertId: string) {
    if (!alertId) return;
    setBusyId(alertId);
    try {
      await acknowledgeRadarAlert(alertId);
      await onAcknowledged?.();
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="relative">
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => setOpen((value) => !value)}
        aria-label={`Alerts (${openAlerts.length} open)`}
      >
        <Bell className="h-4 w-4" />
        {openAlerts.length > 0 && (
          <span className="ml-1 inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-rose-500/90 px-1 text-xs font-semibold text-white">
            {openAlerts.length}
          </span>
        )}
      </Button>
      {open && (
        <div className="absolute right-0 z-20 mt-2 w-80 rounded-md border border-slate-700 bg-slate-900/95 p-2 shadow-xl">
          {openAlerts.length === 0 ? (
            <p className="px-2 py-3 text-sm text-slate-400">No open alerts.</p>
          ) : (
            <ul className="max-h-80 space-y-1 overflow-y-auto">
              {openAlerts.map((alert) => {
                const alertId = textField(alert, ["alert_id"]);
                const severity = textField(alert, ["severity"]) || "low";
                return (
                  <li key={alertId} className="flex items-start gap-2 rounded px-2 py-2 hover:bg-slate-800/60">
                    <div className="min-w-0 flex-1">
                      <p className={cn("text-xs font-semibold uppercase tracking-wide", SEVERITY_TONE[severity] ?? SEVERITY_TONE.low)}>
                        {textField(alert, ["alert_type"]) || "alert"}
                      </p>
                      <p className="truncate text-sm text-slate-200">{textField(alert, ["message"])}</p>
                    </div>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      disabled={busyId === alertId}
                      onClick={() => acknowledge(alertId)}
                      aria-label="Acknowledge alert"
                    >
                      <Check className="h-4 w-4" />
                    </Button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
