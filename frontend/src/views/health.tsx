import { DataGridSection } from "./dataGridSection";
import { WorkspacePage } from "./workspacePage";
import type { AppModel } from "@/model";
import type { PanelData } from "@/types";
import { rows } from "@/utils";

export function HealthPage({ model, data }: { model: AppModel; data: PanelData }) {
  return (
    <WorkspacePage
      eyebrow="System health"
      title="Health"
      subtitle="Provider health, source freshness, broker status, and refresh job state."
      metrics={[
        ["Latest Check", model.latestHealthCheck, "freshest health timestamp", model.sources.health === "live" ? "info" : "muted"],
        ["Freshness Rows", rows(data.sourceFreshness).length, "source checks", rows(data.sourceFreshness).length ? "info" : "muted"],
        ["Provider Runs", rows(data.providerRuns).length, "ingestion runs", rows(data.providerRuns).length ? "info" : "muted"],
        ["Broker Status", rows(data.brokerStatus).length, "account source checks", rows(data.brokerStatus).length ? "info" : "muted"],
      ]}
    >
      <DataGridSection title="Source Freshness" rows={rows(data.sourceFreshness)} />
      <DataGridSection title="Source Health" rows={rows(data.sourceHealth)} />
      <DataGridSection title="Provider Runs" rows={rows(data.providerRuns)} />
      <DataGridSection title="Refresh Jobs" rows={rows(data.refreshJobs)} />
    </WorkspacePage>
  );
}
