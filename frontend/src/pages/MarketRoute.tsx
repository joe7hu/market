import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { WorkspacePage } from "@/views/workspacePage";
import { rows } from "@/utils";
import { textField } from "@/views/rowFormat";
import { isMarketDriver } from "@/views/market/format";
import { MarketAssetMatrix, MarketEnvironmentPanel, ReferenceValuationCharts } from "@/views/market/panels";
import type { JsonValue } from "@/types";

export function MarketRoute() {
  const { data } = useMarketData();
  usePanelScope("market");

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

  return (
    <WorkspacePage
      eyebrow="Market stance"
      title="Where the Market Stands"
      subtitle="Broad market valuation, trend, breadth, risk appetite, and leadership."
    >
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

      <ReferenceValuationCharts rows={referenceRows} />

      <MarketAssetMatrix rows={assetRows} />
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
