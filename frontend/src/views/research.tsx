import { DataTableFrame, StatusBadge } from "@/components/market/workstation";
import type { PanelData, RowRecord } from "@/types";
import { tickerSymbolFromRow } from "@/utils";
import { buildDatasetProfile } from "@/viewModels/datasetProfiles";
import { displayField, numberField, symbolList, toneFromText } from "./rowFormat";
import { DatasetPage } from "./datasetPage";
import { cn } from "@/lib/utils";
import type { OpenTicker } from "./workspacePage";

export function ResearchPage({ data, onOpenTicker }: { data: PanelData; onOpenTicker: OpenTicker }) {
  const profile = buildDatasetProfile("research", data);
  const decisionRows = profile.sections.find((section) => section.title === "Idea Queue")?.rows ?? [];
  return <DatasetPage profile={profile} onOpenTicker={onOpenTicker} beforeTables={<ResearchDecisionBoard rows={decisionRows} onOpenTicker={onOpenTicker} />} />;
}

function ResearchDecisionBoard({ rows: decisionRows, onOpenTicker }: { rows: RowRecord[]; onOpenTicker: OpenTicker }) {
  const topRows = decisionRows.slice(0, 4);
  if (!topRows.length) return null;
  return (
    <DataTableFrame title="Decision Board">
      <div className="grid gap-3 p-4 lg:grid-cols-2">
        {topRows.map((row, index) => {
          const primarySymbol = tickerSymbolFromRow(row) || symbolList(row)[0];
          const label = primarySymbol || `Row ${index + 1}`;
          const decision = displayField(row, ["decision_bucket", "decision", "action_grade"], "Review");
          const score = numberField(row, ["decision_score", "score"], Number.NaN);
          const tone = toneFromText(`${decision} ${Number.isFinite(score) && score >= 70 ? "ready" : ""}`);
          return (
            <button
              key={`${label}-${index}`}
              type="button"
              className="rounded-md border border-border bg-background p-4 text-left transition-colors hover:bg-accent/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              onClick={() => primarySymbol && onOpenTicker(primarySymbol)}
              disabled={!primarySymbol}
            >
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="text-lg font-semibold">{label}</div>
                  <p className="mt-1 text-sm text-muted-foreground">{displayField(row, ["reason", "summary", "source"], "Review this idea against the evidence.")}</p>
                </div>
                <StatusBadge tone={tone}>{decision}</StatusBadge>
              </div>
              {Number.isFinite(score) ? (
                <div className="mt-3">
                  <div className="mb-1 flex justify-between text-xs text-muted-foreground"><span>Decision score</span><span>{score.toFixed(score % 1 ? 2 : 0)}</span></div>
                  <div className="h-2 overflow-hidden rounded-full bg-muted">
                    <div className={cn("h-full rounded-full", score >= 70 ? "bg-green-600" : score >= 40 ? "bg-amber-500" : "bg-muted-foreground")} style={{ width: `${Math.min(100, Math.max(2, score))}%` }} />
                  </div>
                </div>
              ) : null}
            </button>
          );
        })}
      </div>
    </DataTableFrame>
  );
}
