import { type ReactNode } from "react";

import { type RefreshJob } from "@/api";
import { StatusBadge } from "@/components/market/workstation";
import type { RowRecord } from "@/types";
import { displayField, titleLabel, toneFromText } from "@/views/rowFormat";
import { jobDef } from "@/views/health/dataFlow";
import { formatDateTime } from "@/views/health/format";

export function RunStatusTable({ rows: runRows }: { rows: RowRecord[] }) {
  return (
    <table className="w-full min-w-[920px] text-sm">
      <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
        <tr>
          <th className="px-3 py-3">Name</th>
          <th className="px-3 py-3">Capability</th>
          <th className="px-3 py-3">Status</th>
          <th className="px-3 py-3">Started</th>
          <th className="px-3 py-3">Finished</th>
          <th className="px-3 py-3">Detail</th>
        </tr>
      </thead>
      <tbody>
        {runRows.map((row, index) => {
          const status = displayField(row, ["status", "health", "provider_status", "latest_status"], "not_loaded");
          return (
            <tr key={`${displayField(row, ["id", "run_id", "job_name", "provider"], "run")}-${index}`} className="border-b border-border align-top hover:bg-accent/40">
              <td className="max-w-[260px] px-3 py-3 font-medium">{displayField(row, ["job_name", "provider", "source", "id", "run_id"], "run")}</td>
              <td className="px-3 py-3 text-muted-foreground">{displayField(row, ["capability", "source_url", "account_type"], "-")}</td>
              <td className="px-3 py-3"><StatusBadge tone={toneFromText(status)}>{titleLabel(status)}</StatusBadge></td>
              <td className="whitespace-nowrap px-3 py-3 text-muted-foreground">{formatDateTime(displayField(row, ["started_at", "checked_at", "timestamp"], ""))}</td>
              <td className="whitespace-nowrap px-3 py-3 text-muted-foreground">{formatDateTime(displayField(row, ["finished_at", "last_data_at", "updated_at"], ""))}</td>
              <td className="max-w-[460px] px-3 py-3 text-muted-foreground">{displayField(row, ["error", "detail", "message", "failedStep"], "-")}</td>
            </tr>
          );
        })}
        {!runRows.length ? (
          <tr>
            <td colSpan={6} className="px-4 py-6 text-sm text-muted-foreground">No rows available.</td>
          </tr>
        ) : null}
      </tbody>
    </table>
  );
}

export function RefreshHistoryTable({ rows: jobRows }: { rows: RefreshJob[] }) {
  return (
    <table className="w-full min-w-[820px] text-sm">
      <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
        <tr>
          <th className="px-3 py-3">Job</th>
          <th className="px-3 py-3">Status</th>
          <th className="px-3 py-3">Started</th>
          <th className="px-3 py-3">Finished</th>
          <th className="px-3 py-3">Error</th>
        </tr>
      </thead>
      <tbody>
        {jobRows.map((row, index) => (
          <tr key={`${row.id ?? row.job_name}-${index}`} className="border-b border-border align-top hover:bg-accent/40">
            <td className="max-w-[260px] px-3 py-3 font-medium">{jobDef(row.job_name ?? "").label}</td>
            <td className="px-3 py-3"><StatusBadge tone={toneFromText(row.status ?? "")}>{titleLabel(row.status ?? "unknown")}</StatusBadge></td>
            <td className="whitespace-nowrap px-3 py-3 text-muted-foreground">{formatDateTime(row.started_at ?? "")}</td>
            <td className="whitespace-nowrap px-3 py-3 text-muted-foreground">{formatDateTime(row.finished_at ?? "")}</td>
            <td className="max-w-[460px] px-3 py-3 text-muted-foreground">{row.error || "-"}</td>
          </tr>
        ))}
        {!jobRows.length ? (
          <tr>
            <td colSpan={5} className="px-4 py-6 text-sm text-muted-foreground">No refresh jobs recorded.</td>
          </tr>
        ) : null}
      </tbody>
    </table>
  );
}

export function CollapsibleSection({ title, count, children }: { title: string; count: number; children: ReactNode }) {
  return (
    <details className="overflow-hidden rounded-xl border border-border bg-card">
      <summary className="flex cursor-pointer list-none items-center justify-between px-4 py-3 text-lg font-semibold">
        <span>{title}</span>
        <span className="text-sm font-normal text-muted-foreground">{count.toLocaleString()} rows</span>
      </summary>
      <div className="overflow-x-auto border-t border-border">{children}</div>
    </details>
  );
}

export function FragmentRow({ children }: { children: ReactNode }) {
  return <>{children}</>;
}
