// Thesis pipeline, requests, agent browser, and detail pane.

import {useEffect, useMemo, useState } from "react";
import {BrainCircuit, Search} from "lucide-react";
import {DataTableFrame, EmptyState, StatusBadge } from "@/components/market/workstation";
import {Input } from "@/components/ui/input";
import {Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {cn } from "@/lib/utils";
import {RowRecord } from "@/types";
import {Tone } from "@/ui/tone";
import {displayField, fullField, listField, numberField, textField, titleLabel, toneFromText } from "../rowFormat";
import {moneyField, formatScore, formatDate, formatShortDate, dateMillis } from "../optionsRadarFormat";
import {jsonRecord, jsonArrayField, stringFromRecord, latestValidationBy, validationHistoryBy } from "../optionsRadarData";
import {stateTone, thesisStateTone, thesisValidationLabel, validationStatusLabel, validationStatusTone, toneText } from "../optionsRadarTone";
import {Cell, Head, SectionTitle, TickerButton, Truncated } from "../optionsRadarPrimitives";
import {OpenTicker } from "../workspacePage";
import {thesisId, validationForThesis, validationHistoryForThesis, countWhere, contractLabel, stateOf, oldestDate } from "./helpers";
import {OptionThesisAgentRuntime } from "./types";
import {BrowserStat, MetricBox, ReadableSection, ReadableList } from "./shared";

export function ThesisPipelinePanel({
  requests,
  theses,
  validations,
  agentRuntime,
}: {
  requests: RowRecord[];
  theses: RowRecord[];
  validations: RowRecord[];
  agentRuntime: OptionThesisAgentRuntime;
}) {
  const open = countWhere(requests, (row) => textField(row, ["status"], "open").toLowerCase() === "open");
  const failed = countWhere(requests, (row) => textField(row, ["status"]).toLowerCase().includes("failed"));
  const validated = countWhere(validations, (row) => textField(row, ["state"]).toLowerCase() === "validated");
  const tracking = countWhere(validations, (row) => textField(row, ["state"]).toLowerCase() === "tracking");
  const validationPending = countWhere(validations, (row) => textField(row, ["state"]).toLowerCase() === "pending");
  const oldestOpen = oldestDate(requests.filter((row) => textField(row, ["status"], "open").toLowerCase() === "open"), "created_at");
  const queueAtCap = open >= agentRuntime.requestCap;
  const items: Array<[string, string, string, Tone]> = [
    ["Premarket queue", open.toLocaleString(), queueAtCap ? `at current queue cap of ${agentRuntime.requestCap}` : "top-ranked signals awaiting synthesis", open ? "warn" : "muted"],
    ["Agent cadence", agentRuntime.active ? "Daily" : "Paused", agentRuntime.active ? `premarket batch limit ${agentRuntime.limit}` : "option thesis command is not enabled", agentRuntime.active ? "good" : open ? "warn" : "muted"],
    ["Oldest open", oldestOpen ? formatDate(oldestOpen) : "-", "age of the oldest unfulfilled request", oldestOpen ? "warn" : "muted"],
    ["Completed hypotheses", theses.length.toLocaleString(), "structured hypotheses returned by agents", theses.length ? "good" : "muted"],
    ["Tracking", tracking.toLocaleString(), "active hypotheses being checked against price, proof, and invalidation gates", tracking ? "info" : "muted"],
    ["Needs proof", validationPending.toLocaleString(), "hypotheses blocked by missing deterministic proof support", validationPending ? "warn" : "muted"],
    ["Validated", validated.toLocaleString(), "theses passing deterministic checks", validated ? "good" : validations.length ? "warn" : "muted"],
    ["Failed", failed.toLocaleString(), "agent calls needing attention", failed ? "bad" : "good"],
  ];
  return (
    <section className="rounded-md border border-border bg-card p-4">
      <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
        <div>
          <h2 className="text-base font-semibold">Thesis Checks</h2>
          <p className="mt-1 max-w-4xl text-sm leading-6 text-muted-foreground">
            Agent synthesis runs once before the market review window. Deterministic validation decides whether each thesis actually supports the option signal.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <StatusBadge tone={agentRuntime.active ? "good" : open ? "warn" : "muted"}>{agentRuntime.active ? "Worker active" : "Worker paused"}</StatusBadge>
          {queueAtCap ? <StatusBadge tone="warn">At request cap</StatusBadge> : null}
          <StatusBadge tone={theses.length ? "good" : open ? "warn" : "muted"}>{theses.length ? "Hypotheses available" : open ? "Requests open" : "No open requests"}</StatusBadge>
        </div>
      </div>
      <div className="mt-4 grid gap-2 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-5">
        {items.map(([label, value, detail, tone]) => (
          <div key={label} className="rounded-md border border-border/70 bg-background px-3 py-2">
            <div className="text-[10px] font-semibold uppercase text-muted-foreground">{label}</div>
            <div className={cn("mt-1 text-sm font-semibold tabular-nums", toneText(tone))}>{value}</div>
            <div className="mt-1 text-xs text-muted-foreground">{detail}</div>
          </div>
        ))}
      </div>
    </section>
  );
}

export function ThesisRequestsTable({
  rows,
  eventById,
  onOpenTicker,
  agentRuntime,
}: {
  rows: RowRecord[];
  eventById: Map<string, RowRecord>;
  onOpenTicker: OpenTicker;
  agentRuntime: OptionThesisAgentRuntime;
}) {
  if (!rows.length) {
    return <EmptyState title="No active thesis requests" detail="No current top-ranked candidates are waiting for agent thesis generation." icon={BrainCircuit} />;
  }

  return (
    <DataTableFrame
      title={<SectionTitle title="Premarket Thesis Queue" count={rows.length} />}
      action={<StatusBadge tone={agentRuntime.active ? "good" : "warn"}>{agentRuntime.active ? "Worker active" : "Worker paused"}</StatusBadge>}
    >
      <div className="border-b border-border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">
        Current top-ranked signals waiting for daily premarket synthesis. Completed hypotheses appear below only after deterministic validation links them back to a current candidate.
      </div>
      <table className="w-full min-w-[960px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <Head>Ticker</Head>
            <Head>Status</Head>
            <Head className="text-right">Priority</Head>
            <Head>Candidate State</Head>
            <Head>Created</Head>
            <Head>Candidate</Head>
            <Head>Next Step</Head>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 80).map((row) => {
            const ticker = textField(row, ["ticker"]);
            const event = eventById.get(textField(row, ["event_id"]));
            const status = textField(row, ["status"], "open");
            const contract = event ? contractLabel(event) : textField(row, ["event_id"]);
            return (
              <tr key={textField(row, ["request_id"], `${ticker}-${textField(row, ["created_at"])}`)} className="border-b border-border align-top transition-colors hover:bg-accent/40">
                <Cell>{ticker ? <TickerButton ticker={ticker} onOpenTicker={onOpenTicker} /> : "-"}</Cell>
                <Cell><StatusBadge tone={toneFromText(status)}>{titleLabel(status)}</StatusBadge></Cell>
                <Cell className="text-right tabular-nums">{formatScore(numberField(row, ["priority_score"], Number.NaN))}</Cell>
                <Cell><StatusBadge tone={stateTone(stateOf(event))}>{titleLabel(stateOf(event) || "pending")}</StatusBadge></Cell>
                <Cell className="whitespace-nowrap text-muted-foreground">{formatDate(textField(row, ["created_at"]))}</Cell>
                <Cell className="max-w-[260px]"><Truncated>{contract}</Truncated></Cell>
                <Cell className="max-w-[320px]"><Truncated>{status.toLowerCase() === "open" ? "Wait for daily premarket thesis worker" : titleLabel(status)}</Truncated></Cell>
              </tr>
            );
          })}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

export function AgentThesisBrowser({ theses, validations, onOpenTicker }: { theses: RowRecord[]; validations: RowRecord[]; onOpenTicker: OpenTicker }) {
  const [selectedId, setSelectedId] = useState("");
  const [query, setQuery] = useState("");
  const [stateFilter, setStateFilter] = useState("all");
  const validationHistoryByThesis = useMemo(() => validationHistoryBy(validations, "thesis_id"), [validations]);
  const latestValidationByThesis = useMemo(() => latestValidationBy(validations, "thesis_id"), [validations]);
  const legacyValidationByTicker = useMemo(() => latestValidationBy(validations.filter((row) => !textField(row, ["thesis_id"])), "ticker"), [validations]);
  const thesisRows = useMemo(
    () => [...theses].sort((left, right) => dateMillis(textField(right, ["created_at"])) - dateMillis(textField(left, ["created_at"]))),
    [theses],
  );

  useEffect(() => {
    if (!thesisRows.length) {
      if (selectedId) setSelectedId("");
      return;
    }
    if (!selectedId || !thesisRows.some((row) => thesisId(row) === selectedId)) {
      setSelectedId(thesisId(thesisRows[0]));
    }
  }, [selectedId, thesisRows]);

  const filtered = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return thesisRows.filter((row) => {
      const history = validationHistoryForThesis(row, validationHistoryByThesis, legacyValidationByTicker);
      const validation = history[0];
      const validationState = textField(validation, ["state"], "pending").toLowerCase();
      const redTeam = textField(validation, ["red_team_status"]).toLowerCase();
      if (stateFilter === "tracking" && validationState !== "tracking") return false;
      if (stateFilter === "validated" && validationState !== "validated") return false;
      if (stateFilter === "pending" && validationState !== "pending") return false;
      if (stateFilter === "invalidated" && !validationState.includes("invalidated")) return false;
      if (stateFilter === "fundamental-risk" && !redTeam.includes("hard_risk")) return false;
      if (!normalizedQuery) return true;
      return [
        textField(row, ["ticker"]),
        fullField(row, ["core_thesis"], ""),
        fullField(row, ["bear_case"], ""),
        fullField(row, ["catalyst_summary", "catalysts"], ""),
        fullField(row, ["invalidation_conditions"], ""),
        fullField(validation, ["reason"], ""),
      ].join(" ").toLowerCase().includes(normalizedQuery);
    });
  }, [legacyValidationByTicker, query, stateFilter, thesisRows, validationHistoryByThesis]);

  const selected = filtered.find((row) => thesisId(row) === selectedId) ?? filtered[0];
  const selectedValidation = selected ? validationForThesis(selected, latestValidationByThesis, legacyValidationByTicker) : undefined;
  const selectedValidationHistory = selected ? validationHistoryForThesis(selected, validationHistoryByThesis, legacyValidationByTicker) : [];
  const validated = countWhere(thesisRows, (row) => textField(validationHistoryForThesis(row, validationHistoryByThesis, legacyValidationByTicker)[0], ["state"]).toLowerCase() === "validated");
  const tracking = countWhere(thesisRows, (row) => textField(validationHistoryForThesis(row, validationHistoryByThesis, legacyValidationByTicker)[0], ["state"]).toLowerCase() === "tracking");
  const validationPending = countWhere(thesisRows, (row) => textField(validationHistoryForThesis(row, validationHistoryByThesis, legacyValidationByTicker)[0], ["state"]).toLowerCase() === "pending");
  const invalidated = countWhere(thesisRows, (row) => textField(validationHistoryForThesis(row, validationHistoryByThesis, legacyValidationByTicker)[0], ["state"]).toLowerCase().includes("invalidated"));
  const hardRisk = countWhere(thesisRows, (row) => textField(validationHistoryForThesis(row, validationHistoryByThesis, legacyValidationByTicker)[0], ["red_team_status"]).toLowerCase().includes("hard_risk"));

  if (!thesisRows.length) {
    return <EmptyState title="No completed theses" detail="No structured agent hypotheses are stored yet." icon={BrainCircuit} />;
  }

  return (
    <section className="overflow-hidden rounded-md border border-border bg-card">
      <div className="border-b border-border p-4">
        <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-base font-semibold">Completed Hypotheses</h2>
              <StatusBadge tone={thesisRows.length ? "good" : "muted"}>{thesisRows.length.toLocaleString()} completed</StatusBadge>
            </div>
            <p className="mt-1 max-w-4xl text-sm leading-6 text-muted-foreground">
              Browse returned hypotheses with deterministic validation, proof requirements, catalysts, invalidation, and bear case in the same view.
            </p>
          </div>
          <div className="grid grid-cols-2 gap-2 text-right sm:min-w-[540px] sm:grid-cols-5">
            <BrowserStat label="Validated" value={validated} tone={validated ? "good" : "muted"} />
            <BrowserStat label="Tracking" value={tracking} tone={tracking ? "info" : "muted"} />
            <BrowserStat label="Needs Proof" value={validationPending} tone={validationPending ? "warn" : "muted"} />
            <BrowserStat label="Invalidated" value={invalidated} tone={invalidated ? "bad" : "muted"} />
            <BrowserStat label="Hard Risk" value={hardRisk} tone={hardRisk ? "warn" : "muted"} />
          </div>
        </div>
        <div className="mt-4 grid gap-2 md:grid-cols-[minmax(0,1fr)_220px]">
          <div className="relative">
            <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search theses, catalysts, bear cases..." className="pl-9" />
          </div>
          <Select value={stateFilter} onValueChange={setStateFilter}>
            <SelectTrigger>
              <SelectValue placeholder="Validation state" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All states</SelectItem>
              <SelectItem value="tracking">Tracking</SelectItem>
              <SelectItem value="pending">Needs proof</SelectItem>
              <SelectItem value="validated">Validated</SelectItem>
              <SelectItem value="invalidated">Invalidated</SelectItem>
              <SelectItem value="fundamental-risk">Fundamental risk</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>

      <div className="grid min-h-[640px] xl:grid-cols-[380px_minmax(0,1fr)]">
        <div className="border-b border-border xl:border-b-0 xl:border-r">
          <div className="flex items-center justify-between border-b border-border px-4 py-2 text-xs text-muted-foreground">
            <span>{filtered.length.toLocaleString()} shown</span>
            <span>{stateFilter === "all" ? "Latest first" : stateFilter === "pending" ? "Needs proof" : titleLabel(stateFilter)}</span>
          </div>
          <div className="max-h-[640px] overflow-y-auto">
            {filtered.length ? (
              filtered.map((row) => {
                const id = thesisId(row);
                const history = validationHistoryForThesis(row, validationHistoryByThesis, legacyValidationByTicker);
                const validation = history[0];
                return (
                  <div key={id}>
                    <button
                      type="button"
                      className={cn(
                        "block w-full border-b border-border px-4 py-3 text-left transition-colors hover:bg-accent/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                        selected && id === thesisId(selected) ? "bg-accent/70" : "bg-transparent",
                      )}
                      onClick={() => setSelectedId(id)}
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="font-semibold">{displayField(row, ["ticker"], "Unknown")}</span>
                            <StatusBadge tone={thesisStateTone(textField(validation, ["state"]))}>{thesisValidationLabel(validation)}</StatusBadge>
                            {history.length > 1 ? <span className="text-xs text-muted-foreground">{history.length} checks</span> : null}
                          </div>
                          <p className="mt-2 line-clamp-3 text-sm leading-5 text-muted-foreground">{displayField(row, ["core_thesis"], "No core thesis")}</p>
                        </div>
                        <div className="shrink-0 text-right text-xs tabular-nums">
                          <div className="font-semibold">{formatScore(numberField(row, ["confidence"], Number.NaN))}</div>
                          <div className="text-muted-foreground">conf</div>
                        </div>
                      </div>
                      <div className="mt-3 grid grid-cols-3 gap-2 text-xs text-muted-foreground">
                        <span>{moneyField(row, ["bull_target_price"])} bull</span>
                        <span>{moneyField(row, ["base_target_price"])} base</span>
                        <span>{formatShortDate(textField(row, ["bull_target_date"]))}</span>
                      </div>
                    </button>
                    {selected && id === thesisId(selected) ? (
                      <div className="border-b border-border bg-background xl:hidden">
                        <ThesisDetailPane thesis={selected} validation={selectedValidation} validationHistory={selectedValidationHistory} onOpenTicker={onOpenTicker} />
                      </div>
                    ) : null}
                  </div>
                );
              })
            ) : (
              <div className="px-4 py-10 text-sm text-muted-foreground">No theses match the current filter.</div>
            )}
          </div>
        </div>

        {selected ? (
          <div className="hidden xl:block">
          <ThesisDetailPane thesis={selected} validation={selectedValidation} validationHistory={selectedValidationHistory} onOpenTicker={onOpenTicker} />
          </div>
        ) : (
          <div className="p-8 text-sm text-muted-foreground">No thesis matches the current filter. Clear search or choose another validation state.</div>
        )}
      </div>
    </section>
  );
}

export function ThesisDetailPane({ thesis, validation, validationHistory, onOpenTicker }: { thesis: RowRecord; validation: RowRecord | undefined; validationHistory: RowRecord[]; onOpenTicker: OpenTicker }) {
  const ticker = textField(thesis, ["ticker"]);
  const state = textField(validation, ["state"]);
  const redTeam = textField(validation, ["red_team_status"], "not_checked");
  return (
    <div className="min-w-0">
      <div className="border-b border-border p-5">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              {ticker ? <TickerButton ticker={ticker} onOpenTicker={onOpenTicker} /> : <span className="text-lg font-semibold">Unknown ticker</span>}
              <StatusBadge tone={thesisStateTone(state)}>{thesisValidationLabel(validation)}</StatusBadge>
              <StatusBadge tone={validationStatusTone(redTeam)}>{validationStatusLabel(redTeam)}</StatusBadge>
            </div>
            <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
              <span>Created {formatDate(textField(thesis, ["created_at"]))}</span>
              <span>Validation {formatShortDate(textField(validation, ["validation_date"]))}</span>
              <span>{displayField(thesis, ["agent_version"], "Agent version unknown")}</span>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:min-w-[520px]">
            <MetricBox label="Bull Target" value={moneyField(thesis, ["bull_target_price"])} />
            <MetricBox label="Base Target" value={moneyField(thesis, ["base_target_price"])} />
            <MetricBox label="Bull Date" value={formatShortDate(textField(thesis, ["bull_target_date"]))} />
            <MetricBox label="Confidence" value={formatScore(numberField(thesis, ["confidence"], Number.NaN))} />
          </div>
        </div>
      </div>

      <div className="space-y-6 p-5">
        <ReadableSection title="Core Thesis">
          <p className="whitespace-pre-wrap text-sm leading-7 text-foreground">{displayField(thesis, ["core_thesis"], "No core thesis text.")}</p>
        </ReadableSection>

        <div className="grid gap-5 xl:grid-cols-2">
          <ReadableSection title="Required Proofs">
            <ReadableList items={listField(thesis, ["required_proofs"])} empty="No proof points stored." />
          </ReadableSection>
          <ReadableSection title="Catalysts">
            <CatalystList thesis={thesis} />
          </ReadableSection>
          <ReadableSection title="Invalidation">
            <ReadableList items={listField(thesis, ["invalidation_conditions", "invalidation"])} empty="No invalidation conditions stored." />
          </ReadableSection>
          <ReadableSection title="Bear Case">
            <p className="whitespace-pre-wrap text-sm leading-7 text-foreground">{displayField(thesis, ["bear_case"], "No bear case stored.")}</p>
          </ReadableSection>
        </div>

        <ReadableSection title="Validation">
          <div className="flex flex-wrap gap-2">
            <StatusBadge tone={validationStatusTone(textField(validation, ["proof_status"]))}>Proofs {titleLabel(displayField(validation, ["proof_status"], "unknown"))}</StatusBadge>
            <StatusBadge tone={validationStatusTone(textField(validation, ["catalyst_status"]))}>Catalysts {titleLabel(displayField(validation, ["catalyst_status"], "unknown"))}</StatusBadge>
            <StatusBadge tone={validationStatusTone(textField(validation, ["invalidation_status"]))}>Invalidation {titleLabel(displayField(validation, ["invalidation_status"], "unknown"))}</StatusBadge>
            <StatusBadge tone={validationStatusTone(textField(validation, ["evidence_status"]))}>Evidence {titleLabel(displayField(validation, ["evidence_status"], "unknown"))}</StatusBadge>
          </div>
          <p className="mt-3 whitespace-pre-wrap text-sm leading-7 text-foreground">{displayField(validation, ["reason"], "No validation reason stored.")}</p>
        </ReadableSection>

        <ReadableSection title="Validation History">
          {validationHistory.length ? (
            <div className="divide-y divide-border rounded-md border border-border">
              {validationHistory.map((row) => (
                <div key={textField(row, ["validation_id"], `${textField(row, ["strategy_version"])}-${textField(row, ["validation_date"])}`)} className="grid gap-3 px-3 py-3 text-sm lg:grid-cols-[150px_190px_minmax(0,1fr)]">
                  <div className="text-xs text-muted-foreground">
                    <div>{formatShortDate(textField(row, ["validation_date"]))}</div>
                    <div>{formatDate(textField(row, ["validated_at"]))}</div>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <StatusBadge tone={thesisStateTone(textField(row, ["state"]))}>{thesisValidationLabel(row)}</StatusBadge>
                    <StatusBadge tone={validationStatusTone(textField(row, ["red_team_status"]))}>{validationStatusLabel(displayField(row, ["red_team_status"], "unknown"))}</StatusBadge>
                  </div>
                  <div className="min-w-0">
                    <div className="truncate text-xs text-muted-foreground">{displayField(row, ["strategy_version"], "Strategy unknown")}</div>
                    <p className="mt-1 whitespace-pre-wrap leading-6">{displayField(row, ["reason"], "No validation reason stored.")}</p>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">No validation checks are stored for this thesis.</p>
          )}
        </ReadableSection>

        <ReadableSection title="Evidence References">
          <ReadableList items={listField(thesis, ["evidence_refs"])} empty="No evidence references stored." />
        </ReadableSection>
      </div>
    </div>
  );
}

export function CatalystList({ thesis }: { thesis: RowRecord }) {
  const catalysts = jsonArrayField(thesis, "catalysts");
  if (!catalysts.length) return <p className="text-sm text-muted-foreground">No catalysts stored.</p>;
  return (
    <div className="space-y-3">
      {catalysts.map((item, index) => {
        const record = jsonRecord(item);
        if (!record) {
          return <p key={index} className="text-sm leading-6">{String(item)}</p>;
        }
        return (
          <div key={`${index}-${stringFromRecord(record, "type", "catalyst")}`} className="border-l-2 border-border pl-3">
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge tone="info">{titleLabel(stringFromRecord(record, "type", "Catalyst"))}</StatusBadge>
              <span className="text-xs text-muted-foreground">{stringFromRecord(record, "expected_window", "Window unknown")}</span>
            </div>
            <p className="mt-2 text-sm leading-6">{stringFromRecord(record, "what_to_watch", fullField({ value: item } as RowRecord, ["value"]))}</p>
          </div>
        );
      })}
    </div>
  );
}

