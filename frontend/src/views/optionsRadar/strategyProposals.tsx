// Strategy proposal table + cards.

import {CheckCircle2, GitBranchPlus, Loader2 } from "lucide-react";
import {EmptyState, StatusBadge } from "@/components/market/workstation";
import {Button } from "@/components/ui/button";
import {cn } from "@/lib/utils";
import {RowRecord } from "@/types";
import {displayField, textField, titleLabel, toneFromText } from "../rowFormat";
import {formatDate } from "../optionsRadarFormat";
import {toneText } from "../optionsRadarTone";
import {proposalGateSummary, proposalChangeItems, proposalChangeNote, compactStrategyVersion, backtestDetail, forwardDetail} from "./helpers";
import {GatePill, InsightLine } from "./shared";

export function StrategyProposalsTable({
  rows,
  backtestByProposal,
  forwardByProposal,
  promotingProposal,
  promotionError,
  onPromote,
}: {
  rows: RowRecord[];
  backtestByProposal: Map<string, RowRecord>;
  forwardByProposal: Map<string, RowRecord>;
  promotingProposal: string | null;
  promotionError: string | null;
  onPromote: (proposalId: string) => Promise<void> | void;
}) {
  if (!rows.length) {
    return <EmptyState title="No active challenger" detail="The professional scorer remains the champion. A challenger appears here only after a current, typed hypothesis enters deterministic replay; legacy imported proposals are archived and never promotable." icon={GitBranchPlus} />;
  }

  return (
    <section className="rounded-md border border-border bg-card p-4">
      <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
        <div>
          <h2 className="text-base font-semibold">Strategy Mutation Gates</h2>
          <p className="mt-1 max-w-4xl text-sm leading-6 text-muted-foreground">
            Agent proposals are advisory until deterministic backtest, forward shadow test, and human approval all clear. Failed gates are shown as blockers, not action items.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <StatusBadge tone="muted">{rows.length.toLocaleString()} proposals</StatusBadge>
          {promotionError ? <StatusBadge tone="bad">{promotionError}</StatusBadge> : null}
        </div>
      </div>
      <div className="mt-4 grid gap-3 xl:grid-cols-2">
        {rows.slice(0, 8).map((row) => {
          const proposalId = textField(row, ["proposal_id"]);
          return (
            <StrategyProposalCard
              key={proposalId || textField(row, ["proposed_strategy_version"])}
              row={row}
              backtest={backtestByProposal.get(proposalId)}
              forward={forwardByProposal.get(proposalId)}
              isPromoting={promotingProposal === proposalId}
              onPromote={onPromote}
            />
          );
        })}
      </div>
    </section>
  );
}

export function StrategyProposalCard({
  row,
  backtest,
  forward,
  isPromoting,
  onPromote,
}: {
  row: RowRecord;
  backtest: RowRecord | undefined;
  forward: RowRecord | undefined;
  isPromoting: boolean;
  onPromote: (proposalId: string) => Promise<void> | void;
}) {
  const proposalId = textField(row, ["proposal_id"]);
  const status = textField(row, ["status"], "pending");
  const human = textField(row, ["human_approval_status"], "pending");
  const backtestVerdict = textField(backtest, ["verdict"]).toLowerCase();
  const forwardVerdict = textField(forward, ["verdict", "status"]).toLowerCase();
  const approvedBy = textField(row, ["approved_by"]);
  const approvedAt = textField(row, ["approved_at"]);
  const canPromote =
    Boolean(proposalId) &&
    status === "ready_for_human_review" &&
    human !== "approved" &&
    backtestVerdict === "pass" &&
    forwardVerdict === "pass";
  const gate = proposalGateSummary(row, backtest, forward);
  const changes = proposalChangeItems(row);
  const note = proposalChangeNote(row);

  return (
    <article className="rounded-md border border-border/70 bg-background p-3">
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge tone={gate.tone}>{gate.label}</StatusBadge>
            <StatusBadge tone={toneFromText(status)}>{titleLabel(status)}</StatusBadge>
            <StatusBadge tone={human === "approved" ? "good" : human === "rejected" ? "bad" : "warn"}>{titleLabel(human)}</StatusBadge>
          </div>
          <h3 className="mt-2 text-sm font-semibold leading-6">{compactStrategyVersion(displayField(row, ["proposed_strategy_version"]))}</h3>
          {approvedBy || approvedAt ? (
            <div className="mt-1 text-xs text-muted-foreground">{approvedBy ? `approved by ${approvedBy}` : "approved"}{approvedAt ? ` ${formatDate(approvedAt)}` : ""}</div>
          ) : null}
        </div>
        {human === "approved" ? (
          <StatusBadge tone="good">Promoted</StatusBadge>
        ) : (
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-9 min-h-9 w-fit"
            disabled={!canPromote || isPromoting}
            title={canPromote ? "Promote strategy" : gate.detail}
            onClick={() => void onPromote(proposalId)}
          >
            {isPromoting ? <Loader2 className="animate-spin" /> : <CheckCircle2 />}
            <span>Promote</span>
          </Button>
        )}
      </div>

      <div className="mt-3 grid gap-2 md:grid-cols-3">
        <GatePill label="Backtest" row={backtest} keys={["verdict"]} detail={backtestDetail(backtest)} />
        <GatePill label="Forward" row={forward} keys={["verdict", "status"]} detail={forwardDetail(forward)} />
        <div className="rounded-md bg-muted px-2 py-2">
          <div className="text-[10px] font-semibold uppercase text-muted-foreground">Gate Meaning</div>
          <div className={cn("mt-1 text-xs font-semibold", toneText(gate.tone))}>{gate.detail}</div>
        </div>
      </div>

      {note ? <InsightLine className="mt-3" label="Agent hypothesis" value={note} /> : null}
      {changes.length ? (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {changes.slice(0, 6).map((change) => (
            <span key={change} className="rounded-md border border-border/70 bg-card px-2 py-1 text-xs text-muted-foreground">{change}</span>
          ))}
        </div>
      ) : null}
      <div className="mt-3 space-y-2 text-sm leading-6">
        <InsightLine label="Why proposed" value={displayField(row, ["rationale"])} />
        <InsightLine label="Risk" value={displayField(row, ["risk"])} />
      </div>
    </article>
  );
}
