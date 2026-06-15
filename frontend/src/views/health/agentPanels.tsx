import { BrainCircuit, Loader2, Play, Save } from "lucide-react";
import { useState, type ReactNode } from "react";

import { updateAgentSettings } from "@/api";
import { DataTableFrame, StatusBadge } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { PanelData } from "@/types";
import type { Tone } from "@/ui/tone";
import { titleLabel, toneFromText } from "@/views/rowFormat";
import { StatusDot } from "@/views/health/dataFlow";
import {
  agentLastRun,
  agentOptionConfig,
  agentQueueStats,
  buildAgentPipelines,
  recentAgentRuns,
} from "@/views/health/aggregate";
import { formatAge, formatDateTime } from "@/views/health/format";
import { RefreshHistoryTable } from "@/views/health/tables";
import type { UseRefreshJobs } from "@/views/health/useRefreshJobs";

const AGENT_JOB = "run_option_agents";

// Mirrors the per-ticker bundle assembled in build_agent_thesis_request (backend).
const AGENT_CONTEXT_INPUTS = [
  "Option candidate",
  "Instrument / sector",
  "Stock + option features",
  "Fundamentals",
  "Technicals",
  "Ownership / 13F",
  "News",
  "X / social + blog signals",
  "Portfolio position",
  "Decision grade",
  "Catalysts / earnings",
];

type ControlForm = { enabled: boolean; thesisLimit: number; postmortemLimit: number; timeoutSeconds: number };

export function AgentControlPanel({
  data,
  jobs,
  schedulerSeconds,
  onChanged,
}: {
  data: PanelData;
  jobs: UseRefreshJobs;
  schedulerSeconds: number;
  onChanged: () => void;
}) {
  const pipeline = buildAgentPipelines(data)[0];
  const runtime = agentOptionConfig(data);
  const queue = agentQueueStats(data);
  const lastRun = agentLastRun(jobs.rows);
  const recent = recentAgentRuns(jobs.rows);

  const live: ControlForm = {
    enabled: runtime.enabled,
    thesisLimit: runtime.thesisLimit,
    postmortemLimit: runtime.postmortemLimit,
    timeoutSeconds: runtime.timeoutSeconds,
  };
  const [draft, setDraft] = useState<ControlForm | null>(null);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const form = draft ?? live;
  const running = jobs.pendingJobs.has(AGENT_JOB) || jobs.jobStates[AGENT_JOB]?.status === "running";

  const save = async (overrides: Partial<ControlForm> = {}) => {
    const next = { ...form, ...overrides };
    setDraft(next);
    setSaving(true);
    setError("");
    setMessage("");
    try {
      await updateAgentSettings({
        option_agent: {
          enabled: next.enabled,
          thesis_limit: next.thesisLimit,
          postmortem_limit: next.postmortemLimit,
          timeout_seconds: next.timeoutSeconds,
        },
      });
      setMessage("Agent settings saved.");
      setDraft(null);
      onChanged();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Failed to save agent settings");
    } finally {
      setSaving(false);
    }
  };

  return (
    <DataTableFrame
      title="Agent Control"
      action={
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge tone={runtime.active ? "good" : "muted"}>{runtime.active ? "Active" : "Paused"}</StatusBadge>
          <StatusBadge tone={schedulerSeconds > 0 ? "warn" : "muted"}>
            {schedulerSeconds > 0 ? `In-app every ${schedulerSeconds}s` : "In-app paused"}
          </StatusBadge>
          <Button type="button" size="sm" disabled={running} onClick={() => void jobs.start(AGENT_JOB)} title="Run the consolidated option agent now">
            {running ? <Loader2 className="size-4 animate-spin" /> : <Play className="size-4" />}
            {running ? "Running" : "Run now"}
          </Button>
        </div>
      }
    >
      <div className="space-y-4 p-4">
        {message ? <Notice tone="good">{message}</Notice> : null}
        {error ? <Notice tone="bad">{error}</Notice> : null}

        {/* Controls */}
        <div className="rounded-lg border border-border bg-background p-4">
          <div className="mb-3 flex items-center gap-2">
            <BrainCircuit className="size-4 text-muted-foreground" />
            <h3 className="text-sm font-semibold">Consolidated Option Agent</h3>
            {runtime.mode ? <span className="text-xs text-muted-foreground">mode: {titleLabel(runtime.mode)}</span> : null}
          </div>
          <div className="grid items-end gap-3 sm:grid-cols-2 xl:grid-cols-5">
            <label className="flex items-center justify-between gap-2 rounded-md border border-border px-3 py-2 sm:col-span-2 xl:col-span-1">
              <span className="text-sm font-medium">Enabled</span>
              <input
                type="checkbox"
                checked={form.enabled}
                disabled={saving}
                onChange={(event) => void save({ enabled: event.target.checked })}
                className="size-5 accent-primary"
              />
            </label>
            <NumberField label="Thesis cap" value={form.thesisLimit} min={0} max={50} onChange={(v) => setDraft({ ...form, thesisLimit: v })} />
            <NumberField label="Postmortem cap" value={form.postmortemLimit} min={0} max={50} onChange={(v) => setDraft({ ...form, postmortemLimit: v })} />
            <NumberField label="Timeout (s)" value={form.timeoutSeconds} min={10} max={900} onChange={(v) => setDraft({ ...form, timeoutSeconds: v })} />
            <Button type="button" variant="outline" disabled={saving || !draft} onClick={() => void save()}>
              <Save className={saving ? "animate-pulse" : undefined} />
              {saving ? "Saving" : "Save caps"}
            </Button>
          </div>
          <p className="mt-2 text-xs text-muted-foreground">
            One batched pass covers all open thesis + postmortem requests in a single call. Caps bound spend per run; persisted to config.yaml.
          </p>
          <div className="mt-3">
            <div className="text-[11px] font-medium uppercase text-muted-foreground">Each per-ticker run ingests</div>
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              {AGENT_CONTEXT_INPUTS.map((input) => (
                <span key={input} className="rounded-md border border-border bg-muted/40 px-2 py-0.5 text-[11px] text-muted-foreground">
                  {input}
                </span>
              ))}
            </div>
          </div>
        </div>

        {/* Granular status */}
        <div className="grid gap-3 md:grid-cols-4">
          <StatTile
            label="Queue depth"
            value={queue.totalOpen}
            caption={`${queue.thesisOpen} thesis · ${queue.postmortemOpen} postmortem`}
            tone={queue.totalOpen ? "warn" : "good"}
          />
          <StatTile
            label="Oldest open"
            value={queue.oldestOpenAt ? formatAge(queue.oldestOpenAt) : "—"}
            caption="age of the oldest queued request"
            tone={queue.oldestOpenAt ? "warn" : "good"}
          />
          <StatTile label="Completed" value={queue.done} caption="stored structured outputs" tone={queue.done ? "info" : "muted"} />
          <StatTile label="Failed" value={pipeline?.failed ?? queue.failed} caption="calls that returned no usable output" tone={(pipeline?.failed ?? queue.failed) ? "bad" : "good"} />
        </div>

        {/* Last run */}
        <div className="rounded-lg border border-border bg-background p-4">
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-sm font-semibold">Last run</h3>
            {lastRun ? <StatusBadge tone={toneFromText(lastRun.status)}>{titleLabel(lastRun.status)}</StatusBadge> : <StatusBadge tone="muted">No run recorded</StatusBadge>}
          </div>
          {lastRun ? (
            <>
              <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted-foreground">
                <span>Accepted <span className="tabular-nums text-emerald-600 dark:text-emerald-400">{lastRun.accepted}</span> / {lastRun.attempted}</span>
                <span>Failed <span className="tabular-nums text-foreground">{lastRun.failed}</span></span>
                <span>Mode <span className="text-foreground">{titleLabel(lastRun.mode)}</span></span>
                <span>Finished <span className="text-foreground">{formatDateTime(lastRun.finishedAt)} ({formatAge(lastRun.finishedAt)})</span></span>
              </div>
              {lastRun.failures.length ? (
                <ul className="mt-3 space-y-1 text-xs">
                  {lastRun.failures.map((failure, index) => (
                    <li key={`${failure.ticker}-${index}`} className="flex gap-2">
                      <StatusDot tone="bad" className="mt-1" />
                      <span><span className="font-medium text-foreground">{failure.ticker}</span> <span className="text-muted-foreground">{failure.error}</span></span>
                    </li>
                  ))}
                </ul>
              ) : null}
            </>
          ) : (
            <p className="text-xs text-muted-foreground">Run the agent to populate per-request outcomes here.</p>
          )}
        </div>

        {/* Recent run history */}
        <details className="overflow-hidden rounded-lg border border-border">
          <summary className="flex cursor-pointer list-none items-center justify-between bg-muted/40 px-3 py-2 text-sm font-medium">
            <span>Recent agent runs</span>
            <span className="text-xs text-muted-foreground">{recent.length} shown</span>
          </summary>
          <div className="overflow-x-auto border-t border-border">
            <RefreshHistoryTable rows={recent} />
          </div>
        </details>
      </div>
    </DataTableFrame>
  );
}

function NumberField({ label, value, min, max, onChange }: { label: string; value: number; min: number; max: number; onChange: (value: number) => void }) {
  return (
    <label className="block space-y-1">
      <span className="text-xs font-medium text-muted-foreground">{label}</span>
      <Input
        type="number"
        min={min}
        max={max}
        value={value}
        onChange={(event) => {
          const parsed = Number(event.target.value);
          onChange(Number.isFinite(parsed) ? Math.max(min, Math.min(max, Math.round(parsed))) : min);
        }}
      />
    </label>
  );
}

function StatTile({ label, value, caption, tone }: { label: string; value: number | string; caption: string; tone: Tone }) {
  return (
    <div className="rounded-lg border border-border bg-background p-3">
      <div className="text-xs font-medium uppercase text-muted-foreground">{label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums">{typeof value === "number" ? value.toLocaleString() : value}</div>
      <div className="mt-1 flex items-center gap-2 text-xs text-muted-foreground"><StatusDot tone={tone} /> {caption}</div>
    </div>
  );
}

function Notice({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <div className="rounded-md border border-border bg-card px-3 py-2 text-sm"><StatusBadge tone={tone}>{children}</StatusBadge></div>;
}
