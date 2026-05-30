import { ExternalLink, Search } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";

import { DataTableFrame, StatusBadge } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import type { RowRecord } from "@/types";
import { displayValue, tickerSymbolFromRow } from "@/utils";
import { symbolList, titleLabel, toneFromText } from "./rowFormat";
import type { OpenTicker } from "./workspacePage";

export function DataGridSection({ title, rows: sectionRows, onOpenTicker }: { title: string; rows: RowRecord[]; onOpenTicker?: OpenTicker }) {
  const [query, setQuery] = useState("");
  const columns = useMemo(() => columnKeys(sectionRows), [sectionRows]);
  const filteredRows = useMemo(() => filterRows(sectionRows, columns, query), [sectionRows, columns, query]);
  const visibleRows = filteredRows.slice(0, 80);

  if (!sectionRows.length) {
    return (
      <DataTableFrame title={<SectionTitle title={title} count={0} />}>
        <div className="px-4 py-6 text-sm text-muted-foreground">No items available.</div>
      </DataTableFrame>
    );
  }

  return (
    <DataTableFrame
      title={<SectionTitle title={title} count={sectionRows.length} />}
      action={
        <div className="flex min-w-0 flex-1 items-center justify-end gap-3">
          <span className="hidden whitespace-nowrap text-xs text-muted-foreground sm:inline" aria-live="polite">
            {filteredRows.length.toLocaleString()} / {sectionRows.length.toLocaleString()}
          </span>
          <div className="relative w-full max-w-64">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              className="pl-8 text-sm"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Filter"
              aria-label={`Filter ${title}`}
            />
          </div>
        </div>
      }
    >
      <table className="w-full min-w-[840px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            {columns.map((column) => <th key={column} className="px-3 py-3">{titleLabel(column)}</th>)}
            {onOpenTicker && <th className="px-3 py-3">Open</th>}
          </tr>
        </thead>
        <tbody>
          {visibleRows.map((row, index) => {
            const symbol = tickerSymbolFromRow(row) || symbolList(row)[0];
            return (
              <tr key={index} className="border-b border-border align-top transition-colors hover:bg-accent/40">
                {columns.map((column) => (
                  <td key={column} className={cn("max-w-[360px] px-3 py-3 leading-6", isPriorityColumn(column) && "font-medium")}>
                    {formatCellContent(column, row[column])}
                  </td>
                ))}
                {onOpenTicker && (
                  <td className="px-3 py-2">
                    {symbol ? <Button type="button" variant="ghost" size="sm" onClick={() => onOpenTicker(symbol)}><ExternalLink /> {symbol}</Button> : <span className="text-muted-foreground">-</span>}
                  </td>
                )}
              </tr>
            );
          })}
          {!visibleRows.length && (
            <tr>
              <td colSpan={columns.length + (onOpenTicker ? 1 : 0)} className="px-4 py-6 text-center text-sm text-muted-foreground">
                No items match "{query.trim()}".
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

function SectionTitle({ title, count }: { title: string; count: number }) {
  return (
    <span className="flex items-center gap-2">
      <span>{title}</span>
      <span className="rounded-md bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground" aria-label={`${count} items`}>
        {count.toLocaleString()}
      </span>
    </span>
  );
}

function formatCellContent(column: string, value: RowRecord[string]): ReactNode {
  if (value === undefined || value === null || value === "") return <span className="text-muted-foreground">-</span>;

  if (isScoreColumn(column)) {
    const numeric = numericValue(value);
    if (numeric !== null) return <ScoreCell value={numeric} display={formatCellValue(column, value)} />;
  }

  if (isToneColumn(column)) {
    const label = formatCellValue(column, value);
    return <StatusBadge tone={toneForCell(column, label)}>{label}</StatusBadge>;
  }

  if (isSymbolColumn(column)) {
    return <span className="font-semibold tracking-normal text-foreground">{formatCellValue(column, value)}</span>;
  }

  if (isDateColumn(column)) {
    return <span className="whitespace-nowrap text-muted-foreground">{formatCellValue(column, value)}</span>;
  }

  return formatCellValue(column, value);
}

function ScoreCell({ value, display }: { value: number; display: string }) {
  const normalized = normalizeScore(value);
  const tone = value >= 70 || (value > 0 && value <= 1 && value >= 0.7) ? "good" : value >= 40 || (value > 0 && value <= 1 && value >= 0.4) ? "warn" : "muted";
  return (
    <div className="min-w-28">
      <div className="mb-1 flex items-center justify-between gap-3">
        <span className="font-medium tabular-nums">{display}</span>
        <span className={cn("size-1.5 shrink-0 rounded-full", tone === "good" ? "bg-green-600" : tone === "warn" ? "bg-amber-500" : "bg-muted-foreground")} />
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-muted">
        <div
          className={cn("h-full rounded-full", tone === "good" ? "bg-green-600" : tone === "warn" ? "bg-amber-500" : "bg-muted-foreground")}
          style={{ width: `${normalized}%` }}
        />
      </div>
    </div>
  );
}

function formatCellValue(column: string, value: RowRecord[string]): string {
  if (value === undefined || value === null || value === "") return "-";
  if (typeof value === "string") {
    if (isDateColumn(column)) {
      const date = new Date(value);
      if (!Number.isNaN(date.getTime())) {
        return date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
      }
    }
    if (isToneColumn(column)) {
      return titleLabel(value);
    }
  }
  return displayValue(value);
}

function filterRows(sectionRows: RowRecord[], columns: string[], query: string): RowRecord[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return sectionRows;
  return sectionRows.filter((row) => {
    const symbol = tickerSymbolFromRow(row) || symbolList(row).join(" ");
    const haystack = [symbol, ...columns.map((column) => formatCellValue(column, row[column]))].join(" ").toLowerCase();
    return haystack.includes(needle);
  });
}

function isDateColumn(column: string): boolean {
  return ["as_of", "updated_at", "created_at", "checked_at", "last_run_at", "timestamp", "filed_at"].includes(column) || column.endsWith("_at");
}

function isToneColumn(column: string): boolean {
  return ["status", "decision", "decision_bucket", "action_grade", "action_type", "risk_type", "severity", "health", "freshness_state", "needs_review", "blocker", "conviction", "confidence", "grade"].includes(column);
}

function isSymbolColumn(column: string): boolean {
  return ["symbol", "ticker"].includes(column);
}

function isPriorityColumn(column: string): boolean {
  return ["symbol", "ticker", "name", "title", "decision", "next_action"].includes(column);
}

function isScoreColumn(column: string): boolean {
  const normalized = column.toLowerCase();
  return normalized.includes("score") || normalized.includes("confidence") || normalized.includes("strength") || normalized.includes("conviction");
}

function numericValue(value: RowRecord[string]): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const parsed = Number(value.trim().replace(/[$,%_,]/g, ""));
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function normalizeScore(value: number): number {
  if (value > 0 && value <= 1) return Math.max(4, Math.min(100, value * 100));
  return Math.max(4, Math.min(100, value));
}

function toneForCell(column: string, value: string) {
  const combined = `${column} ${value}`;
  if (column === "needs_review" && ["yes", "true"].includes(value.toLowerCase())) return "warn";
  if (column === "blocker" && value !== "-" && value.toLowerCase() !== "none") return "bad";
  return toneFromText(combined);
}

function columnKeys(sectionRows: RowRecord[]): string[] {
  const preferred = ["symbol", "ticker", "name", "status", "decision", "score", "rank", "reason", "next_action", "source", "as_of", "updated_at"];
  const discovered = new Set<string>();
  for (const row of sectionRows.slice(0, 12)) {
    Object.keys(row).forEach((key) => discovered.add(key));
  }
  const ordered = preferred.filter((key) => discovered.has(key));
  const extras = [...discovered].filter((key) => !ordered.includes(key)).slice(0, Math.max(0, 8 - ordered.length));
  return [...ordered, ...extras].slice(0, 8);
}
