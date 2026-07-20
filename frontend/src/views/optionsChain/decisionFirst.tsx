import { useEffect, useState, type ComponentType } from "react";
import { loadOptionHistorySnapshots, loadOptionsCandidates, loadOptionsDecisionBrief, loadOptionsPaperJournal, type OptionHistorySnapshot, type OptionsDecisionBrief, type OptionsDecisionCandidate, type OptionsPaperJournalRow } from "@/api";
import { StatusBadge } from "@/components/market/workstation";
import { WorkspacePage } from "../workspacePage";

type Tab = "decision" | "discover" | "evidence" | "journal" | "learn";

export function DecisionFirstOptionsChainPage({ EvidenceWorkspace }: { EvidenceWorkspace: ComponentType }) {
  const [tab, setTab] = useState<Tab>("decision");
  const [lane, setLane] = useState<"thesis" | "anomaly">("thesis");
  const [brief, setBrief] = useState<OptionsDecisionBrief | null>(null);
  const [snapshots, setSnapshots] = useState<OptionHistorySnapshot[]>([]);
  const [candidates, setCandidates] = useState<OptionsDecisionCandidate[]>([]);
  const [journal, setJournal] = useState<OptionsPaperJournalRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void Promise.all([loadOptionsDecisionBrief("QQQ", lane, controller.signal), loadOptionHistorySnapshots("QQQ")])
      .then(([nextBrief, nextSnapshots]) => { setBrief(nextBrief); setSnapshots(nextSnapshots.rows); setError(null); })
      .catch((cause: unknown) => { if ((cause as { name?: string }).name !== "AbortError") setError(message(cause)); });
    return () => controller.abort();
  }, [lane]);

  useEffect(() => {
    if (tab !== "discover") return;
    const controller = new AbortController();
    void loadOptionsCandidates({ symbol: "QQQ", lane, limit: 100 }, controller.signal)
      .then((payload) => { setCandidates(payload.rows); setError(null); })
      .catch((cause: unknown) => { if ((cause as { name?: string }).name !== "AbortError") setError(message(cause)); });
    return () => controller.abort();
  }, [tab, lane]);

  useEffect(() => {
    if (tab !== "journal") return;
    const controller = new AbortController();
    void loadOptionsPaperJournal("QQQ", controller.signal)
      .then((payload) => { setJournal(payload.rows); setError(null); })
      .catch((cause: unknown) => { if ((cause as { name?: string }).name !== "AbortError") setError(message(cause)); });
    return () => controller.abort();
  }, [tab]);

  const candidate = brief?.strongest_candidate;
  return <WorkspacePage eyebrow="QQQ underwriting" title="Options Decision System" subtitle="Paper-only underwriting. Options Radar remains the broad discovery surface." metrics={[
    ["State", brief?.state ?? "COLLECTING", brief?.paper_only ? "Paper-only; no live order submission." : "Awaiting compact decision brief.", tone(brief?.state)],
    ["Lane", lane === "thesis" ? "Thesis-led" : "Anomaly research", "Lanes are deliberately not universally ranked.", "info"],
    ["Captures", `${snapshots.length}`, snapshots[0] ? "Latest complete generation is available as evidence." : "Waiting for a complete capture.", snapshots[0] ? "good" : "warn"],
  ]} actions={<div className="flex items-center gap-2"><label className="text-xs">Lane <select className="ml-1 h-9 rounded border bg-background px-2 text-sm" value={lane} onChange={(event) => setLane(event.target.value as "thesis" | "anomaly")}><option value="thesis">Thesis</option><option value="anomaly">Anomaly</option></select></label><StatusBadge tone={brief?.mode === "paper" ? "warn" : "info"}>{brief?.mode ?? "shadow"} mode</StatusBadge></div>}>
    {error ? <p className="rounded border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{error}</p> : null}
    <div className="flex w-full gap-1 overflow-x-auto rounded-md border border-border bg-muted p-1">{(["decision", "discover", "evidence", "journal", "learn"] as Tab[]).map((value) => <button key={value} type="button" onClick={() => setTab(value)} className={`min-h-11 rounded px-3 text-sm capitalize ${tab === value ? "bg-background shadow-sm" : "text-muted-foreground"}`}>{label(value)}</button>)}</div>
    {tab === "decision" ? <Decision brief={brief} candidate={candidate} /> : null}
    {tab === "discover" ? <Discover rows={candidates} lane={lane} /> : null}
    {tab === "evidence" ? <EvidenceWorkspace /> : null}
    {tab === "journal" ? <Journal rows={journal} /> : null}
    {tab === "learn" ? <Learn brief={brief} /> : null}
  </WorkspacePage>;
}

function Decision({ brief, candidate }: { brief: OptionsDecisionBrief | null; candidate: Record<string, unknown> | null | undefined }) {
  if (!brief) return <Empty text="Loading the compact decision brief…" />;
  if (!candidate) return <Empty text={String(brief.summary.message ?? "No QQQ candidate has cleared the evidence gate.")} />;
  return <section className="grid gap-4 rounded-lg border border-border bg-card p-4 md:grid-cols-2"><div><h2 className="text-lg font-medium">{String(candidate.classification ?? "Candidate")}</h2><p className="mt-1 text-sm text-muted-foreground">{String(candidate.option_type ?? "").toUpperCase()} {String(candidate.expiration ?? "")} · {money(candidate.strike)}</p><StatusBadge tone={tone(brief.state)}>{brief.state}</StatusBadge></div><dl className="grid grid-cols-2 gap-3 text-sm"><Metric label="Fair interval" value={`${money(candidate.fair_low)} – ${money(candidate.fair_high)}`} /><Metric label="Modeled net edge" value={money(candidate.modeled_net_edge)} /><Metric label="Confidence" value={percent(candidate.confidence)} /><Metric label="Cohort" value={String(candidate.capture_generation_id ?? "—")} /></dl><p className="md:col-span-2 text-sm text-muted-foreground">Historical price-shape evidence is not a fill guarantee. A paper-ready action requires the exact thesis, calibration, and a later coherent quote cohort.</p></section>;
}

function Discover({ rows, lane }: { rows: OptionsDecisionCandidate[]; lane: string }) { return <section className="rounded-lg border border-border bg-card p-4"><h2 className="font-medium">{lane === "anomaly" ? "Research evidence" : "Thesis candidates"}</h2>{rows.length ? <div className="mt-3 divide-y text-sm">{rows.map((row) => <div className="grid gap-1 py-3 sm:grid-cols-4" key={row.decision_id}><span>{row.structure}</span><span>{row.expiration} {money(row.strike)}</span><span>{row.paper_state}</span><span>{money(row.modeled_net_edge)}</span></div>)}</div> : <Empty text="No published candidate matches this lane. Anomaly evidence stays research-only until a directional thesis is adopted." />}</section>; }
function Journal({ rows }: { rows: OptionsPaperJournalRow[] }) { return <section className="rounded-lg border border-border bg-card p-4"><h2 className="font-medium">Paper journal</h2>{rows.length ? <div className="mt-3 divide-y text-sm">{rows.map((row) => <div className="grid gap-1 py-3 sm:grid-cols-4" key={row.shadow_id}><span>{row.status}</span><span>{String(row.structure ?? "—")}</span><span>{String(row.fill_basis ?? "pending")}</span><span>{String(row.pending_entry_reason ?? "—")}</span></div>)}</div> : <Empty text="No system shadows or paper actions exist yet." />}</section>; }
function Learn({ brief }: { brief: OptionsDecisionBrief | null }) { return <section className="rounded-lg border border-border p-4"><h2 className="font-medium">Learning gates</h2><p className="mt-2 text-sm text-muted-foreground">PAPER_READY requires 30 mature outcomes for the exact structure, regime, and model revision, a positive lower-95% expectancy, and Brier score at or below 0.25. Current status: {brief?.state ?? "COLLECTING"}.</p></section>; }
function Metric({ label, value }: { label: string; value: string }) { return <div><dt className="text-xs text-muted-foreground">{label}</dt><dd className="mt-0.5 font-medium">{value}</dd></div>; }
function Empty({ text }: { text: string }) { return <p className="rounded-lg border border-dashed p-6 text-sm text-muted-foreground">{text}</p>; }
function label(tab: Tab) { return tab === "decision" ? "Decision Brief" : tab === "discover" ? "Discover" : tab === "evidence" ? "Surface & Chain" : tab === "journal" ? "Paper Journal" : "Learn"; }
function tone(state?: string): "good" | "warn" | "info" | "muted" { return state === "PAPER_READY" ? "good" : state === "REJECT" ? "warn" : state === "WATCH" ? "info" : "muted"; }
function money(value: unknown) { const parsed = typeof value === "number" ? value : Number(value); return Number.isFinite(parsed) ? parsed.toLocaleString(undefined, { style: "currency", currency: "USD" }) : "—"; }
function percent(value: unknown) { const parsed = typeof value === "number" ? value : Number(value); return Number.isFinite(parsed) ? parsed.toLocaleString(undefined, { style: "percent", maximumFractionDigits: 1 }) : "—"; }
function message(value: unknown) { return value instanceof Error ? value.message : "Unable to load options decision data"; }
