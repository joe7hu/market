import { BrainCircuit, Loader2, Play, Save, Send } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import {
  analyzeTicker,
  loadAgent,
  startRefreshJob,
  updateAgentSettings,
  type AgentOverview,
  type AgentRun,
  type OptionAgentSettingsInput,
} from "@/api";
import { DataTableFrame, StatusBadge } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { Tone } from "@/ui/tone";
import { titleLabel, toneFromText } from "@/views/rowFormat";
import { WorkspacePage, type MetricSpec } from "@/views/workspacePage";

// Manual / on-demand runs force the consolidated pass regardless of auto-run.
const FORCE_JOB = "run_option_agents_force";
const CONTEXT_KEYS = ["fundamentals", "technicals", "ownership", "news", "social_signals", "catalysts", "portfolio", "decision"] as const;

type ControlForm = {
  enabled: boolean;
  command: string;
  provider: string;
  model: string;
  reasoning_effort: string;
  timeout_seconds: number;
  thesis_limit: number;
  postmortem_limit: number;
  auto_run_seconds: number;
  max_runs_per_day: number;
  context_sources: Record<string, boolean>;
};

export function AgentPage() {
  const [data, setData] = useState<AgentOverview | null>(null);
  const [draft, setDraft] = useState<ControlForm | null>(null);
  const [ticker, setTicker] = useState("");
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState<string>("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      setData(await loadAgent());
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Failed to load agent overview");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const live = useMemo(() => formFromConfig(data?.config ?? {}), [data?.config]);
  const form = draft ?? live;
  const running = busy === FORCE_JOB;
  const hasCommand = Boolean(form.command.trim());
  const autoRun = form.enabled;

  const saveControls = async (overrides: Partial<ControlForm> = {}) => {
    const next = { ...form, ...overrides };
    setDraft(next);
    setBusy("save");
    setError("");
    setMessage("");
    try {
      await updateAgentSettings({ option_agent: toPayload(next) });
      setDraft(null);
      setMessage("Agent settings saved to config.yaml.");
      await refresh();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Failed to save agent settings");
    } finally {
      setBusy("");
    }
  };

  const runNow = async () => {
    setBusy(FORCE_JOB);
    setError("");
    setMessage("");
    try {
      await startRefreshJob(FORCE_JOB);
      setMessage("Agent run started.");
      await refresh();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Failed to start agent run");
    } finally {
      setBusy("");
    }
  };

  const analyze = async () => {
    const symbol = ticker.trim().toUpperCase();
    if (!symbol) return;
    setBusy("analyze");
    setError("");
    setMessage("");
    try {
      const result = await analyzeTicker(symbol, prompt.trim() || undefined);
      setMessage(`Queued ${symbol} for analysis (request ${result.request_id.slice(0, 12)}…). Run started.`);
      setTicker("");
      setPrompt("");
      await refresh();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Failed to queue analysis");
    } finally {
      setBusy("");
    }
  };

  const cost = data?.cost;
  const queue = data?.queue;
  const metrics: MetricSpec[] = [
    ["Auto-run", autoRun ? "On" : "Off", autoRun ? "scheduled pass enabled" : "scheduled pass paused", autoRun ? "good" : "muted"],
    ["On-demand", hasCommand ? "Ready" : "No command", hasCommand ? "run / analyze available" : "set a command below", hasCommand ? "good" : "warn"],
    ["Open queue", (queue?.total_open ?? 0).toLocaleString(), `${queue?.thesis_open ?? 0} thesis · ${queue?.postmortem_open ?? 0} pm`, queue?.total_open ? "warn" : "good"],
    ["Cost today", cost ? `$${cost.today.est_cost_usd.toFixed(4)}` : "—", `${(cost?.today.input_tokens ?? 0).toLocaleString()} in / ${(cost?.today.output_tokens ?? 0).toLocaleString()} out tok`, cost?.today.est_cost_usd ? "info" : "muted"],
    ["Cost 7d", cost ? `$${cost.last_7d.est_cost_usd.toFixed(4)}` : "—", `${cost?.last_7d.runs ?? 0} runs`, "info"],
  ];

  return (
    <WorkspacePage
      eyebrow="Control plane"
      title="Agent"
      subtitle="Full control over how the option agent analyzes each ticker — config, on-demand runs, context, and cost."
      metrics={metrics}
      actions={
        <Button type="button" disabled={running || !hasCommand} onClick={() => void runNow()} title={hasCommand ? "Run the consolidated agent now (independent of auto-run)" : "Set the agent command first"}>
          {running ? <Loader2 className="size-4 animate-spin" /> : <Play className="size-4" />}
          {running ? "Running" : "Run now"}
        </Button>
      }
    >
      {message ? <Notice tone="good">{message}</Notice> : null}
      {error ? <Notice tone="bad">{error}</Notice> : null}

      {/* On-demand analysis */}
      <DataTableFrame title="On-demand analysis">
        <div className="space-y-3 p-4">
          <p className="text-sm text-muted-foreground">
            Analyze any ticker now — even one without an option candidate. The agent receives the full per-ticker context; an optional
            custom prompt is appended to the default instructions.
          </p>
          <div className="flex flex-col gap-3 lg:flex-row lg:items-start">
            <div className="lg:w-40">
              <label className="text-xs font-medium text-muted-foreground">Ticker</label>
              <Input value={ticker} onChange={(e) => setTicker(e.target.value)} placeholder="NVDA" className="uppercase" />
            </div>
            <div className="flex-1">
              <label className="text-xs font-medium text-muted-foreground">Custom prompt (optional)</label>
              <textarea
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                rows={2}
                placeholder="e.g. Focus on datacenter GPU demand and the impact of the latest export rules."
                className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
              />
            </div>
            <div className="lg:pt-5">
              <Button type="button" disabled={busy === "analyze" || !ticker.trim() || !hasCommand} onClick={() => void analyze()}>
                {busy === "analyze" ? <Loader2 className="size-4 animate-spin" /> : <Send className="size-4" />}
                Analyze
              </Button>
            </div>
          </div>
          {!hasCommand ? <p className="text-xs text-amber-600 dark:text-amber-400">Set the agent command below to run on-demand analyses (auto-run is a separate toggle).</p> : null}
        </div>
      </DataTableFrame>

      {/* Controls */}
      <DataTableFrame
        title="Configuration"
        action={<StatusBadge tone={autoRun ? "good" : "muted"}>{autoRun ? "Auto-run on" : "Auto-run off"}</StatusBadge>}
      >
        <div className="space-y-4 p-4">
          <div className="flex items-start gap-3">
            <div className="rounded-md border border-border bg-muted p-2"><BrainCircuit className="size-5 text-muted-foreground" /></div>
            <p className="text-sm leading-6 text-muted-foreground">
              One consolidated pass covers all open thesis + postmortem requests in a single call. Saved to config.yaml; cadence changes apply on app restart.
            </p>
          </div>

          <label className="flex items-center justify-between gap-3 rounded-lg border border-border bg-background px-3 py-2">
            <span>
              <span className="block text-sm font-medium">Auto-run (scheduled)</span>
              <span className="block text-xs text-muted-foreground">Runs the agent automatically on the cadence below. On-demand &amp; Run now work without this.</span>
            </span>
            <input type="checkbox" checked={form.enabled} disabled={busy === "save"} onChange={(e) => void saveControls({ enabled: e.target.checked })} className="size-5 accent-primary" />
          </label>

          <Field label="Command"><Input value={form.command} onChange={(e) => setDraft({ ...form, command: e.target.value })} placeholder="market-codex-option-agent" /></Field>

          <div className="grid gap-3 sm:grid-cols-3">
            <SelectField label="Provider" value={form.provider} options={["codex", "openai"]} onChange={(v) => setDraft({ ...form, provider: v })} />
            <Field label="Model"><Input value={form.model} onChange={(e) => setDraft({ ...form, model: e.target.value })} placeholder="(provider default)" /></Field>
            <SelectField label="Reasoning effort" value={form.reasoning_effort} options={["", "minimal", "low", "medium", "high"]} onChange={(v) => setDraft({ ...form, reasoning_effort: v })} />
          </div>

          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <NumberField label="Thesis cap" value={form.thesis_limit} min={0} max={50} onChange={(v) => setDraft({ ...form, thesis_limit: v })} />
            <NumberField label="Postmortem cap" value={form.postmortem_limit} min={0} max={50} onChange={(v) => setDraft({ ...form, postmortem_limit: v })} />
            <NumberField label="Timeout (s)" value={form.timeout_seconds} min={10} max={900} onChange={(v) => setDraft({ ...form, timeout_seconds: v })} />
            <NumberField label="Max runs/day" value={form.max_runs_per_day} min={0} max={48} onChange={(v) => setDraft({ ...form, max_runs_per_day: v })} />
          </div>

          <Field label="Auto-run cadence (seconds, 0 = use env default; applies on restart)">
            <Input type="number" min={0} value={form.auto_run_seconds} onChange={(e) => setDraft({ ...form, auto_run_seconds: boundedInt(e.target.value, 0, 604800) })} />
          </Field>

          <div>
            <div className="mb-1.5 text-xs font-medium uppercase text-muted-foreground">Per-ticker context fed to each run</div>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              {CONTEXT_KEYS.map((key) => (
                <label key={key} className="flex items-center gap-2 rounded-md border border-border bg-background px-2 py-1.5 text-sm">
                  <input
                    type="checkbox"
                    checked={form.context_sources[key] ?? true}
                    onChange={(e) => setDraft({ ...form, context_sources: { ...form.context_sources, [key]: e.target.checked } })}
                    className="size-4 accent-primary"
                  />
                  {titleLabel(key)}
                </label>
              ))}
            </div>
            <p className="mt-1 text-xs text-muted-foreground">Toggle a source off to trim the prompt and reduce input tokens/cost.</p>
          </div>

          <div className="flex justify-end">
            <Button type="button" variant="outline" disabled={busy === "save" || !draft} onClick={() => void saveControls()}>
              <Save className={busy === "save" ? "animate-pulse" : undefined} />
              {busy === "save" ? "Saving" : "Save configuration"}
            </Button>
          </div>
        </div>
      </DataTableFrame>

      {/* Run history + cost */}
      <DataTableFrame title="Run history & cost">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[920px] text-sm">
            <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
              <tr>
                <th className="px-3 py-3">Started</th>
                <th className="px-3 py-3">Trigger</th>
                <th className="px-3 py-3">Ticker</th>
                <th className="px-3 py-3">Model</th>
                <th className="px-3 py-3">Tokens (in/out)</th>
                <th className="px-3 py-3">Est. cost</th>
                <th className="px-3 py-3">Thesis</th>
                <th className="px-3 py-3">Postmortem</th>
                <th className="px-3 py-3">Status</th>
              </tr>
            </thead>
            <tbody>
              {(data?.runs ?? []).map((run, index) => <RunRow key={run.id ?? index} run={run} />)}
              {!data?.runs?.length ? (
                <tr><td colSpan={9} className="px-4 py-6 text-sm text-muted-foreground">No agent runs recorded yet.</td></tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </DataTableFrame>
    </WorkspacePage>
  );
}

function RunRow({ run }: { run: AgentRun }) {
  const estimated = run.tokens_estimated ? " ~" : "";
  return (
    <tr className="border-b border-border align-top hover:bg-accent/40">
      <td className="whitespace-nowrap px-3 py-3 text-muted-foreground">{formatTime(run.started_at)}</td>
      <td className="px-3 py-3">{titleLabel(run.trigger || "scheduled")}</td>
      <td className="px-3 py-3 font-medium">{run.ticker || "—"}</td>
      <td className="px-3 py-3 text-muted-foreground">{run.model || run.provider || "—"}</td>
      <td className="px-3 py-3 tabular-nums">{(run.input_tokens ?? 0).toLocaleString()}{estimated} / {(run.output_tokens ?? 0).toLocaleString()}</td>
      <td className="px-3 py-3 tabular-nums">${Number(run.est_cost_usd ?? 0).toFixed(4)}</td>
      <td className="px-3 py-3 tabular-nums">{run.thesis_accepted ?? 0}/{run.thesis_attempted ?? 0}</td>
      <td className="px-3 py-3 tabular-nums">{run.postmortem_accepted ?? 0}/{run.postmortem_attempted ?? 0}</td>
      <td className="px-3 py-3"><StatusBadge tone={toneFromText(run.status || "")}>{titleLabel(run.status || "unknown")}</StatusBadge></td>
    </tr>
  );
}

function formFromConfig(config: Record<string, unknown>): ControlForm {
  const sources = (config.context_sources && typeof config.context_sources === "object" ? config.context_sources : {}) as Record<string, boolean>;
  return {
    enabled: Boolean(config.enabled),
    command: strFrom(config.command, ""),
    provider: strFrom(config.provider, "codex"),
    model: strFrom(config.model, ""),
    reasoning_effort: strFrom(config.reasoning_effort, ""),
    timeout_seconds: numFrom(config.timeout_seconds, 180),
    thesis_limit: numFrom(config.thesis_limit, 8),
    postmortem_limit: numFrom(config.postmortem_limit, 4),
    auto_run_seconds: numFrom(config.auto_run_seconds, 0),
    max_runs_per_day: numFrom(config.max_runs_per_day, 1),
    context_sources: Object.fromEntries(CONTEXT_KEYS.map((k) => [k, sources[k] ?? true])),
  };
}

function toPayload(form: ControlForm): OptionAgentSettingsInput {
  return {
    enabled: form.enabled,
    command: form.command,
    provider: form.provider,
    model: form.model,
    reasoning_effort: form.reasoning_effort,
    timeout_seconds: form.timeout_seconds,
    thesis_limit: form.thesis_limit,
    postmortem_limit: form.postmortem_limit,
    auto_run_seconds: form.auto_run_seconds,
    max_runs_per_day: form.max_runs_per_day,
    context_sources: form.context_sources,
  };
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block space-y-1">
      <span className="text-xs font-medium text-muted-foreground">{label}</span>
      {children}
    </label>
  );
}

function NumberField({ label, value, min, max, onChange }: { label: string; value: number; min: number; max: number; onChange: (v: number) => void }) {
  return (
    <Field label={label}>
      <Input type="number" min={min} max={max} value={value} onChange={(e) => onChange(boundedInt(e.target.value, min, max))} />
    </Field>
  );
}

function SelectField({ label, value, options, onChange }: { label: string; value: string; options: string[]; onChange: (v: string) => void }) {
  return (
    <Field label={label}>
      <select value={value} onChange={(e) => onChange(e.target.value)} className="h-9 w-full rounded-md border border-border bg-background px-3 text-sm">
        {options.map((opt) => <option key={opt} value={opt}>{opt || "(default)"}</option>)}
      </select>
    </Field>
  );
}

function Notice({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <div className="rounded-md border border-border bg-card px-4 py-3 text-sm"><StatusBadge tone={tone}>{children}</StatusBadge></div>;
}

function boundedInt(value: string, min: number, max: number): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return min;
  return Math.max(min, Math.min(max, Math.round(parsed)));
}

function numFrom(value: unknown, fallback: number): number {
  const parsed = typeof value === "number" ? value : typeof value === "string" ? Number(value) : Number.NaN;
  return Number.isFinite(parsed) ? parsed : fallback;
}

function strFrom(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : value == null ? fallback : String(value);
}

function formatTime(value: string | undefined | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}
