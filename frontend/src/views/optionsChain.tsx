import { Suspense, lazy, useEffect, useMemo, useState, type ReactNode } from "react";
import { flexRender, getCoreRowModel, useReactTable, type ColumnDef } from "@tanstack/react-table";
import { useSearchParams } from "react-router-dom";
import { loadOptionHistoryAnomalies, loadOptionHistoryChain, loadOptionHistoryCurves, loadOptionHistorySnapshots, loadOptionHistorySurface, loadOptionHistorySurfaceGrid, loadOptionHistorySurfaceGroups, type OptionHistoryAnomaly, type OptionHistoryChainRow, type OptionHistoryCurves, type OptionHistoryPage, type OptionHistorySnapshot, type OptionHistorySurface, type OptionHistorySurfaceGrid, type OptionHistorySurfaceGroup } from "@/api";
import { StatusBadge } from "@/components/market/workstation";
import { blockerCopy } from "./optionsChain/decisionDesk";
import { DecisionFirstOptionsChainPage } from "./optionsChain/decisionFirst";

const OptionSurfacePlot = lazy(async () => ({ default: (await import("./optionsChainPlot")).OptionSurfacePlot }));
const OptionSurfaceExplorer = lazy(async () => ({ default: (await import("./optionsChainPlot")).OptionSurfaceExplorer }));
const OptionCurvePlots = lazy(async () => ({ default: (await import("./optionsChainPlot")).OptionCurvePlots }));
export const CHAIN_PAGE_SIZE = 10;
export const SURFACE_MONEYNESS_BOUND = 0.06;
export const SURFACE_MAX_DTE = 90;

export function OptionsChainPage() {
  return <DecisionFirstOptionsChainPage EvidenceWorkspace={EvidenceWorkspace} />;
}

export function EvidenceWorkspace({ embedded: _embedded = false }: { embedded?: boolean }) {
  const symbol = "QQQ";
  const [search, setSearch] = useSearchParams();
  const [snapshots, setSnapshots] = useState<OptionHistorySnapshot[]>([]);
  const [groups, setGroups] = useState<OptionHistorySurfaceGroup[]>([]);
  const [chain, setChain] = useState<OptionHistoryPage<OptionHistoryChainRow>>({ rows: [], count: 0, offset: 0, limit: CHAIN_PAGE_SIZE });
  const [surface, setSurface] = useState<OptionHistorySurface | null>(null);
  const [surfaceGrid, setSurfaceGrid] = useState<OptionHistorySurfaceGrid | null>(null);
  const [curves, setCurves] = useState<OptionHistoryCurves | null>(null);
  const [anomalies, setAnomalies] = useState<OptionHistoryPage<OptionHistoryAnomaly>>({ rows: [], count: 0, offset: 0, limit: 250 });
  const [error, setError] = useState<string | null>(null);
  const [webgl, setWebgl] = useState<boolean | null>(null);
  const snapshot = numberParam(search.get("snapshot"));
  const expiration = search.get("expiry") ?? "";
  const optionType = search.get("type") === "call" || search.get("type") === "put" ? search.get("type") : "";
  const fullChain = search.get("full_chain") === "1";
  const minMoneyness = fullChain ? "" : search.get("min") ?? "-0.10";
  const maxMoneyness = fullChain ? "" : search.get("max") ?? "0.10";
  const page = Math.max(0, Number(search.get("page") ?? "0") || 0);
  const view = search.get("evidence_view") === "surface" || search.get("evidence_view") === "curves" ? search.get("evidence_view") : "chain";
  const surfaceView = search.get("surface_view") === "evidence" ? "evidence" : "explorer";
  const update = (changes: Record<string, string | undefined>) => {
    const next = new URLSearchParams(search);
    for (const [key, value] of Object.entries(changes)) {
      if (value === undefined || value === "") next.delete(key); else next.set(key, value);
    }
    setSearch(next, { replace: true });
  };

  useEffect(() => {
    const canvas = document.createElement("canvas");
    setWebgl(Boolean(canvas.getContext("webgl") || canvas.getContext("experimental-webgl")));
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void loadOptionHistorySnapshots(symbol, controller.signal).then((result) => {
      setSnapshots(result.rows);
      if (!snapshot && result.rows[0]) update({ snapshot: String(result.rows[0].snapshot_id), page: "0" });
    }).catch(asError(setError));
    return () => controller.abort();
  }, []);
  useEffect(() => {
    setGroups([]); setChain({ rows: [], count: 0, offset: 0, limit: CHAIN_PAGE_SIZE }); setSurface(null); setSurfaceGrid(null);
    setCurves(null); setAnomalies({ rows: [], count: 0, offset: 0, limit: 250 }); setError(null);
    if (!snapshot) return;
    const controller = new AbortController();
    void loadOptionHistorySurfaceGroups({ symbol, snapshot }, controller.signal).then((result) => {
      setGroups(result.rows);
      const preferred = result.rows.find((group) => group.expiration === expiration && group.option_type === optionType);
      const first = preferred ?? [...result.rows].filter((group) => group.dte >= 7).sort((left, right) => Math.abs(left.dte - 30) - Math.abs(right.dte - 30))[0] ?? result.rows[0];
      if (first && (first.expiration !== expiration || first.option_type !== optionType)) update({ expiry: first.expiration, type: first.option_type, page: "0" });
    }).catch(asError(setError));
    return () => controller.abort();
  }, [symbol, snapshot]);
  useEffect(() => {
    if (!snapshot) return;
    const controller = new AbortController();
    const params = { symbol, snapshot, expiration: expiration || undefined, option_type: optionType || undefined, min_moneyness: minMoneyness || undefined, max_moneyness: maxMoneyness || undefined, offset: page * CHAIN_PAGE_SIZE, limit: CHAIN_PAGE_SIZE };
    void loadOptionHistoryChain(params, controller.signal).then((result) => { setChain(result); setError(null); }).catch(asError(setError));
    return () => controller.abort();
  }, [symbol, snapshot, expiration, optionType, minMoneyness, maxMoneyness, page]);
  useEffect(() => {
    if (view !== "surface" || !snapshot || !expiration || !optionType || surfaceView !== "evidence") return;
    const controller = new AbortController();
    void loadOptionHistorySurface({ symbol, snapshot, expiration, option_type: optionType }, controller.signal).then(setSurface).catch(asError(setError));
    return () => controller.abort();
  }, [symbol, snapshot, expiration, optionType, surfaceView, view]);
  useEffect(() => {
    if (view !== "surface" || surfaceView !== "explorer" || !snapshot || !optionType) return;
    setSurfaceGrid(null);
    const controller = new AbortController();
    void loadOptionHistorySurfaceGrid({
      symbol,
      snapshot,
      option_type: optionType,
      min_moneyness: -SURFACE_MONEYNESS_BOUND,
      max_moneyness: SURFACE_MONEYNESS_BOUND,
      max_dte: SURFACE_MAX_DTE,
    }, controller.signal)
      .then(setSurfaceGrid)
      .catch(asError(setError));
    return () => controller.abort();
  }, [symbol, snapshot, optionType, surfaceView, view]);
  useEffect(() => {
    if (view !== "curves" || !snapshot || !expiration) return;
    const controller = new AbortController();
    void Promise.all([loadOptionHistoryCurves({ symbol, snapshot, expiration }, controller.signal), loadOptionHistoryAnomalies({ symbol, snapshot, limit: 100 }, controller.signal)])
      .then(([nextCurves, nextAnomalies]) => { setCurves(nextCurves); setAnomalies(nextAnomalies); })
      .catch(asError(setError));
    return () => controller.abort();
  }, [symbol, snapshot, expiration, view]);

  const expiries = useMemo(() => [...new Set(groups.map((group) => group.expiration))], [groups]);
  const types = useMemo(() => [...new Set(groups.filter((group) => group.expiration === expiration).map((group) => group.option_type))], [groups, expiration]);
  const selected = snapshots.find((row) => row.snapshot_id === snapshot);
  const maxPage = Math.max(0, Math.ceil(chain.count / CHAIN_PAGE_SIZE) - 1);
  const filters = <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-5">
    <Select label="Snapshot" value={String(snapshot ?? "")} onChange={(value) => update({ snapshot: value || undefined, page: "0" })}><option value="" disabled>{snapshots.length ? "Choose capture" : "No complete capture"}</option>{snapshots.map((row) => <option key={row.snapshot_id} value={row.snapshot_id}>{formatCaptureWindow(row)} · {(row.completeness ?? 0).toLocaleString(undefined, { style: "percent", maximumFractionDigits: 1 })}</option>)}</Select>
    <Select label="Expiry" value={expiration} onChange={(value) => update({ expiry: value, type: groups.find((group) => group.expiration === value && group.option_type === optionType)?.option_type ?? groups.find((group) => group.expiration === value)?.option_type, page: "0" })}><option value="">Select expiry</option>{expiries.map((value) => <option key={value}>{value}</option>)}</Select>
    <Select label="Type" value={optionType ?? ""} onChange={(value) => update({ type: value, page: "0" })}><option value="">Select type</option>{types.map((value) => <option key={value} value={value}>{value === "call" ? "Calls" : "Puts"}</option>)}</Select>
    <Input label="Min log-moneyness" value={minMoneyness} onChange={(value) => update({ min: value, full_chain: undefined, page: "0" })} />
    <Input label="Max log-moneyness" value={maxMoneyness} onChange={(value) => update({ max: value, full_chain: undefined, page: "0" })} />
  </div>;

  return <div className="space-y-4">
    <section className="rounded-xl border border-border bg-card p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div><p className="text-xs font-semibold uppercase tracking-[0.16em] text-muted-foreground">Advanced evidence</p><h2 className="mt-1 text-lg font-semibold">Chain, volatility, and history</h2><p className="mt-1 max-w-3xl text-sm text-muted-foreground">Use this area to challenge the decision, inspect execution quality, and understand volatility structure. It is evidence—not a ranked trade list.</p></div>
        <div className="flex flex-wrap gap-2"><StatusBadge tone={selected ? "good" : "warn"}>{selected ? "Complete capture" : "No complete capture"}</StatusBadge>{selected ? <StatusBadge tone="info">{selected.contract_count.toLocaleString()} contracts · {(selected.completeness ?? 0).toLocaleString(undefined, { style: "percent", maximumFractionDigits: 1 })}</StatusBadge> : null}</div>
      </div>
    </section>
    {filters}
    <div className="flex flex-wrap items-center justify-between gap-2"><button type="button" className="min-h-11 rounded border px-3 text-sm" onClick={() => update(fullChain ? { full_chain: undefined, min: "-0.10", max: "0.10", page: "0" } : { full_chain: "1", min: undefined, max: undefined, page: "0" })}>{fullChain ? "Return to near-ATM chain" : "Expand to full chain"}</button>{error ? <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{error}</p> : null}</div>
    <div className="flex w-fit rounded-md border border-border bg-muted p-1"><Tab active={view === "chain"} onClick={() => update({ evidence_view: "chain" })}>Chain</Tab><Tab active={view === "surface"} onClick={() => update({ evidence_view: "surface" })}>IV Surface</Tab><Tab active={view === "curves"} onClick={() => update({ evidence_view: "curves" })}>Curves & History</Tab></div>
    {view === "chain" ? <ChainTable rows={chain.rows} page={page} maxPage={maxPage} count={chain.count} onPage={(next) => update({ page: String(next) })} /> : null}
    {view === "surface" ? <SurfacePanel snapshot={selected} surface={surface} surfaceGrid={surfaceGrid} optionType={(optionType || "call") as "call" | "put"} selectedDte={groups.find((group) => group.expiration === expiration && group.option_type === optionType)?.dte} selectedExpiration={expiration} surfaceView={surfaceView} onSurfaceViewChange={(next) => update({ surface_view: next })} webgl={webgl} /> : null}
    {view === "curves" ? <CurvesPanel curves={curves} anomalies={anomalies.rows} /> : null}
  </div>;
}

function ChainTable({ rows, page, maxPage, count, onPage }: { rows: OptionHistoryChainRow[]; page: number; maxPage: number; count: number; onPage: (page: number) => void }) {
  const columns = useMemo<ColumnDef<OptionHistoryChainRow>[]>(() => [
    { header: "Strike", accessorKey: "strike", cell: ({ getValue }) => money(getValue<number>()) },
    { header: "Bid / Ask", cell: ({ row }) => `${money(row.original.bid)} / ${money(row.original.ask)}` },
    { header: "Spread", cell: ({ row }) => spreadPercent(row.original) },
    { header: "IV", accessorKey: "provider_iv", cell: ({ getValue }) => percent(getValue<number | null>()) },
    { header: "Δ / Γ", cell: ({ row }) => `${number(row.original.provider_delta, 3)} / ${number(row.original.provider_gamma, 3)}` },
    { header: "Θ / Vega", cell: ({ row }) => `${number(row.original.provider_theta, 3)} / ${number(row.original.provider_vega, 3)}` },
    { header: "OI / Vol", cell: ({ row }) => `${integer(row.original.open_interest)} / ${integer(row.original.volume)}` },
    { header: "Bid / Ask size", cell: ({ row }) => `${integer(row.original.bid_size)} / ${integer(row.original.ask_size)}` },
    { header: "Evidence", cell: ({ row }) => chainEvidenceLabel(row.original) },
  ], []);
  const table = useReactTable({ data: rows, columns, getCoreRowModel: getCoreRowModel() });
  return <section className="rounded-lg border border-border bg-card"><div className="flex items-center justify-between gap-3 border-b border-border p-3 text-sm"><span>{count.toLocaleString()} contracts · {rows.length} shown</span><div className="flex items-center gap-2"><button className="min-h-11 rounded border px-3 disabled:opacity-50" disabled={page === 0} onClick={() => onPage(page - 1)}>Previous</button><span>Page {page + 1} / {maxPage + 1}</span><button className="min-h-11 rounded border px-3 disabled:opacity-50" disabled={page >= maxPage} onClick={() => onPage(page + 1)}>Next</button></div></div><div className="divide-y md:hidden">{rows.map((row) => <article key={row.contract_id} className="grid gap-1 p-3 text-sm"><strong>{row.option_type.toUpperCase()} {money(row.strike)} · {row.dte} DTE</strong><span>Bid/ask {money(row.bid)} / {money(row.ask)} · spread {spreadPercent(row)}</span><span>IV {percent(row.provider_iv)} · Δ {number(row.provider_delta, 3)} · Γ {number(row.provider_gamma, 3)}</span><span>Θ {number(row.provider_theta, 3)} · Vega {number(row.provider_vega, 3)} · OI / volume {integer(row.open_interest)} / {integer(row.volume)}</span><span className="text-muted-foreground">{chainEvidenceLabel(row)}</span></article>)}</div><div className="hidden overflow-x-auto md:block"><table className="min-w-[1080px] w-full text-left text-xs"><thead className="bg-muted text-muted-foreground">{table.getHeaderGroups().map((group) => <tr key={group.id}>{group.headers.map((header) => <th className="whitespace-nowrap px-3 py-2 font-medium" key={header.id}>{header.isPlaceholder ? null : flexRender(header.column.columnDef.header, header.getContext())}</th>)}</tr>)}</thead><tbody>{table.getRowModel().rows.map((row) => <tr className="border-t border-border" key={row.id}>{row.getVisibleCells().map((cell) => <td className="max-w-64 whitespace-nowrap px-3 py-2 tabular-nums" key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>)}</tr>)}</tbody></table></div>{rows.length === 0 ? <p className="p-5 text-sm text-muted-foreground">No complete-chain contracts match these filters.</p> : null}</section>;
}

export function spreadPercent(row: Pick<OptionHistoryChainRow, "bid" | "ask">): string {
  if (row.bid === null || row.ask === null || row.ask < row.bid) return "—";
  const midpoint = (row.bid + row.ask) / 2;
  return midpoint > 0 ? ((row.ask - row.bid) / midpoint).toLocaleString(undefined, { style: "percent", maximumFractionDigits: 1 }) : "—";
}

export function chainEvidenceLabel(row: Pick<OptionHistoryChainRow, "evidence_blockers" | "evidence_classification" | "quality_status" | "market_data_status">): string {
  const blockers = (row.evidence_blockers ?? []).slice(0, 2).map((blocker) => blockerCopy(blocker).label);
  if (blockers.length) return blockers.join(" · ");
  if (row.evidence_classification === "rejected") return "No relative-value edge";
  return sentenceCase(row.evidence_classification ?? row.quality_status ?? row.market_data_status ?? "Unscored evidence");
}

function SurfacePanel({ snapshot, surface, surfaceGrid, optionType, selectedDte, selectedExpiration, surfaceView, onSurfaceViewChange, webgl }: { snapshot: OptionHistorySnapshot | undefined; surface: OptionHistorySurface | null; surfaceGrid: OptionHistorySurfaceGrid | null; optionType: "call" | "put"; selectedDte?: number; selectedExpiration: string; surfaceView: "explorer" | "evidence"; onSurfaceViewChange: (view: "explorer" | "evidence") => void; webgl: boolean | null }) {
  if (!snapshot) return <Empty text="A complete snapshot is required before a surface can be shown." />;
  return <section className="rounded-lg border border-border bg-card p-2">
    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-2 pb-2 text-sm">
      <p className="text-muted-foreground">Near-money provider IV (±6% through 90 DTE); realized volatility remains a separate underwriting input.</p>
      <div className="flex rounded border border-border bg-muted p-0.5">
        <Tab active={surfaceView === "explorer"} onClick={() => onSurfaceViewChange("explorer")}>90-day surface</Tab>
        <Tab active={surfaceView === "evidence"} onClick={() => onSurfaceViewChange("evidence")}>Expiry smile</Tab>
      </div>
    </div>
    {surfaceView === "evidence" ? (!surface?.snapshot_id ? <p className="p-4 text-sm text-muted-foreground">Loading bounded expiry evidence…</p> : <Suspense fallback={<p className="p-4 text-sm text-muted-foreground">Loading bounded evidence chart…</p>}><OptionSurfacePlot surface={surface} /></Suspense>) : null}
    {surfaceView === "explorer" ? (!surfaceGrid?.snapshot_id ? <p className="p-4 text-sm text-muted-foreground">Loading bounded provider-IV surface…</p> : <Suspense fallback={<p className="p-4 text-sm text-muted-foreground">Loading surface explorer…</p>}><OptionSurfaceExplorer surface={surfaceGrid} optionType={optionType} selectedDte={selectedDte} selectedExpiration={selectedExpiration} webgl={webgl} /></Suspense>) : null}
  </section>;
}

function CurvesPanel({ curves, anomalies }: { curves: OptionHistoryCurves | null; anomalies: OptionHistoryAnomaly[] }) {
  return <div className="space-y-4"><section className="rounded-lg border border-border p-4"><div className="mb-3 flex items-center justify-between"><h2 className="font-medium">Curves and historical comparisons</h2><StatusBadge tone={curves?.history_state === "ready" ? "good" : "warn"}>{curves?.history_state ?? "collecting"}</StatusBadge></div>{curves ? <Suspense fallback={<p className="text-sm text-muted-foreground">Loading curves…</p>}><OptionCurvePlots curves={curves} /></Suspense> : <Empty text="A complete snapshot is required before curves can be shown." />}</section><div className="grid gap-4 xl:grid-cols-2"><section className="rounded-lg border border-border p-4"><h2 className="mb-3 font-medium">Observed smile points</h2><SmileTable curves={curves} /></section><section className="rounded-lg border border-border p-4"><h2 className="mb-3 font-medium">Statistical anomalies</h2><div className="max-h-[460px] overflow-auto"><table className="w-full text-left text-xs"><thead className="text-muted-foreground"><tr><th>Signal</th><th>State</th><th>Expiry</th><th>Z-score</th></tr></thead><tbody>{anomalies.map((row) => <tr className="border-t" key={row.id}><td className="py-2">{row.anomaly_type}</td><td><StatusBadge tone={row.state === "active" ? "warn" : "muted"}>{row.state}</StatusBadge></td><td>{row.expiration ?? "—"}</td><td>{number(row.z_score, 2)}</td></tr>)}</tbody></table>{anomalies.length === 0 ? <p className="py-4 text-sm text-muted-foreground">No threshold anomalies in this snapshot.</p> : null}</div></section></div></div>;
}

function SmileTable({ curves }: { curves: OptionHistoryCurves | null }) { const smiles = curves?.smiles ?? []; return <div className="max-h-[420px] overflow-auto"><table className="w-full text-left text-xs"><thead className="text-muted-foreground"><tr><th>Expiry</th><th>Type</th><th>DTE</th><th>Points</th></tr></thead><tbody>{smiles.map((smile, index) => <tr className="border-t" key={`${String(smile.expiration)}-${String(smile.option_type)}-${index}`}><td className="py-2">{String(smile.expiration ?? "—")}</td><td>{String(smile.option_type ?? "—")}</td><td>{String(smile.dte ?? "—")}</td><td>{Array.isArray(smile.points) ? smile.points.length : 0}</td></tr>)}</tbody></table>{smiles.length === 0 ? <p className="py-4 text-sm text-muted-foreground">No IV points available.</p> : null}</div>; }
function Tab({ active, onClick, children }: { active: boolean; onClick: () => void; children: string }) { return <button type="button" onClick={onClick} className={`min-h-11 rounded px-3 py-1.5 text-sm outline-offset-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary ${active ? "bg-background shadow-sm" : "text-muted-foreground"}`}>{children}</button>; }
function Select({ label, value, onChange, children }: { label: string; value: string; onChange: (value: string) => void; children: ReactNode }) { return <label className="grid gap-1 text-xs text-muted-foreground">{label}<select value={value} onChange={(event) => onChange(event.target.value)} className="min-h-11 rounded-md border border-input bg-background px-2 text-sm text-foreground">{children}</select></label>; }
function Input({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) { return <label className="grid gap-1 text-xs text-muted-foreground">{label}<input value={value} inputMode="decimal" onChange={(event) => onChange(event.target.value)} className="min-h-11 rounded-md border border-input bg-background px-2 text-sm text-foreground" /></label>; }
function Empty({ text }: { text: string }) { return <p className="rounded-lg border border-dashed p-6 text-sm text-muted-foreground">{text}</p>; }
function asError(setter: (value: string | null) => void) {
  return (error: unknown) => {
    // Every filter request owns an AbortController.  React's development
    // remount and a fast URL/filter change legitimately cancel the previous
    // request; treating that cancellation as an error leaves a misleading
    // red alert next to fresh evidence.
    const message = error instanceof Error ? error.message : "";
    if ((error as { name?: string } | null)?.name === "AbortError" || /aborted/i.test(message)) return;
    setter(message || "Unable to load option history");
  };
}
function formatDate(value: string) { return new Date(value).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }); }
function formatCaptureWindow(snapshot: OptionHistorySnapshot) { const start = snapshot.capture_started_at ?? snapshot.slot_at ?? snapshot.observed_at; const finish = snapshot.capture_finished_at; return finish ? `${formatDate(start)}–${new Date(finish).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : formatDate(start); }
function money(value: number | null | undefined) { return value === null || value === undefined ? "—" : value.toLocaleString(undefined, { style: "currency", currency: "USD" }); }
function percent(value: number | null | undefined) { return value === null || value === undefined ? "—" : value.toLocaleString(undefined, { style: "percent", maximumFractionDigits: 2 }); }
function number(value: number | null | undefined, digits = 2) { return value === null || value === undefined ? "—" : value.toFixed(digits); }
function integer(value: number | null | undefined) { return value === null || value === undefined ? "—" : value.toLocaleString(); }
function sentenceCase(value: string) { const text = value.replaceAll("_", " ").trim(); return text ? text.charAt(0).toUpperCase() + text.slice(1) : "Unknown"; }
function numberParam(value: string | null) { const parsed = Number(value); return Number.isInteger(parsed) && parsed > 0 ? parsed : undefined; }
