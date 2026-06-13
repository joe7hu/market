// Shared presentational primitives for the options radar view. Small, generic
// building blocks (table head/cell, ticker button, metric pill, truncation
// wrappers) used across the radar's component files.

import { type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import { type OpenTicker } from "./workspacePage";

export function SectionTitle({ title, count }: { title: string; count: number }) {
  return (
    <span className="flex items-center gap-2">
      <span>{title}</span>
      <span className="rounded-md bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground" aria-label={`${count} rows`}>
        {count.toLocaleString()}
      </span>
    </span>
  );
}

export function TickerButton({ ticker, onOpenTicker }: { ticker: string; onOpenTicker: OpenTicker }) {
  return (
    <Button type="button" variant="ghost" size="sm" className="-ml-2 h-7 font-semibold tracking-normal" onClick={() => onOpenTicker(ticker)}>
      {ticker}
    </Button>
  );
}

export function Head({ children, className }: { children: ReactNode; className?: string }) {
  return <th className={cn("px-3 py-3 font-semibold", className)}>{children}</th>;
}

export function Cell({ children, className }: { children: ReactNode; className?: string }) {
  return <td className={cn("px-3 py-3 leading-6", className)}>{children}</td>;
}

export function Truncated({ children }: { children: ReactNode }) {
  return <div className="min-w-0 truncate" title={typeof children === "string" ? children : undefined}>{children}</div>;
}

export function FullText({ children }: { children: ReactNode }) {
  return <div className="min-w-0 whitespace-pre-wrap break-words leading-6">{children}</div>;
}

export function MetricPill({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-20 rounded-md bg-muted px-2 py-1 text-right">
      <div className="text-[10px] font-semibold uppercase text-muted-foreground">{label}</div>
      <div className="text-sm font-semibold tabular-nums">{value}</div>
    </div>
  );
}
