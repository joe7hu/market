// Radar summary strip + alert panel.

import {AlertTriangle, CheckCircle2, Loader2 } from "lucide-react";
import {StatusBadge } from "@/components/market/workstation";
import {Button } from "@/components/ui/button";
import {cn } from "@/lib/utils";
import {RowRecord } from "@/types";
import {Tone } from "@/ui/tone";
import {displayField, textField, titleLabel } from "../rowFormat";
import {toneText } from "../optionsRadarTone";

export function RadarSummaryStrip({
  opportunityCount,
  opportunityTickerCount,
  scannedTickerCount,
  fireCount,
  setupCount,
  exceptionalCount,
  researchCount,
  repairCount,
  groupedOpportunityCount,
}: {
  opportunityCount: number;
  opportunityTickerCount: number;
  scannedTickerCount: number;
  fireCount: number;
  setupCount: number;
  exceptionalCount: number;
  researchCount: number;
  repairCount: number;
  groupedOpportunityCount: number;
}) {
  const items: Array<[string, string, Tone]> = [
    ["Trade-Ready", exceptionalCount.toLocaleString(), exceptionalCount ? "good" : "muted"],
    ["Research", researchCount.toLocaleString(), researchCount ? "info" : "muted"],
    ["Data Blocked", repairCount.toLocaleString(), repairCount ? "bad" : "good"],
    ["Coverage", `${scannedTickerCount.toLocaleString()} scanned / ${opportunityTickerCount.toLocaleString()} tickers`, scannedTickerCount >= 20 ? "good" : scannedTickerCount ? "warn" : "muted"],
  ];
  return (
    <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
      {items.map(([label, value, tone]) => (
        <div key={label} className="rounded-md border border-border bg-card px-3 py-2">
          <div className="text-[10px] font-semibold uppercase text-muted-foreground">{label}</div>
          <div className={cn("mt-1 text-sm font-semibold tabular-nums", toneText(tone))}>{value}</div>
        </div>
      ))}
    </div>
  );
}

export function RadarAlertPanel({
  alerts,
  acknowledgingAlert,
  onAcknowledge,
}: {
  alerts: RowRecord[];
  acknowledgingAlert: string | null;
  onAcknowledge: (alertId: string) => void;
}) {
  if (!alerts.length) return null;
  return (
    <section className="rounded-md border border-border bg-card p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <AlertTriangle className="size-4 text-amber-600" />
          <h2 className="text-sm font-semibold">Active Radar Alerts</h2>
          <StatusBadge tone="warn">{alerts.length.toLocaleString()}</StatusBadge>
        </div>
      </div>
      <div className="mt-3 grid gap-2 lg:grid-cols-2">
        {alerts.slice(0, 6).map((alert) => {
          const alertId = textField(alert, ["alert_id"]);
          const severity = textField(alert, ["severity"], "info").toLowerCase();
          const tone: Tone = severity === "critical" ? "bad" : severity === "warning" ? "warn" : "info";
          return (
            <div key={alertId || `${textField(alert, ["ticker"])}-${textField(alert, ["alert_type"])}`} className="flex min-w-0 items-start justify-between gap-3 rounded-md border border-border/70 bg-background px-3 py-2">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-1.5">
                  <StatusBadge tone={tone}>{titleLabel(textField(alert, ["alert_type"], "alert"))}</StatusBadge>
                  <span className="text-sm font-semibold">{displayField(alert, ["title"])}</span>
                </div>
                <p className="mt-1 text-xs leading-5 text-muted-foreground">{displayField(alert, ["detail"])}</p>
              </div>
              <Button type="button" variant="outline" size="sm" className="h-8 shrink-0" disabled={!alertId || acknowledgingAlert === alertId} onClick={() => onAcknowledge(alertId)}>
                {acknowledgingAlert === alertId ? <Loader2 className="size-4 animate-spin" /> : <CheckCircle2 className="size-4" />}
                <span>Ack</span>
              </Button>
            </div>
          );
        })}
      </div>
    </section>
  );
}

