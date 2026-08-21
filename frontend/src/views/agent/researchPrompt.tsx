import { Check, ClipboardCopy, FileSearch2 } from "lucide-react";
import { useState } from "react";

import type { DailyResearchPrompt } from "@/api/agent";
import { DataTableFrame, StatusBadge } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";

export function DailyResearchPromptPanel({ research }: { research?: DailyResearchPrompt }) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "error">("idle");
  const prompt = research?.prompt ?? "";
  const coverage = research?.coverage;
  const ready = Boolean(research?.ready && prompt);

  async function handleCopy() {
    if (!ready) return;
    try {
      await copyToClipboard(prompt);
      setCopyState("copied");
    } catch {
      setCopyState("error");
    }
  }

  return (
    <DataTableFrame
      title="Daily investment deep research"
      action={<StatusBadge tone={ready ? "info" : research ? "bad" : "muted"}>{ready ? "Ready to copy" : research ? "Context unavailable" : "Loading context"}</StatusBadge>}
    >
      <div className="grid lg:grid-cols-[minmax(0,1fr)_minmax(320px,0.42fr)]">
        <div className="min-w-0 space-y-4 p-4 sm:p-5">
          <div className="flex items-start gap-3">
            <div className="flex size-9 shrink-0 items-center justify-center rounded-md border border-primary/25 bg-primary/10 text-primary">
              <FileSearch2 className="size-4" />
            </div>
            <div>
              <p className="text-sm font-medium">Portfolio-aware, macro-aware, cross-asset research handoff</p>
              <p className="mt-1 max-w-4xl text-sm leading-6 text-muted-foreground">
                Backend-generated from every current holding and active watchlist symbol, plus Market&apos;s risk, thesis, event, regime,
                source, and options decision surfaces. The copied assignment asks Deep Research to verify current facts, red-team every
                idea, compare spot/options/crypto against cash, and run broad discovery before recommending action.
              </p>
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <CoverageBadge label="holdings" value={coverage?.portfolio_positions} />
            <CoverageBadge label="watchlist" value={coverage?.watchlist_symbols} />
            <CoverageBadge label="option signals" value={coverage?.option_signals} />
            <CoverageBadge label="macro indicators" value={coverage?.macro_indicators} />
            <CoverageBadge label="events" value={coverage?.events} />
            <CoverageBadge label="theses" value={coverage?.theses} />
            <CoverageBadge label="Market insights" value={coverage?.market_intelligence_items} />
            {coverage?.future_dated_rows_excluded ? <StatusBadge tone="warn">{coverage.future_dated_rows_excluded} future rows excluded</StatusBadge> : null}
          </div>
          {research && !research.ready ? <p className="text-sm text-red-600 dark:text-red-400">Research context is unavailable: {research.message}</p> : null}
          {coverage?.portfolio_symbols?.length ? (
            <p className="text-xs leading-5 text-muted-foreground"><strong className="text-foreground">Portfolio:</strong> {coverage.portfolio_symbols.join(", ")}</p>
          ) : null}
          {coverage?.watchlist?.length ? (
            <p className="text-xs leading-5 text-muted-foreground"><strong className="text-foreground">Watchlist:</strong> {coverage.watchlist.join(", ")}</p>
          ) : null}
        </div>
        <div className="flex flex-col justify-between gap-4 border-t border-border bg-muted/35 p-4 sm:p-5 lg:border-l lg:border-t-0">
          <div className="space-y-2">
            <div className="flex flex-wrap gap-2">
              {research ? <StatusBadge tone="muted">~{research.estimated_tokens.toLocaleString()} tokens</StatusBadge> : null}
              {research?.generated_at ? <StatusBadge tone="muted">Generated {formatTime(research.generated_at)}</StatusBadge> : null}
            </div>
            <p className="text-xs leading-5 text-muted-foreground">
              Use ChatGPT Deep Research and review its proposed plan and sources before it runs. Prices, chains, macro releases, and events must be re-verified at research time.
            </p>
          </div>
          <Button type="button" className="w-full" disabled={!ready} onClick={() => void handleCopy()}>
            {copyState === "copied" ? <Check /> : <ClipboardCopy />}
            {copyState === "copied" ? "Copied to clipboard" : "Copy daily research prompt"}
          </Button>
          <p className="min-h-5 text-center text-xs text-muted-foreground" role="status" aria-live="polite">
            {copyState === "error" ? "Clipboard access failed. Open the preview and copy manually." : copyState === "copied" ? "Ready to paste into Deep Research." : ""}
          </p>
        </div>
      </div>
      {ready ? (
        <details className="border-t border-border">
          <summary className="cursor-pointer select-none px-4 py-3 text-xs font-semibold uppercase tracking-[0.1em] text-muted-foreground transition-colors hover:bg-accent/40 hover:text-foreground sm:px-5">
            Preview and manually copy prompt
          </summary>
          <div className="border-t border-border bg-background p-3 sm:p-4">
            <textarea
              readOnly
              value={prompt}
              aria-label="Generated daily investment deep research prompt"
              className="h-[36rem] w-full resize-y rounded-md border border-border bg-card p-4 font-mono text-xs leading-5 text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
              onFocus={(event) => event.currentTarget.select()}
            />
          </div>
        </details>
      ) : null}
    </DataTableFrame>
  );
}

function CoverageBadge({ label, value }: { label: string; value?: number }) {
  return <StatusBadge tone={value ? "info" : "muted"}>{value ?? 0} {label}</StatusBadge>;
}

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

async function copyToClipboard(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // LAN-hosted sessions can expose the API but reject it outside a secure context.
    }
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) throw new Error("Copy command was rejected");
}
