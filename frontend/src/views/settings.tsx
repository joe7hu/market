import { BrainCircuit, Clock3, MessageCircle, Save, SlidersHorizontal, Terminal } from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";

import { loadSettings, updateAgentSettings, updateResearchSources, type AgentSettingsInput, type ResearchSourcesInput } from "@/api";
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

type OptionAgentForm = {
  enabled: boolean;
  command: string;
  timeout_seconds: number;
  thesis_limit: number;
  postmortem_limit: number;
};

const EMPTY_OPTION_AGENT: OptionAgentForm = {
  enabled: false,
  command: "market-codex-option-agent",
  timeout_seconds: 180,
  thesis_limit: 8,
  postmortem_limit: 4,
};

type ResearchForm = {
  xEnabled: boolean;
  listId: string;
  priorityHandles: string;
  xLimit: number;
  newsEnabled: boolean;
  newsProviders: string;
  blogsEnabled: boolean;
  substackUrls: string;
};

const EMPTY_RESEARCH: ResearchForm = {
  xEnabled: true,
  listId: "",
  priorityHandles: "balajis, karpathy, citrini, BillAckman, dylan522p, IncomeSharks",
  xLimit: 30,
  newsEnabled: true,
  newsProviders: "bloomberg, reuters, google-news, hackernews",
  blogsEnabled: true,
  substackUrls: "",
};

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
  const researchConfig = useMemo(
    () => researchFromSettings(settings.config?.research_sources ?? statusConfig.research_sources),
    [settings.config, statusConfig.research_sources],
  );
  const [draft, setDraft] = useState<{ option_thesis: AgentForm; option_postmortem: AgentForm; option_agent: OptionAgentForm } | null>(null);
  const [researchDraft, setResearchDraft] = useState<ResearchForm | null>(null);
  const [saving, setSaving] = useState(false);
  const [savingSources, setSavingSources] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const thesis = draft?.option_thesis ?? agentConfig.option_thesis;
  const postmortem = draft?.option_postmortem ?? agentConfig.option_postmortem;
  const optionAgent = draft?.option_agent ?? agentConfig.option_agent;
  const research = researchDraft ?? researchConfig;
  const updateDraft = (patch: Partial<{ option_thesis: AgentForm; option_postmortem: AgentForm; option_agent: OptionAgentForm }>) =>
    setDraft({ option_thesis: thesis, option_postmortem: postmortem, option_agent: optionAgent, ...patch });

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
    const payload: AgentSettingsInput = { option_thesis: thesis, option_postmortem: postmortem, option_agent: optionAgent };
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

  const saveSources = async () => {
    setSavingSources(true);
    setError("");
    setMessage("");
    const payload: ResearchSourcesInput = {
      x: {
        enabled: research.xEnabled,
        list_id: research.listId,
        priority_handles: splitList(research.priorityHandles),
        limit: research.xLimit,
      },
      news: { enabled: research.newsEnabled, providers: splitList(research.newsProviders) },
      blogs: { enabled: research.blogsEnabled, substack_urls: splitList(research.substackUrls) },
    };
    try {
      const saved = await updateResearchSources(payload);
      setDirectSettings(saved);
      await loadScope("settings");
      setResearchDraft(null);
      setMessage("Research & social sources saved to config.yaml.");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Failed to save research sources");
    } finally {
      setSavingSources(false);
    }
  };

  return (
    <WorkspacePage
      eyebrow="Configuration"
      title="Agent & Source Control"
      subtitle="Runtime knobs for the consolidated option agent and the live X / news / blog source pulls."
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

      <OptionAgentCard
        value={optionAgent}
        runtime={jsonRecord(agentRuntime.option_agent)}
        loaded={settingsReady}
        onChange={(next) => updateDraft({ option_agent: next })}
      />

      <ResearchSourcesCard
        value={research}
        loaded={settingsReady}
        saving={savingSources}
        onChange={setResearchDraft}
        onSave={() => void saveSources()}
      />

      <div className="grid gap-4 xl:grid-cols-2">
        <AgentSettingsCard
          title="Option Thesis Worker (legacy split)"
          description="Generates product-grounded hypotheses for top-ranked options candidates. Used only when the consolidated Option Agent is disabled."
          value={thesis}
          runtime={jsonRecord(agentRuntime.option_thesis)}
          loaded={settingsReady}
          onChange={(next) => updateDraft({ option_thesis: next })}
        />
        <AgentSettingsCard
          title="Option Postmortem Worker (legacy split)"
          description="Explains important outcomes and drafts strategy mutation proposals. Used only when the consolidated Option Agent is disabled."
          value={postmortem}
          runtime={jsonRecord(agentRuntime.option_postmortem)}
          loaded={settingsReady}
          onChange={(next) => updateDraft({ option_postmortem: next })}
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

function OptionAgentCard({
  value,
  runtime,
  loaded,
  onChange,
}: {
  value: OptionAgentForm;
  runtime: Record<string, unknown>;
  loaded: boolean;
  onChange: (value: OptionAgentForm) => void;
}) {
  const active = booleanFromJson(runtime.active, value.enabled && Boolean(value.command));
  const configured = booleanFromJson(runtime.configured, Boolean(value.command));
  const tone: Tone = !loaded ? "muted" : active ? "good" : configured ? "warn" : "muted";
  return (
    <DataTableFrame
      title="Option Agent (consolidated)"
      action={<StatusBadge tone={tone}>{!loaded ? "Loading" : active ? "Active" : configured ? "Paused" : "Not configured"}</StatusBadge>}
    >
      <div className="space-y-4 p-4">
        <div className="flex items-start gap-3">
          <div className="rounded-md border border-border bg-muted p-2"><BrainCircuit className="size-5 text-muted-foreground" /></div>
          <p className="text-sm leading-6 text-muted-foreground">
            Single batched pass: thesis + postmortem are generated in one LLM/codex call to save tokens. When enabled, this
            replaces the legacy split workers below.
          </p>
        </div>

        <label className="flex items-center justify-between gap-3 rounded-lg border border-border bg-background px-3 py-2">
          <span>
            <span className="block text-sm font-medium">Consolidated agent enabled</span>
            <span className="block text-xs text-muted-foreground">When on, run_option_agents uses one batched call instead of the two split workers.</span>
          </span>
          <input
            type="checkbox"
            checked={value.enabled}
            disabled={!loaded}
            onChange={(event) => onChange({ ...value, enabled: event.target.checked })}
            className="size-5 accent-primary"
          />
        </label>

        <Field label="Command" detail="Local executable invoked once per pass with the batched payload.">
          <Input value={value.command} onChange={(event) => onChange({ ...value, command: event.target.value })} placeholder="market-codex-option-agent" />
        </Field>

        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Thesis cap" detail="Max thesis requests per pass.">
            <Input type="number" min={0} max={50} value={value.thesis_limit} onChange={(event) => onChange({ ...value, thesis_limit: boundedNumber(event.target.value, 0, 50) })} />
          </Field>
          <Field label="Postmortem cap" detail="Max postmortem requests per pass.">
            <Input type="number" min={0} max={50} value={value.postmortem_limit} onChange={(event) => onChange({ ...value, postmortem_limit: boundedNumber(event.target.value, 0, 50) })} />
          </Field>
          <Field label="Timeout seconds" detail="External command timeout.">
            <Input type="number" min={10} max={900} value={value.timeout_seconds} onChange={(event) => onChange({ ...value, timeout_seconds: boundedNumber(event.target.value, 10, 900) })} />
          </Field>
        </div>

        <div className="grid gap-2 text-xs sm:grid-cols-3">
          <RuntimeChip label="Mode" value={stringFromJson(runtime.mode, "consolidated")} />
          <RuntimeChip label="Cadence" value={stringFromJson(runtime.cadence, "daily_premarket")} />
          <RuntimeChip label="Request cap" value={stringFromJson(runtime.request_cap, "-")} />
        </div>
      </div>
    </DataTableFrame>
  );
}

function ResearchSourcesCard({
  value,
  loaded,
  saving,
  onChange,
  onSave,
}: {
  value: ResearchForm;
  loaded: boolean;
  saving: boolean;
  onChange: (value: ResearchForm) => void;
  onSave: () => void;
}) {
  const xConfigured = value.xEnabled && Boolean(value.listId);
  const tone: Tone = !loaded ? "muted" : xConfigured ? "good" : value.xEnabled ? "warn" : "muted";
  return (
    <DataTableFrame
      title="Research & Social Sources"
      action={
        <div className="flex items-center gap-2">
          <StatusBadge tone={tone}>{!loaded ? "Loading" : xConfigured ? "X list set" : value.xEnabled ? "Needs list ID" : "Paused"}</StatusBadge>
          <Button type="button" size="sm" variant="outline" onClick={onSave} disabled={saving || !loaded}>
            <Save className={saving ? "animate-pulse" : undefined} />
            {saving ? "Saving" : "Save sources"}
          </Button>
        </div>
      }
    >
      <div className="space-y-5 p-4">
        <div className="flex items-start gap-3">
          <div className="rounded-md border border-border bg-muted p-2"><MessageCircle className="size-5 text-muted-foreground" /></div>
          <p className="text-sm leading-6 text-muted-foreground">
            Continuous coverage of important X accounts (beyond Arco bookmarks) plus news and blog ingestion via opencli.
            The X list is fetched in one paced call; per-account fetches are the staggered fallback.
          </p>
        </div>

        <div className="space-y-4 rounded-lg border border-border p-3">
          <div className="flex items-center justify-between">
            <span className="text-sm font-semibold">X / Social</span>
            <Toggle checked={value.xEnabled} disabled={!loaded} onChange={(next) => onChange({ ...value, xEnabled: next })} />
          </div>
          <Field label="X list ID" detail="Numeric ID of a curated X/Twitter list (opencli twitter lists). Leave empty to use per-account fetch only.">
            <Input value={value.listId} onChange={(event) => onChange({ ...value, listId: event.target.value })} placeholder="e.g. 1734567890123456789" />
          </Field>
          <Field label="Priority handles" detail="Comma-separated X handles for the per-account fallback (the @ is optional).">
            <Input value={value.priorityHandles} onChange={(event) => onChange({ ...value, priorityHandles: event.target.value })} placeholder="balajis, karpathy, citrini" />
          </Field>
          <Field label="Tweets per fetch" detail="Max tweets per list/account call.">
            <Input type="number" min={1} max={200} value={value.xLimit} onChange={(event) => onChange({ ...value, xLimit: boundedNumber(event.target.value, 1, 200) })} />
          </Field>
        </div>

        <div className="space-y-4 rounded-lg border border-border p-3">
          <div className="flex items-center justify-between">
            <span className="text-sm font-semibold">News</span>
            <Toggle checked={value.newsEnabled} disabled={!loaded} onChange={(next) => onChange({ ...value, newsEnabled: next })} />
          </div>
          <Field label="Providers" detail="Comma-separated opencli news adapters.">
            <Input value={value.newsProviders} onChange={(event) => onChange({ ...value, newsProviders: event.target.value })} placeholder="bloomberg, reuters, google-news, hackernews" />
          </Field>
        </div>

        <div className="space-y-4 rounded-lg border border-border p-3">
          <div className="flex items-center justify-between">
            <span className="text-sm font-semibold">Blogs & Memos</span>
            <Toggle checked={value.blogsEnabled} disabled={!loaded} onChange={(next) => onChange({ ...value, blogsEnabled: next })} />
          </div>
          <Field label="Substack URLs" detail="Comma- or newline-separated Substack publication URLs.">
            <Input value={value.substackUrls} onChange={(event) => onChange({ ...value, substackUrls: event.target.value })} placeholder="https://www.example.substack.com" />
          </Field>
        </div>
      </div>
    </DataTableFrame>
  );
}

function Toggle({ checked, disabled, onChange }: { checked: boolean; disabled: boolean; onChange: (next: boolean) => void }) {
  return (
    <input
      type="checkbox"
      checked={checked}
      disabled={disabled}
      onChange={(event) => onChange(event.target.checked)}
      className="size-5 accent-primary"
    />
  );
}

function RuntimePanel({ scheduler, modelOverrides }: { scheduler: Record<string, unknown>; modelOverrides: Record<string, unknown> }) {
  const rows = [
    ["Agent refresh", stringFromJson(scheduler.agent_refresh_seconds, "0"), "0 keeps in-app agent runs disabled"],
    ["Radar refresh", stringFromJson(scheduler.radar_refresh_seconds, "900"), "fast deterministic signal cadence"],
    ["Source refresh", stringFromJson(scheduler.source_refresh_seconds, "3600"), "option-chain/source pull cadence"],
    ["Learning refresh", stringFromJson(scheduler.learning_refresh_seconds, "21600"), "heavy deterministic learning cadence"],
    ["Social refresh", stringFromJson(scheduler.social_refresh_seconds, "1800"), "X / social pull cadence (MARKET_SOCIAL_REFRESH_SECONDS)"],
    ["Research refresh", stringFromJson(scheduler.research_refresh_seconds, "3600"), "news / blogs pull cadence (MARKET_RESEARCH_REFRESH_SECONDS)"],
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

function agentConfigFromSettings(value: unknown): { option_thesis: AgentForm; option_postmortem: AgentForm; option_agent: OptionAgentForm } {
  const agents = jsonRecord(value);
  return {
    option_thesis: agentForm(jsonRecord(agents.option_thesis), { ...EMPTY_AGENT, command: "market-codex-option-thesis-agent", limit: 8, timeout_seconds: 180 }),
    option_postmortem: agentForm(jsonRecord(agents.option_postmortem), { ...EMPTY_AGENT, command: "market-codex-option-postmortem-agent", limit: 4, timeout_seconds: 180 }),
    option_agent: optionAgentForm(jsonRecord(agents.option_agent)),
  };
}

function optionAgentForm(value: Record<string, unknown>): OptionAgentForm {
  return {
    enabled: booleanFromJson(value.enabled, EMPTY_OPTION_AGENT.enabled),
    command: stringFromJson(value.command, EMPTY_OPTION_AGENT.command),
    timeout_seconds: numberFromJson(value.timeout_seconds, EMPTY_OPTION_AGENT.timeout_seconds),
    thesis_limit: numberFromJson(value.thesis_limit, EMPTY_OPTION_AGENT.thesis_limit),
    postmortem_limit: numberFromJson(value.postmortem_limit, EMPTY_OPTION_AGENT.postmortem_limit),
  };
}

function researchFromSettings(value: unknown): ResearchForm {
  const research = jsonRecord(value);
  const x = jsonRecord(research.x);
  const news = jsonRecord(research.news);
  const blogs = jsonRecord(research.blogs);
  return {
    xEnabled: booleanFromJson(x.enabled, EMPTY_RESEARCH.xEnabled),
    listId: stringFromJson(x.list_id, EMPTY_RESEARCH.listId),
    priorityHandles: joinList(x.priority_handles, EMPTY_RESEARCH.priorityHandles),
    xLimit: numberFromJson(x.limit, EMPTY_RESEARCH.xLimit),
    newsEnabled: booleanFromJson(news.enabled, EMPTY_RESEARCH.newsEnabled),
    newsProviders: joinList(news.providers, EMPTY_RESEARCH.newsProviders),
    blogsEnabled: booleanFromJson(blogs.enabled, EMPTY_RESEARCH.blogsEnabled),
    substackUrls: joinList(blogs.substack_urls, ""),
  };
}

function splitList(value: string): string[] {
  const out: string[] = [];
  for (const raw of value.split(/[\n,]+/)) {
    const token = raw.trim().replace(/^@/, "");
    if (token && !out.includes(token)) out.push(token);
  }
  return out;
}

function joinList(value: unknown, fallback: string): string {
  if (Array.isArray(value)) return value.map((item) => String(item)).join(", ");
  if (typeof value === "string") return value;
  return fallback;
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
