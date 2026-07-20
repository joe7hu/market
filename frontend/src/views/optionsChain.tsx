import { Suspense, lazy, useEffect, useMemo, useState, type ReactNode } from "react";
import { flexRender, getCoreRowModel, useReactTable, type ColumnDef } from "@tanstack/react-table";
import { loadOptionHistoryAnomalies, loadOptionHistoryChain, loadOptionHistoryCurves, loadOptionHistorySnapshots, loadOptionHistorySurface, type OptionHistoryAnomaly, type OptionHistoryChainRow, type OptionHistoryCurves, type OptionHistoryPage, type OptionHistorySnapshot, type OptionHistorySurface } from "@/api";
import { StatusBadge } from "@/components/market/workstation";
import { WorkspacePage } from "./workspacePage";

const OptionSurfacePlot = lazy(async () => ({ default: (await import("./optionsChainPlot")).OptionSurfacePlot }));
const OptionCurvePlots = lazy(async () => ({ default: (await import("./optionsChainPlot")).OptionCurvePlots }));
const PAGE_SIZE = 100;

export function OptionsChainPage() {
  const [symbol] = useState("QQQ");
  const [snapshots, setSnapshots] = useState<OptionHistorySnapshot[]>([]);
  const [snapshot, setSnapshot] = useState<number | undefined>();
  const [expiration, setExpiration] = useState("");
  const [optionType, setOptionType] = useState<"call" | "put" | "">("");
  const [minMoneyness, setMinMoneyness] = useState("");
  const [maxMoneyness, setMaxMoneyness] = useState("");
  const [page, setPage] = useState(0);
  const [chain, setChain] = useState<OptionHistoryPage<OptionHistoryChainRow>>({ rows: [], count: 0, offset: 0, limit: PAGE_SIZE });
  const [surface, setSurface] = useState<OptionHistorySurface | null>(null);
  const [curves, setCurves] = useState<OptionHistoryCurves | null>(null);
  const [anomalies, setAnomalies] = useState<OptionHistoryPage<OptionHistoryAnomaly>>({ rows: [], count: 0, offset: 0, limit: 250 });
  const [view, setView] = useState<"chain" | "surface" | "curves">("chain");
  const [error, setError] = useState<string | null>(null);
  const [webgl, setWebgl] = useState<boolean | null>(null);

  useEffect(() => {
    const canvas = document.createElement("canvas");
    setWebgl(Boolean(canvas.getContext("webgl") || canvas.getContext("experimental-webgl")));
  }, []);
  useEffect(() => { void loadOptionHistorySnapshots(symbol).then((result) => { setSnapshots(result.rows); setSnapshot((current) => current ?? result.rows[0]?.snapshot_id); }).catch(asError(setError)); }, [symbol]);
  useEffect(() => {
    if (!snapshot) return;
    const params = { symbol, snapshot, expiration: expiration || undefined, option_type: optionType || undefined, min_moneyness: minMoneyness || undefined, max_moneyness: maxMoneyness || undefined, offset: page * PAGE_SIZE, limit: PAGE_SIZE };
    void loadOptionHistoryChain(params).then(setChain).catch(asError(setError));
  }, [symbol, snapshot, expiration, optionType, minMoneyness, maxMoneyness, page]);
  useEffect(() => {
    if (!snapshot) return;
    const params = { symbol, snapshot, expiration: expiration || undefined };
    void Promise.all([loadOptionHistorySurface({ symbol, snapshot, option_type: optionType || undefined }), loadOptionHistoryCurves(params), loadOptionHistoryAnomalies({ symbol, snapshot, limit: 250 })])
      .then(([nextSurface, nextCurves, nextAnomalies]) => { setSurface(nextSurface); setCurves(nextCurves); setAnomalies(nextAnomalies); })
      .catch(asError(setError));
  }, [symbol, snapshot, expiration, optionType]);

  const expiries = useMemo(() => [...new Set((surface?.observed ?? []).flatMap((row) => typeof row.expiration === "string" ? [row.expiration] : []))].sort(), [surface]);
  const selected = snapshots.find((row) => row.snapshot_id === snapshot);
  const maxPage = Math.max(0, Math.ceil(chain.count / PAGE_SIZE) - 1);
  const filters = <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-5">
    <Select label="Snapshot" value={String(snapshot ?? "")} onChange={(value) => { setSnapshot(Number(value) || undefined); setPage(0); }}><option value="">No complete capture</option>{snapshots.map((row) => <option key={row.snapshot_id} value={row.snapshot_id}>{formatCaptureWindow(row)} · {(row.completeness ?? 0).toLocaleString(undefined, { style: "percent", maximumFractionDigits: 1 })}</option>)}</Select>
    <Select label="Expiry" value={expiration} onChange={(value) => { setExpiration(value); setPage(0); }}><option value="">All expiries</option>{expiries.map((value) => <option key={value}>{value}</option>)}</Select>
    <Select label="Type" value={optionType} onChange={(value) => { setOptionType(value as "call" | "put" | ""); setPage(0); }}><option value="">Calls and puts</option><option value="call">Calls</option><option value="put">Puts</option></Select>
    <Input label="Min log-moneyness" value={minMoneyness} onChange={setMinMoneyness} />
    <Input label="Max log-moneyness" value={maxMoneyness} onChange={setMaxMoneyness} />
  </div>;

  return <WorkspacePage eyebrow="Options History" title="QQQ Options Chain" subtitle="Full tradable Robinhood chain history. IV surfaces and anomalies are descriptive statistics, not trade recommendations." metrics={[
    ["As of", selected ? formatDate(selected.capture_finished_at ?? selected.slot_at ?? selected.observed_at) : "Collecting", selected ? `${formatCaptureWindow(selected)} capture · ${selected.contract_count.toLocaleString()} contracts` : "No complete snapshot yet", selected ? "good" : "warn"],
    ["Coverage", selected?.completeness !== null && selected?.completeness !== undefined ? selected.completeness.toLocaleString(undefined, { style: "percent", maximumFractionDigits: 1 }) : "—", "Complete snapshots only feed models and this workstation", "info"],
    ["History", `${snapshots.length.toLocaleString()} captures`, curves?.history_state === "ready" ? "History-dependent signals are active" : "History-dependent signals are collecting", curves?.history_state === "ready" ? "good" : "warn"],
    ["Anomalies", `${anomalies.count.toLocaleString()}`, "Statistical labels only", "info"],
  ]} actions={<StatusBadge tone={selected ? "good" : "warn"}>{selected ? "Complete capture" : "Collecting full-chain history"}</StatusBadge>}>
    {filters}
    {error ? <p className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{error}</p> : null}
    <div className="flex w-fit rounded-md border border-border bg-muted p-1"><Tab active={view === "chain"} onClick={() => setView("chain")}>Chain</Tab><Tab active={view === "surface"} onClick={() => setView("surface")}>IV Surface</Tab><Tab active={view === "curves"} onClick={() => setView("curves")}>Curves & History</Tab></div>
    {view === "chain" ? <ChainTable rows={chain.rows} page={page} maxPage={maxPage} count={chain.count} onPage={setPage} /> : null}
    {view === "surface" ? <SurfacePanel surface={surface} optionType={optionType || "call"} webgl={webgl} curves={curves} /> : null}
    {view === "curves" ? <CurvesPanel curves={curves} anomalies={anomalies.rows} /> : null}
  </WorkspacePage>;
}

function ChainTable({ rows, page, maxPage, count, onPage }: { rows: OptionHistoryChainRow[]; page: number; maxPage: number; count: number; onPage: (page: number) => void }) {
  const columns = useMemo<ColumnDef<OptionHistoryChainRow>[]>(() => [
    { header: "Expiry", accessorKey: "expiration" }, { header: "Type", accessorKey: "option_type" }, { header: "Strike", accessorKey: "strike", cell: ({ getValue }) => money(getValue<number>()) },
    { header: "DTE", accessorKey: "dte" }, { header: "Bid / Ask", cell: ({ row }) => `${money(row.original.bid)} / ${money(row.original.ask)}` }, { header: "IV", accessorKey: "provider_iv", cell: ({ getValue }) => percent(getValue<number | null>()) },
    { header: "Δ / Γ", cell: ({ row }) => `${number(row.original.provider_delta, 3)} / ${number(row.original.provider_gamma, 3)}` }, { header: "Θ / Vega / Rho", cell: ({ row }) => `${number(row.original.provider_theta, 3)} / ${number(row.original.provider_vega, 3)} / ${number(row.original.provider_rho, 3)}` },
    { header: "OI / Vol", cell: ({ row }) => `${integer(row.original.open_interest)} / ${integer(row.original.volume)}` }, { header: "Sizes", cell: ({ row }) => `${integer(row.original.bid_size)} / ${integer(row.original.ask_size)}` },
  ], []);
  const table = useReactTable({ data: rows, columns, getCoreRowModel: getCoreRowModel() });
  return <section className="rounded-lg border border-border bg-card"><div className="flex items-center justify-between gap-3 border-b border-border p-3 text-sm"><span>{count.toLocaleString()} contracts</span><div className="flex items-center gap-2"><button className="rounded border px-2 py-1 disabled:opacity-50" disabled={page === 0} onClick={() => onPage(page - 1)}>Previous</button><span>Page {page + 1} / {maxPage + 1}</span><button className="rounded border px-2 py-1 disabled:opacity-50" disabled={page >= maxPage} onClick={() => onPage(page + 1)}>Next</button></div></div><div className="overflow-x-auto"><table className="min-w-[1100px] w-full text-left text-xs"><thead className="bg-muted text-muted-foreground">{table.getHeaderGroups().map((group) => <tr key={group.id}>{group.headers.map((header) => <th className="whitespace-nowrap px-3 py-2 font-medium" key={header.id}>{header.isPlaceholder ? null : flexRender(header.column.columnDef.header, header.getContext())}</th>)}</tr>)}</thead><tbody>{table.getRowModel().rows.map((row) => <tr className="border-t border-border" key={row.id}>{row.getVisibleCells().map((cell) => <td className="whitespace-nowrap px-3 py-2 tabular-nums" key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>)}</tr>)}</tbody></table></div>{rows.length === 0 ? <p className="p-5 text-sm text-muted-foreground">No complete-chain contracts match these filters.</p> : null}</section>;
}

function SurfacePanel({ surface, optionType, webgl, curves }: { surface: OptionHistorySurface | null; optionType: "call" | "put"; webgl: boolean | null; curves: OptionHistoryCurves | null }) {
  if (!surface?.snapshot_id) return <Empty text="A complete snapshot is required before a surface can be shown." />;
  if (webgl === false) return <section className="rounded-lg border border-border p-4"><StatusBadge tone="warn">WebGL unavailable</StatusBadge><p className="mt-2 text-sm text-muted-foreground">The tabular volatility smiles below remain available without 3D acceleration.</p><SmileTable curves={curves} /></section>;
  return <section className="rounded-lg border border-border bg-card p-2"><Suspense fallback={<p className="p-4 text-sm text-muted-foreground">Loading WebGL surface…</p>}><OptionSurfacePlot surface={surface} optionType={optionType} /></Suspense></section>;
}

function CurvesPanel({ curves, anomalies }: { curves: OptionHistoryCurves | null; anomalies: OptionHistoryAnomaly[] }) {
  return <div className="space-y-4"><section className="rounded-lg border border-border p-4"><div className="mb-3 flex items-center justify-between"><h2 className="font-medium">Curves and historical comparisons</h2><StatusBadge tone={curves?.history_state === "ready" ? "good" : "warn"}>{curves?.history_state ?? "collecting"}</StatusBadge></div>{curves ? <Suspense fallback={<p className="text-sm text-muted-foreground">Loading curves…</p>}><OptionCurvePlots curves={curves} /></Suspense> : <Empty text="A complete snapshot is required before curves can be shown." />}</section><div className="grid gap-4 xl:grid-cols-2"><section className="rounded-lg border border-border p-4"><h2 className="mb-3 font-medium">Observed smile points</h2><SmileTable curves={curves} /></section><section className="rounded-lg border border-border p-4"><h2 className="mb-3 font-medium">Statistical anomalies</h2><div className="max-h-[460px] overflow-auto"><table className="w-full text-left text-xs"><thead className="text-muted-foreground"><tr><th>Signal</th><th>State</th><th>Expiry</th><th>Z-score</th></tr></thead><tbody>{anomalies.map((row) => <tr className="border-t" key={row.id}><td className="py-2">{row.anomaly_type}</td><td><StatusBadge tone={row.state === "active" ? "warn" : "muted"}>{row.state}</StatusBadge></td><td>{row.expiration ?? "—"}</td><td>{number(row.z_score, 2)}</td></tr>)}</tbody></table>{anomalies.length === 0 ? <p className="py-4 text-sm text-muted-foreground">No threshold anomalies in this snapshot.</p> : null}</div></section></div></div>;
}

function SmileTable({ curves }: { curves: OptionHistoryCurves | null }) { const smiles = curves?.smiles ?? []; return <div className="max-h-[420px] overflow-auto"><table className="w-full text-left text-xs"><thead className="text-muted-foreground"><tr><th>Expiry</th><th>Type</th><th>DTE</th><th>Points</th></tr></thead><tbody>{smiles.map((smile, index) => <tr className="border-t" key={`${String(smile.expiration)}-${String(smile.option_type)}-${index}`}><td className="py-2">{String(smile.expiration ?? "—")}</td><td>{String(smile.option_type ?? "—")}</td><td>{String(smile.dte ?? "—")}</td><td>{Array.isArray(smile.points) ? smile.points.length : 0}</td></tr>)}</tbody></table>{smiles.length === 0 ? <p className="py-4 text-sm text-muted-foreground">No IV points available.</p> : null}</div>; }
function Tab({ active, onClick, children }: { active: boolean; onClick: () => void; children: string }) { return <button type="button" onClick={onClick} className={`rounded px-3 py-1.5 text-sm ${active ? "bg-background shadow-sm" : "text-muted-foreground"}`}>{children}</button>; }
function Select({ label, value, onChange, children }: { label: string; value: string; onChange: (value: string) => void; children: ReactNode }) { return <label className="grid gap-1 text-xs text-muted-foreground">{label}<select value={value} onChange={(event) => onChange(event.target.value)} className="h-9 rounded-md border border-input bg-background px-2 text-sm text-foreground">{children}</select></label>; }
function Input({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) { return <label className="grid gap-1 text-xs text-muted-foreground">{label}<input value={value} inputMode="decimal" onChange={(event) => onChange(event.target.value)} className="h-9 rounded-md border border-input bg-background px-2 text-sm text-foreground" /></label>; }
function Empty({ text }: { text: string }) { return <p className="rounded-lg border border-dashed p-6 text-sm text-muted-foreground">{text}</p>; }
function asError(setter: (value: string | null) => void) { return (error: unknown) => setter(error instanceof Error ? error.message : "Unable to load option history"); }
function formatDate(value: string) { return new Date(value).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }); }
function formatCaptureWindow(snapshot: OptionHistorySnapshot) { const start = snapshot.capture_started_at ?? snapshot.slot_at ?? snapshot.observed_at; const finish = snapshot.capture_finished_at; return finish ? `${formatDate(start)}–${new Date(finish).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : formatDate(start); }
function money(value: number | null | undefined) { return value === null || value === undefined ? "—" : value.toLocaleString(undefined, { style: "currency", currency: "USD" }); }
function percent(value: number | null | undefined) { return value === null || value === undefined ? "—" : value.toLocaleString(undefined, { style: "percent", maximumFractionDigits: 2 }); }
function number(value: number | null | undefined, digits = 2) { return value === null || value === undefined ? "—" : value.toFixed(digits); }
function integer(value: number | null | undefined) { return value === null || value === undefined ? "—" : value.toLocaleString(); }
