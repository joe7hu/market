import {useMemo, useState} from "react";
import {Check, ClipboardCopy, FileSearch2} from "lucide-react";
import basePrompt from "../../../../prompts/options_radar_deep_research.md?raw";
import {StatusBadge} from "@/components/market/workstation";
import {Button} from "@/components/ui/button";
import {RowRecord} from "@/types";
import {formatScore} from "../optionsRadarFormat";
import {recordField, stringFromRecord} from "../optionsRadarData";
import {numberField, textField, titleLabel} from "../rowFormat";
import {compareGroupedOpportunities, stateOf} from "./helpers";

type RankedSignal = {
  ticker: string;
  state: string;
  structure: string;
  expiration: string;
  score: number;
};

export function buildOptionsResearchPrompt(rows: RowRecord[], observationTime: string): string {
  const signals = rankedSignals(rows);
  if (!signals.length) return "";
  const tickers = signals.map((signal) => signal.ticker);
  const tableRows = signals.map((signal, index) => (
    `| ${index + 1} | ${signal.ticker} | ${titleLabel(signal.state || "watch")} | ${titleLabel(signal.structure || "option")} | ${signal.expiration || "Verify live chain"} | ${formatScore(signal.score)} |`
  ));

  return [
    "# Customized Options Deep-Research Assignment",
    "",
    "## Current Market Radar Inputs — Mandatory Secondary Underwriting",
    "",
    `Observation time: ${observationTime || "Current Market publication"}`,
    `Current ranked tickers: ${tickers.join(", ")}`,
    "",
    "These tickers came from the current Market Options Radar publication. They are mandatory inputs to secondary underwriting, but they are not the discovery universe and must not be used to bypass the broad-universe process below.",
    "",
    "Complete the required broad discovery first. Then underwrite every radar ticker alongside newly discovered candidates, compare them on the same evidence, saturation, optionability, and expected-value standards, and state clearly when a radar ticker should be rejected or deprioritized. Do not force any radar ticker into the final recommendations.",
    "",
    "Treat the radar structure and score as provisional context, not as verified live-chain facts or investment conclusions. Re-check the underlying thesis, catalyst, current price, option chain, liquidity, implied volatility, payoff, and execution immediately before recommending a trade.",
    "",
    "| Radar rank | Ticker | State | Radar structure | Radar expiration | Rank score |",
    "|---:|---|---|---|---|---:|",
    ...tableRows,
    "",
    "In `Top Opportunities Now`, explicitly identify which finalists came from the Market radar and which came from independent broad discovery. For each radar ticker, give a final disposition: `ADVANCE`, `SECONDARY REVIEW`, `LIQUIDITY WATCH`, `ALREADY PRICED`, `WEAK TRANSMISSION`, `NO CATALYST`, or `REJECT`.",
    "",
    "---",
    "",
    basePrompt.trim(),
  ].join("\n");
}

export function ResearchPromptPanel({rows, observationTime}: {rows: RowRecord[]; observationTime: string}) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "error">("idle");
  const signals = useMemo(() => rankedSignals(rows), [rows]);
  const prompt = useMemo(() => buildOptionsResearchPrompt(rows, observationTime), [observationTime, rows]);

  async function handleCopy() {
    if (!prompt) return;
    try {
      await copyToClipboard(prompt);
      setCopyState("copied");
    } catch {
      setCopyState("error");
    }
  }

  return (
    <section className="overflow-hidden rounded-md border border-border bg-card">
      <div className="grid lg:grid-cols-[minmax(0,1fr)_minmax(300px,0.42fr)]">
        <div className="min-w-0 p-4 sm:p-5">
          <div className="flex flex-wrap items-center gap-2">
            <div className="flex size-9 items-center justify-center rounded-md border border-primary/25 bg-primary/10 text-primary">
              <FileSearch2 className="size-4" />
            </div>
            <div>
              <p className="text-[10px] font-semibold uppercase tracking-[0.16em] text-muted-foreground">ChatGPT research handoff</p>
              <h2 className="text-base font-semibold">Broad-Universe Deep Research Prompt</h2>
            </div>
          </div>
          <p className="mt-3 max-w-4xl text-sm leading-6 text-muted-foreground">
            Generates one copy-ready assignment from the full discovery mandate and the current ranked signals. Radar names remain required comparison inputs—not a shortcut around broad discovery.
          </p>
          <div className="mt-4 flex flex-wrap gap-1.5">
            {signals.length ? signals.map((signal, index) => (
              <span key={signal.ticker} className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-2.5 py-1 text-xs font-semibold tabular-nums">
                <span className="text-muted-foreground">{index + 1}</span>
                {signal.ticker}
              </span>
            )) : <StatusBadge tone="muted">No ranked tickers available</StatusBadge>}
          </div>
        </div>
        <div className="flex flex-col justify-between gap-4 border-t border-border bg-muted/35 p-4 lg:border-l lg:border-t-0 sm:p-5">
          <div>
            <div className="flex flex-wrap gap-2">
              <StatusBadge tone={signals.length ? "info" : "muted"}>{signals.length} unique ticker{signals.length === 1 ? "" : "s"}</StatusBadge>
              {prompt ? <StatusBadge tone="muted">{prompt.length.toLocaleString()} characters</StatusBadge> : null}
            </div>
            <p className="mt-3 text-xs leading-5 text-muted-foreground">
              The copied prompt includes current rank, signal state, provisional structure, expiration, and score, followed by the complete research protocol.
            </p>
          </div>
          <Button type="button" className="w-full" disabled={!prompt} onClick={() => void handleCopy()}>
            {copyState === "copied" ? <Check /> : <ClipboardCopy />}
            {copyState === "copied" ? "Copied to clipboard" : "Copy research prompt"}
          </Button>
          <p className="min-h-5 text-center text-xs text-muted-foreground" role="status" aria-live="polite">
            {copyState === "error" ? "Clipboard access failed. Open the preview and copy manually." : copyState === "copied" ? "Ready to paste into ChatGPT." : ""}
          </p>
        </div>
      </div>
      {prompt ? (
        <details className="border-t border-border">
          <summary className="cursor-pointer select-none px-4 py-3 text-xs font-semibold uppercase tracking-[0.1em] text-muted-foreground transition-colors hover:bg-accent/40 hover:text-foreground sm:px-5">
            Preview and manually copy prompt
          </summary>
          <div className="border-t border-border bg-background p-3 sm:p-4">
            <textarea
              readOnly
              value={prompt}
              aria-label="Generated options deep research prompt"
              className="h-[32rem] w-full resize-y rounded-md border border-border bg-card p-4 font-mono text-xs leading-5 text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
              onFocus={(event) => event.currentTarget.select()}
            />
          </div>
        </details>
      ) : null}
    </section>
  );
}

function rankedSignals(rows: RowRecord[]): RankedSignal[] {
  const byTicker = new Map<string, RankedSignal>();
  for (const row of [...rows].sort(compareGroupedOpportunities)) {
    const ticker = textField(row, ["ticker"]).toUpperCase();
    if (!ticker || byTicker.has(ticker)) continue;
    const raw = recordField(row, "raw");
    byTicker.set(ticker, {
      ticker,
      state: stateOf(row),
      structure: textField(row, ["structure", "option_type"]),
      expiration: textField(row, ["expiration", "expiry"], stringFromRecord(raw, "expiration")),
      score: numberField(row, ["rank_score", "score", "conviction_score"], Number.NaN),
    });
  }
  return [...byTicker.values()];
}

async function copyToClipboard(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // LAN-hosted Market sessions may expose the Clipboard API but reject it
      // outside a secure context. Fall through to the selection-based copy.
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
