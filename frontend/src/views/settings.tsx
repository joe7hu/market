import { BrainCircuit, Clock3, Save, SlidersHorizontal, Terminal } from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";

import { loadSettings, updateAgentSettings, type AgentSettingsInput } from "@/api";
import { DataTableFrame, StatusBadge } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useMarketData } from "@/marketData";
import type { SettingsPayload } from "@/types";
import type { Tone } from "@/ui/tone";
import { titleLabel, toneFromText } from "@/views/rowFormat";
import { WorkspacePage, type MetricSpec } from "./workspacePage";

type AgentForm = {
  enabled: boolean;
  command: string;
  timeout_seconds: number;
  limit: number;
};

const EMPTY_AGENT: AgentForm = { enabled: false, command: "", timeout_seconds: 120, limit: 0 };

export function SettingsPage() {
  const { data, loadScope } = useMarketData();
  const [directSettings, setDirectSettings] = useState<SettingsPayload | null>(null);
  const settings = directSettings ?? data.settings;
  const statusMetadata = jsonRecord(data.dashboard.status?.metadata);
  const statusConfig = jsonRecord(statusMetadata.config);
  const agentConfig = useMemo(
    () => agentConfigFromSettings(settings.agents?.config ?? settings.config?.agents ?? statusConfig.agents),
    [settings.agents?.config, settings.config, statusConfig.agents],
  );
  const agentRuntime = jsonRecord(settings.agents?.runtime ?? statusMetadata.agents);
  const scheduler = jsonRecord(settings.agents?.scheduler ?? statusMetadata.scheduler);
  const modelOverrides = jsonRecord(settings.agents?.model_overrides);
  const settingsReady = Boolean(directSettings || settings.agents || settings.config?.agents || statusConfig.agents);
  const [draft, setDraft] = useState<{ option_thesis: AgentForm; option_postmortem: AgentForm } | null>(null);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const thesis = draft?.option_thesis ?? agentConfig.option_thesis;
  const postmortem = draft?.option_postmortem ?? agentConfig.option_postmortem;

  useEffect(() => {
    let cancelled = false;
    void loadSettings()
      .then((payload) => {
        if (!cancelled) setDirectSettings(payload);
      })
      .catch((exc) => {
        if (!cancelled) setError(exc instanceof Error ? exc.message : "Failed to load agent settings");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const metrics: MetricSpec[] = [
    ["Thesis Worker", settingsReady ? thesis.enabled ? "Enabled" : "Paused" : "Loading", thesis.command || "No command configured", settingsReady ? thesis.enabled && thesis.command ? "good" : "warn" : "muted"],
    ["Postmortems", settingsReady ? postmortem.enabled ? "Enabled" : "Paused" : "Loading", postmortem.command || "No command configured", settingsReady ? postmortem.enabled && postmortem.command ? "good" : "muted" : "muted"],
    ["Premarket Cap", `${thesis.limit + postmortem.limit}`, "max agent calls per run", thesis.limit + postmortem.limit ? "info" : "muted"],
    ["In-App Agents", numberFromJson(scheduler.agent_refresh_seconds, 0) > 0 ? "Enabled" : "Paused", `MARKET_AGENT_REFRESH_SECONDS=${stringFromJson(scheduler.agent_refresh_seconds, "0")}`, numberFromJson(scheduler.agent_refresh_seconds, 0) > 0 ? "warn" : "good"],
  ];

  const save = async () => {
    setSaving(true);
    setError("");
    setMessage("");
    const payload: AgentSettingsInput = { option_thesis: thesis, option_postmortem: postmortem };
    try {
      const saved = await updateAgentSettings(payload);
      setDirectSettings(saved);
      await loadScope("settings");
      setDraft(null);
      setMessage("Agent settings saved to config.yaml.");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Failed to save agent settings");
    } finally {
      setSaving(false);
    }
  };

  return (
    <WorkspacePage
      eyebrow="Configuration"
      title="Agent Control Plane"
      subtitle="Runtime knobs for Market's token-bearing option thesis and postmortem workers."
      metrics={metrics}
      actions={
        <Button type="button" onClick={() => void save()} disabled={saving || !settingsReady}>
          <Save className={saving ? "animate-pulse" : undefined} />
          {saving ? "Saving" : "Save agents"}
        </Button>
      }
    >
      {message ? <InlineNotice tone="good">{message}</InlineNotice> : null}
      {error ? <InlineNotice tone="bad">{error}</InlineNotice> : null}

      <div className="grid gap-4 xl:grid-cols-2">
        <AgentSettingsCard
          title="Option Thesis Worker"
          description="Generates product-grounded hypotheses for top-ranked options candidates."
          value={thesis}
          runtime={jsonRecord(agentRuntime.option_thesis)}
          loaded={settingsReady}
          onChange={(next) => setDraft({ option_thesis: next, option_postmortem: postmortem })}
        />
        <AgentSettingsCard
          title="Option Postmortem Worker"
          description="Explains important outcomes and drafts strategy mutation proposals."
          value={postmortem}
          runtime={jsonRecord(agentRuntime.option_postmortem)}
          loaded={settingsReady}
          onChange={(next) => setDraft({ option_thesis: thesis, option_postmortem: next })}
        />
      </div>

      <RuntimePanel scheduler={scheduler} modelOverrides={modelOverrides} />
    </WorkspacePage>
  );
}

function AgentSettingsCard({
  title,
  description,
  value,
  runtime,
  loaded,
  onChange,
}: {
  title: string;
  description: string;
  value: AgentForm;
  runtime: Record<string, unknown>;
  loaded: boolean;
  onChange: (value: AgentForm) => void;
}) {
  const active = booleanFromJson(runtime.active, value.enabled && Boolean(value.command));
  const configured = booleanFromJson(runtime.configured, Boolean(value.command));
  const tone: Tone = !loaded ? "muted" : active ? "good" : configured ? "warn" : "muted";
  return (
    <DataTableFrame
      title={title}
      action={<StatusBadge tone={tone}>{!loaded ? "Loading" : active ? "Active" : configured ? "Paused" : "Not configured"}</StatusBadge>}
    >
      <div className="space-y-4 p-4">
        <div className="flex items-start gap-3">
          <div className="rounded-md border border-border bg-muted p-2"><BrainCircuit className="size-5 text-muted-foreground" /></div>
          <p className="text-sm leading-6 text-muted-foreground">{description}</p>
        </div>

        <label className="flex items-center justify-between gap-3 rounded-lg border border-border bg-background px-3 py-2">
          <span>
            <span className="block text-sm font-medium">Worker enabled</span>
            <span className="block text-xs text-muted-foreground">When off, queued requests stay visible but this worker will not run.</span>
          </span>
          <input
            type="checkbox"
            checked={value.enabled}
            disabled={!loaded}
            onChange={(event) => onChange({ ...value, enabled: event.target.checked })}
            className="size-5 accent-primary"
          />
        </label>

        <Field label="Command" detail="Local executable invoked by run_option_agents.">
          <Input value={value.command} onChange={(event) => onChange({ ...value, command: event.target.value })} placeholder="market-codex-option-thesis-agent" />
        </Field>

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Per-run limit" detail="Maximum calls per runner invocation.">
            <Input type="number" min={0} max={50} value={value.limit} onChange={(event) => onChange({ ...value, limit: boundedNumber(event.target.value, 0, 50) })} />
          </Field>
          <Field label="Timeout seconds" detail="External command timeout.">
            <Input type="number" min={10} max={900} value={value.timeout_seconds} onChange={(event) => onChange({ ...value, timeout_seconds: boundedNumber(event.target.value, 10, 900) })} />
          </Field>
        </div>

        <div className="grid gap-2 text-xs sm:grid-cols-3">
          <RuntimeChip label="Cadence" value={stringFromJson(runtime.cadence, "daily_premarket")} />
          <RuntimeChip label="Queue policy" value={stringFromJson(runtime.queue_policy, "open requests")} />
          <RuntimeChip label="Request cap" value={stringFromJson(runtime.request_cap, "-")} />
        </div>
      </div>
    </DataTableFrame>
  );
}

function RuntimePanel({ scheduler, modelOverrides }: { scheduler: Record<string, unknown>; modelOverrides: Record<string, unknown> }) {
  const rows = [
    ["Agent refresh", stringFromJson(scheduler.agent_refresh_seconds, "0"), "0 keeps in-app agent runs disabled"],
    ["Radar refresh", stringFromJson(scheduler.radar_refresh_seconds, "900"), "fast deterministic signal cadence"],
    ["Source refresh", stringFromJson(scheduler.source_refresh_seconds, "3600"), "option-chain/source pull cadence"],
    ["Learning refresh", stringFromJson(scheduler.learning_refresh_seconds, "21600"), "heavy deterministic learning cadence"],
    ["Radar source", stringFromJson(scheduler.radar_option_source, "ibkr"), "option source used by scheduler"],
    ["Codex model", stringFromJson(modelOverrides.codex_model, "default"), "MARKET_CODEX_MODEL"],
    ["Codex effort", stringFromJson(modelOverrides.codex_reasoning_effort, "default"), "MARKET_CODEX_REASONING_EFFORT"],
    ["OpenAI model", stringFromJson(modelOverrides.openai_model, "gpt-5.2"), "MARKET_OPENAI_MODEL"],
    ["Output cap", stringFromJson(modelOverrides.openai_max_output_tokens, "2000"), "MARKET_OPENAI_MAX_OUTPUT_TOKENS"],
  ];
  return (
    <DataTableFrame title="Runtime Environment" action={<StatusBadge tone="info">read-only env</StatusBadge>}>
      <table className="w-full min-w-[760px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <th className="px-3 py-3">Control</th>
            <th className="px-3 py-3">Value</th>
            <th className="px-3 py-3">Source</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([label, value, source]) => (
            <tr key={label} className="border-b border-border align-top hover:bg-accent/40">
              <td className="px-3 py-3">
                <div className="flex items-center gap-2 font-medium">
                  {label.includes("model") || label.includes("effort") || label.includes("Output") ? <Terminal className="size-4 text-muted-foreground" /> : <Clock3 className="size-4 text-muted-foreground" />}
                  {label}
                </div>
              </td>
              <td className="px-3 py-3"><StatusBadge tone={toneFromText(value)}>{titleLabel(value || "default")}</StatusBadge></td>
              <td className="px-3 py-3 text-muted-foreground">{source}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

function Field({ label, detail, children }: { label: string; detail: string; children: ReactNode }) {
  return (
    <label className="block space-y-1.5">
      <span className="flex items-center gap-2 text-sm font-medium"><SlidersHorizontal className="size-4 text-muted-foreground" /> {label}</span>
      {children}
      <span className="block text-xs text-muted-foreground">{detail}</span>
    </label>
  );
}

function RuntimeChip({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-border bg-muted/40 p-2">
      <div className="text-[11px] uppercase text-muted-foreground">{label}</div>
      <div className="mt-1 truncate font-medium" title={value}>{titleLabel(value)}</div>
    </div>
  );
}

function InlineNotice({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <div className="rounded-md border border-border bg-card px-4 py-3 text-sm"><StatusBadge tone={tone}>{children}</StatusBadge></div>;
}

function agentConfigFromSettings(value: unknown): { option_thesis: AgentForm; option_postmortem: AgentForm } {
  const agents = jsonRecord(value);
  return {
    option_thesis: agentForm(jsonRecord(agents.option_thesis), { ...EMPTY_AGENT, command: "market-codex-option-thesis-agent", limit: 8, timeout_seconds: 180 }),
    option_postmortem: agentForm(jsonRecord(agents.option_postmortem), { ...EMPTY_AGENT, command: "market-codex-option-postmortem-agent", limit: 4, timeout_seconds: 180 }),
  };
}

function agentForm(value: Record<string, unknown>, fallback: AgentForm): AgentForm {
  return {
    enabled: booleanFromJson(value.enabled, fallback.enabled),
    command: stringFromJson(value.command, fallback.command),
    timeout_seconds: numberFromJson(value.timeout_seconds, fallback.timeout_seconds),
    limit: numberFromJson(value.limit, fallback.limit),
  };
}

function boundedNumber(value: string, min: number, max: number): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return min;
  return Math.max(min, Math.min(max, Math.round(parsed)));
}

function jsonRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function numberFromJson(value: unknown, fallback: number): number {
  const parsed = typeof value === "number" ? value : typeof value === "string" ? Number(value) : Number.NaN;
  return Number.isFinite(parsed) ? parsed : fallback;
}

function booleanFromJson(value: unknown, fallback: boolean): boolean {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string") return ["1", "true", "yes", "on", "enabled", "active"].includes(value.trim().toLowerCase());
  return fallback;
}

function stringFromJson(value: unknown, fallback: string): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return fallback;
}
