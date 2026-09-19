import { WorkflowReadiness } from "@/components/market/WorkflowReadiness";
import { useCallback } from "react";
import { Link } from "react-router-dom";
import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { Button } from "@/components/ui/button";
import { ScopeStatusNotice } from "@/components/market/scopeStatus";
import { WorkspacePage } from "@/views/workspacePage";
import { rows } from "@/utils";
import { textField } from "@/shared/rowFormat";
import { isMarketDriver } from "@/views/market/format";
import { MarketAssetMatrix, MarketEnvironmentPanel, ReferenceValuationCharts } from "@/views/market/panels";
import type { JsonValue } from "@/types";

export function MarketRoute() {
  const { data, scopeStatus, loadScope } = useMarketData();
  usePanelScope("market", { retries: 2 });
  const status = scopeStatus.market;
  const loading = !status || status.state === "loading";
  // The provider owns the error state and preserves labeled last-good data.
  const reload = useCallback(() => { void loadScope("market", { force: true }).catch(() => undefined); }, [loadScope]);

  const referenceRows = rows(data.marketValuationReferenceCharts);
  const assetRows = rows(data.marketEnvironmentAssets);
  const environmentRows = rows(data.marketEnvironmentModel);
  const snapshotRows = rows(data.marketStateSnapshot);
  const coverageRows = rows(data.coverageMatrix);
  const posteriorRows = rows(data.marketStatePosterior);
  const coverageVectorRows = rows(data.marketCoverageVector);
  const scenarioRows = rows(data.marketScenarioPaths);
  const optionSlaRows = rows(data.optionLiquiditySla);
  const observationRows = rows(data.marketObservations);
  const freshness = marketFreshness(data.dashboard.status?.metadata?.market_freshness);
  const drivers = environmentRows.filter((row) => textField(row, ["category"]) !== "Overall");
  const marketDrivers = drivers.filter((row) => isMarketDriver(textField(row, ["category"])));
  const hasSnapshot = [referenceRows, assetRows, marketDrivers, snapshotRows, coverageRows,
    posteriorRows, coverageVectorRows, scenarioRows, optionSlaRows, observationRows].some((items) => items.length > 0);
  const missingSections = [
    !marketDrivers.length ? "market drivers" : null,
    !snapshotRows.length ? "point-in-time market state" : null,
    !referenceRows.length ? "valuation history" : null,
    !assetRows.length ? "asset comparison" : null,
  ].filter((item): item is string => item !== null);

  return (
    <WorkspacePage
      eyebrow="Market stance"
      title="Where the Market Stands"
      subtitle="Broad market valuation, trend, breadth, risk appetite, and leadership."
      actions={<Button type="button" variant="outline" onClick={reload} disabled={loading}>{loading ? "Loading…" : "Reload snapshot"}</Button>}
    >
      <WorkflowReadiness view="market" onDataChanged={reload} />
      <ScopeStatusNotice status={status} onRetry={reload} />
      {!hasSnapshot ? (
        <section role="status" className="rounded-xl border border-border bg-card p-6">
          <h2 className="text-lg font-semibold">{loading ? "Loading market snapshot…" : "Market snapshot is unavailable"}</h2>
          <p className="mt-2 text-sm text-muted-foreground">{loading
            ? "Waiting for the published market data. Missing values are not a neutral market signal."
            : "No usable market snapshot is available in this view. This is a data or publication gap, not a market stance."}</p>
          {!loading ? <>
            <p className="mt-2 text-sm text-muted-foreground">Check the source data and downstream Market publication in System health. A successful source check alone does not establish a usable snapshot.</p>
            <div className="mt-4 flex flex-wrap items-center gap-4">
              <Button type="button" variant="outline" onClick={reload}>Retry snapshot</Button>
              <Link className="text-sm text-primary underline" to="/health">Review data and publication health</Link>
            </div>
            <p className="mt-3 text-xs text-muted-foreground">Reload checks published data; it does not ingest missing data or repair the publisher.</p>
          </> : null}
        </section>
      ) : <>
        {loading ? <p role="status" className="text-sm text-muted-foreground">Refreshing the snapshot. Previously loaded data remains visible.</p> : null}
        {missingSections.length ? <section role="status" className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-100">
          <strong>Partial market coverage.</strong> Not available in this snapshot: {missingSections.join(", ")}. Available evidence remains visible; missing dimensions must not be interpreted as neutral.
          {" "}<Link className="underline" to="/health">Review data health</Link>
        </section> : null}
        <MarketEnvironmentPanel
          rows={marketDrivers}
          referenceRows={referenceRows}
          assetRows={assetRows}
          snapshotRows={snapshotRows}
          coverageRows={coverageRows}
          posteriorRows={posteriorRows}
          coverageVectorRows={coverageVectorRows}
          scenarioRows={scenarioRows}
          optionSlaRows={optionSlaRows}
          observationRows={observationRows}
          freshness={freshness}
        />
        {referenceRows.length ? <ReferenceValuationCharts rows={referenceRows} /> : null}
        {assetRows.length ? <MarketAssetMatrix rows={assetRows} /> : null}
      </>}
    </WorkspacePage>
  );
}

function marketFreshness(value: JsonValue | undefined): { status: string; reason: string } | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const record = value as Record<string, JsonValue | undefined>;
  return {
    status: typeof record.status === "string" ? record.status : "unknown",
    reason: typeof record.reason === "string" ? record.reason : "",
  };
}
