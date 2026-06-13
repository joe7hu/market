// Candidate events table, mobile card, and signal/thesis sub-views.

import {Fragment, useEffect, useMemo, useState } from "react";
import {ArrowDownUp, ChevronDown, ChevronLeft, ChevronRight, Search, Target } from "lucide-react";
import {DataTableFrame, EmptyState, StatusBadge } from "@/components/market/workstation";
import {Button } from "@/components/ui/button";
import {Input } from "@/components/ui/input";
import {Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {cn } from "@/lib/utils";
import {RowRecord } from "@/types";
import {displayField, listField, numberField, textField, titleLabel, toneFromText } from "../rowFormat";
import {moneyField, formatRatio, formatScore, formatShortDate } from "../optionsRadarFormat";
import {recordField, listFromRecord, stringFromRecord } from "../optionsRadarData";
import {stateTone, thesisStateTone, thesisValidationLabel } from "../optionsRadarTone";
import {Cell, FullText, Head, MetricPill, SectionTitle, TickerButton } from "../optionsRadarPrimitives";
import {OpenTicker } from "../workspacePage";
import {candidateActionText, uniqueText, uniqueValues, candidateConviction, candidateFamily, stateOf, qualityOf, thesisState, focusCandidateRows, compareCandidates, readableReasonSummary } from "./helpers";
import {OptionThesisAgentRuntime, CandidateSort, CandidateStateFilter, CandidateFocus, ThesisFilter, QualityFilter, FamilyFilter, CANDIDATE_PAGE_SIZE } from "./types";
import {ReadableReasonGroup, InlineMetric, MobileSection, QualityIndicator, HelpLabel, FillTarget, PremiumCapHint, OpportunityOutcome } from "./shared";
import {OpportunityThesisSummary } from "./signalBrief";

export function CandidateEventsTable({
  rows,
  thesisRequestByEvent,
  latestMarkByEvent,
  latestAttributionByEvent,
  latestThesisValidationByEvent,
  latestAgentThesisByTicker,
  agentRuntime,
  onOpenTicker,
}: {
  rows: RowRecord[];
  thesisRequestByEvent: Map<string, RowRecord>;
  latestMarkByEvent: Map<string, RowRecord>;
  latestAttributionByEvent: Map<string, RowRecord>;
  latestThesisValidationByEvent: Map<string, RowRecord>;
  latestAgentThesisByTicker: Map<string, RowRecord>;
  agentRuntime: OptionThesisAgentRuntime;
  onOpenTicker: OpenTicker;
}) {
  const [query, setQuery] = useState("");
  const [stateFilter, setStateFilter] = useState<CandidateStateFilter>("all");
  const [thesisFilter, setThesisFilter] = useState<ThesisFilter>("all");
  const [qualityFilter, setQualityFilter] = useState<QualityFilter>("all");
  const [familyFilter, setFamilyFilter] = useState<FamilyFilter>("all");
  const [focus, setFocus] = useState<CandidateFocus>("top-per-ticker");
  const [sort, setSort] = useState<CandidateSort>("state");
  const [page, setPage] = useState(0);
  const [expandedThesisEvent, setExpandedThesisEvent] = useState<string | null>(null);

  const filteredRows = useMemo(() => {
    const normalizedQuery = query.trim().toUpperCase();
    return rows
      .filter((row) => {
        if (stateFilter !== "all" && stateOf(row) !== stateFilter) return false;
        if (qualityFilter !== "all" && qualityOf(row) !== qualityFilter) return false;
        if (familyFilter !== "all" && candidateFamily(row) !== familyFilter) return false;
        if (thesisFilter !== "all" && thesisState(row, thesisRequestByEvent).kind !== thesisFilter) return false;
        if (!normalizedQuery) return true;
        const haystack = [
          textField(row, ["ticker"]),
          textField(row, ["contract_id"]),
          textField(row, ["strategy_version"]),
          readableReasonSummary(row),
        ].join(" ").toUpperCase();
        return haystack.includes(normalizedQuery);
      });
  }, [familyFilter, query, qualityFilter, rows, stateFilter, thesisFilter, thesisRequestByEvent]);

  const focusedRows = useMemo(
    () => focusCandidateRows(filteredRows, focus).sort((left, right) => compareCandidates(left, right, sort)),
    [filteredRows, focus, sort],
  );

  useEffect(() => {
    setPage(0);
    setExpandedThesisEvent(null);
  }, [focus, query, qualityFilter, sort, stateFilter, thesisFilter]);

  const pageCount = Math.max(1, Math.ceil(focusedRows.length / CANDIDATE_PAGE_SIZE));
  const boundedPage = Math.min(page, pageCount - 1);
  const visibleRows = focusedRows.slice(boundedPage * CANDIDATE_PAGE_SIZE, (boundedPage + 1) * CANDIDATE_PAGE_SIZE);
  const tickerCount = uniqueText(focusedRows, "ticker").length;
  const familyOptions = useMemo(() => uniqueValues(rows.map(candidateFamily)).filter(Boolean), [rows]);

  if (!rows.length) {
    return <EmptyState title="No candidate events" detail="No options radar candidates are stored yet." icon={Target} />;
  }

  return (
    <DataTableFrame
      title={<SectionTitle title="Top Ranked Signals" count={focusedRows.length} />}
      action={
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <span>{tickerCount.toLocaleString()} tickers</span>
          <span>{filteredRows.length.toLocaleString()} matched</span>
        </div>
      }
    >
      <div className="border-b border-border p-3">
        <div className="grid gap-2 lg:grid-cols-[minmax(220px,1fr)_155px_150px_150px_160px_160px_190px_auto]">
          <div className="relative min-w-0">
            <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input className="pl-9" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Ticker, contract, or thesis blocker" aria-label="Filter signal evidence" />
          </div>
          <Select value={focus} onValueChange={(value) => setFocus(value as CandidateFocus)}>
            <SelectTrigger aria-label="Candidate focus"><SelectValue placeholder="Focus" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="top-per-ticker">Top per ticker</SelectItem>
              <SelectItem value="top25">Top 25 contracts</SelectItem>
              <SelectItem value="all">All ranked contracts</SelectItem>
            </SelectContent>
          </Select>
          <Select value={stateFilter} onValueChange={(value) => setStateFilter(value as CandidateStateFilter)}>
            <SelectTrigger aria-label="State filter"><SelectValue placeholder="State" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All states</SelectItem>
              <SelectItem value="FIRE">Fire</SelectItem>
              <SelectItem value="SETUP">Setup</SelectItem>
              <SelectItem value="WATCH">Watch</SelectItem>
            </SelectContent>
          </Select>
          <Select value={thesisFilter} onValueChange={(value) => setThesisFilter(value as ThesisFilter)}>
            <SelectTrigger aria-label="Thesis filter"><SelectValue placeholder="Thesis" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Any thesis</SelectItem>
              <SelectItem value="needs">Needs evidence</SelectItem>
              <SelectItem value="requested">Thesis requested</SelectItem>
              <SelectItem value="attached">Thesis attached</SelectItem>
            </SelectContent>
          </Select>
          <Select value={qualityFilter} onValueChange={(value) => setQualityFilter(value as QualityFilter)}>
            <SelectTrigger aria-label="Quality filter"><SelectValue placeholder="Quality" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Any quality</SelectItem>
              <SelectItem value="ok">Provider OK</SelectItem>
              <SelectItem value="caution">Caution</SelectItem>
              <SelectItem value="bad">Bad data</SelectItem>
            </SelectContent>
          </Select>
          <Select value={familyFilter} onValueChange={(value) => setFamilyFilter(value)}>
            <SelectTrigger aria-label="Family filter"><SelectValue placeholder="Family" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All families</SelectItem>
              {familyOptions.map((family) => <SelectItem key={family} value={family}>{family}</SelectItem>)}
            </SelectContent>
          </Select>
          <Select value={sort} onValueChange={(value) => setSort(value as CandidateSort)}>
            <SelectTrigger aria-label="Sort candidates"><SelectValue placeholder="Sort" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="conviction-desc">Conviction high to low</SelectItem>
              <SelectItem value="move-asc">Required move low to high</SelectItem>
              <SelectItem value="premium-asc">Option mid low to high</SelectItem>
              <SelectItem value="expiry-asc">Expiration soonest</SelectItem>
              <SelectItem value="state">Queue rank</SelectItem>
              <SelectItem value="ticker-asc">Ticker A to Z</SelectItem>
            </SelectContent>
          </Select>
          <Button type="button" variant="outline" size="sm" className="h-9" onClick={() => {
            setQuery("");
            setStateFilter("all");
            setThesisFilter("all");
            setQualityFilter("all");
            setFamilyFilter("all");
            setFocus("top-per-ticker");
            setSort("state");
          }}>
            <ArrowDownUp className="size-4" />
            <span>Reset</span>
          </Button>
        </div>
        <p className="mt-2 text-xs leading-5 text-muted-foreground">
          One primary surface for current ranked contracts. Each row combines the contract signal, evidence, thesis validation, observed outcome, and next action.
        </p>
      </div>
      <div className="space-y-3 p-3 lg:hidden">
        {visibleRows.map((row) => {
          const ticker = textField(row, ["ticker"]);
          const eventId = textField(row, ["event_id"]);
          const thesisExpanded = expandedThesisEvent === eventId;
          return (
            <CandidateMobileCard
              key={textField(row, ["event_id"], `${ticker}-${textField(row, ["contract_id"])}`)}
              row={row}
              request={thesisRequestByEvent.get(eventId)}
              validation={latestThesisValidationByEvent.get(eventId)}
              thesis={latestAgentThesisByTicker.get(ticker)}
              mark={latestMarkByEvent.get(eventId)}
              attribution={latestAttributionByEvent.get(eventId)}
              agentRuntime={agentRuntime}
              expanded={thesisExpanded}
              onToggleThesis={() => setExpandedThesisEvent(thesisExpanded ? null : eventId)}
              onOpenTicker={onOpenTicker}
            />
          );
        })}
      </div>
      <table className="hidden w-full min-w-[1780px] table-fixed text-sm lg:table">
        <colgroup>
          <col className="w-[8rem]" />
          <col className="w-[9rem]" />
          <col className="w-[16rem]" />
          <col className="w-[10rem]" />
          <col className="w-[11rem]" />
          <col className="w-[24rem]" />
          <col className="w-[28rem]" />
          <col className="w-[15rem]" />
          <col className="w-[22rem]" />
        </colgroup>
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <Head>Ticker</Head>
            <Head>Signal State</Head>
            <Head>Contract</Head>
            <Head className="text-right"><HelpLabel label="Option Mid" detail="Current option midpoint from the stored chain snapshot. Est fill adds the strategy slippage assumption used by shadow entries." /></Head>
            <Head className="text-right"><HelpLabel label="10x Math" detail="Underlying stock price where intrinsic value is about ten times the option mid. Fill target uses the estimated fill premium when it differs. Cap room is how far the estimated fill is below the strategy premium ceiling." /></Head>
            <Head>Signal Evidence</Head>
            <Head>Thesis</Head>
            <Head>Impact So Far</Head>
            <Head>Next Action</Head>
          </tr>
        </thead>
        <tbody>
          {visibleRows.map((row) => {
            const ticker = textField(row, ["ticker"]);
            const state = stateOf(row);
            const qualityStatus = textField(row, ["quality_status"], "ok").toLowerCase();
            const qualityFlags = listField(row, ["quality_flags"]);
            const eventId = textField(row, ["event_id"]);
            const mark = latestMarkByEvent.get(eventId);
            const attribution = latestAttributionByEvent.get(eventId);
            const request = thesisRequestByEvent.get(eventId);
            const validation = latestThesisValidationByEvent.get(eventId);
            const thesis = latestAgentThesisByTicker.get(ticker);
            const thesisExpanded = expandedThesisEvent === eventId;
            const rowKey = textField(row, ["event_id"], `${ticker}-${textField(row, ["contract_id"])}`);
            return (
              <Fragment key={rowKey}>
                <tr
                  className={cn(
                    "border-b border-border align-top transition-colors hover:bg-accent/40",
                    qualityStatus === "bad" && "bg-destructive/5",
                    qualityStatus === "caution" && "bg-amber-500/5",
                  )}
                >
                  <Cell>{ticker ? <TickerButton ticker={ticker} onOpenTicker={onOpenTicker} /> : "-"}</Cell>
                  <Cell>
                    <div className="space-y-1.5">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <StatusBadge tone={stateTone(state)}>{titleLabel(state || "pending")}</StatusBadge>
                        <QualityIndicator status={qualityStatus} flags={qualityFlags} />
                      </div>
                      <div className="text-xs text-muted-foreground">conviction {formatScore(candidateConviction(row))}</div>
                    </div>
                  </Cell>
                  <Cell>
                    <FullText>{displayField(row, ["contract_id"])}</FullText>
                    <div className="mt-1 text-xs text-muted-foreground">{candidateFamily(row)} | {titleLabel(stringFromRecord(recordField(row, "raw"), "option_type", "call"))} {formatShortDate(stringFromRecord(recordField(row, "raw"), "expiration"))}</div>
                  </Cell>
                  <Cell className="text-right tabular-nums">
                    <div>{moneyField(row, ["premium_mid"])}</div>
                    <div className="text-xs text-muted-foreground">fill {moneyField(row, ["premium_fill_assumption"])}</div>
                  </Cell>
                  <Cell className="text-right tabular-nums">
                    <div>{moneyField(row, ["required_10x_price"])}</div>
                    <FillTarget row={row} />
                    <PremiumCapHint row={row} />
                  </Cell>
                  <Cell><CandidateSignalEvidence row={row} /></Cell>
                  <Cell>
                    <ThesisCompactSummary
                      request={request}
                      validation={validation}
                      thesis={thesis}
                      expanded={thesisExpanded}
                      onToggle={() => setExpandedThesisEvent(thesisExpanded ? null : eventId)}
                    />
                  </Cell>
                  <Cell><OpportunityOutcome mark={mark} attribution={attribution} /></Cell>
                  <Cell><FullText>{candidateActionText(row, validation)}</FullText></Cell>
                </tr>
                {thesisExpanded ? (
                  <tr className="border-b border-border bg-muted/20">
                    <td colSpan={9} className="px-3 py-4">
                      <div className="max-w-5xl rounded-md border border-border bg-card p-4">
                        <OpportunityThesisSummary row={row} request={request} validation={validation} thesis={thesis} agentRuntime={agentRuntime} />
                      </div>
                    </td>
                  </tr>
                ) : null}
              </Fragment>
            );
          })}
        </tbody>
      </table>
      <div className="flex flex-col gap-2 border-t border-border p-3 text-sm text-muted-foreground sm:flex-row sm:items-center sm:justify-between">
        <div>
          Showing {focusedRows.length ? (boundedPage * CANDIDATE_PAGE_SIZE + 1).toLocaleString() : "0"}-{Math.min((boundedPage + 1) * CANDIDATE_PAGE_SIZE, focusedRows.length).toLocaleString()} of {focusedRows.length.toLocaleString()}
        </div>
        <div className="flex items-center gap-2">
          <Button type="button" variant="outline" size="sm" disabled={boundedPage === 0} onClick={() => setPage((current) => Math.max(0, current - 1))} aria-label="Previous candidate page">
            <ChevronLeft className="size-4" />
          </Button>
          <span className="min-w-24 text-center tabular-nums">Page {boundedPage + 1} / {pageCount}</span>
          <Button type="button" variant="outline" size="sm" disabled={boundedPage >= pageCount - 1} onClick={() => setPage((current) => Math.min(pageCount - 1, current + 1))} aria-label="Next candidate page">
            <ChevronRight className="size-4" />
          </Button>
        </div>
      </div>
    </DataTableFrame>
  );
}

export function CandidateMobileCard({
  row,
  request,
  validation,
  thesis,
  mark,
  attribution,
  agentRuntime,
  expanded,
  onToggleThesis,
  onOpenTicker,
}: {
  row: RowRecord;
  request: RowRecord | undefined;
  validation: RowRecord | undefined;
  thesis: RowRecord | undefined;
  mark: RowRecord | undefined;
  attribution: RowRecord | undefined;
  agentRuntime: OptionThesisAgentRuntime;
  expanded: boolean;
  onToggleThesis: () => void;
  onOpenTicker: OpenTicker;
}) {
  const state = stateOf(row);
  const qualityStatus = textField(row, ["quality_status"], "ok").toLowerCase();
  const qualityFlags = listField(row, ["quality_flags"]);
  return (
    <article className={cn("rounded-md border border-border bg-card p-3", qualityStatus === "bad" && "border-destructive/40 bg-destructive/5", qualityStatus === "caution" && "border-amber-500/30 bg-amber-50/30 dark:bg-amber-950/10")}>
      <div className="flex min-w-0 items-start justify-between gap-3">
        <div className="min-w-0">
          <TickerButton ticker={textField(row, ["ticker"])} onOpenTicker={onOpenTicker} />
          <FullText>{displayField(row, ["contract_id"])}</FullText>
          <div className="mt-1 text-xs text-muted-foreground">
            {titleLabel(stringFromRecord(recordField(row, "raw"), "option_type", "call"))} {formatShortDate(stringFromRecord(recordField(row, "raw"), "expiration"))}
          </div>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <div className="flex items-center gap-1.5">
            <StatusBadge tone={stateTone(state)}>{titleLabel(state || "pending")}</StatusBadge>
            <QualityIndicator status={qualityStatus} flags={qualityFlags} />
          </div>
          <div className="text-xs text-muted-foreground">conviction {formatScore(candidateConviction(row))}</div>
        </div>
      </div>

      <div className="mt-3 grid grid-cols-3 gap-2">
        <InlineMetric label="Premium" value={moneyField(row, ["premium_mid"])} />
        <InlineMetric label="10x Price" value={moneyField(row, ["required_10x_price"])} />
        <InlineMetric label="Move" value={formatRatio(numberField(row, ["required_move_pct"], Number.NaN))} />
      </div>

      <div className="mt-3 space-y-3">
        <MobileSection label="Signal Evidence"><CandidateSignalEvidence row={row} /></MobileSection>
        <MobileSection label="Thesis">
          <ThesisCompactSummary request={request} validation={validation} thesis={thesis} expanded={expanded} onToggle={onToggleThesis} />
          {expanded ? (
            <div className="mt-3 rounded-md border border-border bg-background p-3">
              <OpportunityThesisSummary row={row} request={request} validation={validation} thesis={thesis} agentRuntime={agentRuntime} />
            </div>
          ) : null}
        </MobileSection>
        <MobileSection label="Impact So Far"><OpportunityOutcome mark={mark} attribution={attribution} /></MobileSection>
        <MobileSection label="Next Action"><FullText>{candidateActionText(row, validation)}</FullText></MobileSection>
      </div>
    </article>
  );
}

export function ThesisCompactSummary({
  request,
  validation,
  thesis,
  expanded,
  onToggle,
}: {
  request: RowRecord | undefined;
  validation: RowRecord | undefined;
  thesis: RowRecord | undefined;
  expanded: boolean;
  onToggle: () => void;
}) {
  const requestStatus = textField(request, ["status"]);
  const hasDetail = Boolean(validation || thesis || request);
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-1.5">
        <StatusBadge tone={thesisStateTone(textField(validation, ["state"]))}>{thesisValidationLabel(validation)}</StatusBadge>
        {requestStatus ? <StatusBadge tone={toneFromText(requestStatus)}>{titleLabel(requestStatus)}</StatusBadge> : null}
      </div>
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="h-8"
        onClick={onToggle}
        disabled={!hasDetail}
        aria-expanded={expanded}
      >
        <ChevronDown className={cn("size-4 transition-transform", expanded && "rotate-180")} />
        <span>{expanded ? "Hide thesis" : hasDetail ? "Expand thesis" : "No thesis detail"}</span>
      </Button>
    </div>
  );
}

export function CandidateSignalEvidence({ row }: { row: RowRecord }) {
  const raw = recordField(row, "raw");
  const hardRejects = listFromRecord(raw, "hard_rejects");
  const blockers = listFromRecord(raw, "blockers");
  const positives = listFromRecord(raw, "positives");
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-1.5">
        <MetricPill label="Conviction" value={formatScore(candidateConviction(row))} />
        <MetricPill label="Move" value={formatRatio(numberField(row, ["required_move_pct"], Number.NaN))} />
      </div>
      {hardRejects.length ? <ReadableReasonGroup label="Hard rejects" reasons={hardRejects} tone="bad" /> : null}
      {blockers.length ? <ReadableReasonGroup label="Blockers" reasons={blockers} tone="warn" /> : null}
      {positives.length ? <ReadableReasonGroup label="Supports" reasons={positives} tone="good" /> : null}
      {!hardRejects.length && !blockers.length && !positives.length ? <StatusBadge tone="muted">No stored evidence</StatusBadge> : null}
    </div>
  );
}

