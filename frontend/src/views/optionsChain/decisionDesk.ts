import type { OptionsDecisionBrief } from "@/api";

export type DecisionPresentation = {
  eyebrow: string;
  title: string;
  detail: string;
  tone: "good" | "warn" | "info" | "muted";
  action: "thesis" | "evidence" | "record";
  actionLabel: string;
};

const BLOCKER_COPY: Record<string, { label: string; detail: string }> = {
  insufficient_eligible_points: {
    label: "Too few comparable contracts",
    detail: "The expiry and option type do not have enough clean points for a reliable fit.",
  },
  unsupported_dte: {
    label: "Outside the supported DTE window",
    detail: "The contract is too near or too far from expiry for the current underwriting model.",
  },
  missing_aligned_underlying: {
    label: "Underlying price not aligned",
    detail: "QQQ and option quotes were not observed close enough together.",
  },
  missing_or_stale_underlying: {
    label: "Underlying quote stale",
    detail: "The QQQ reference price is missing or too old for a coherent comparison.",
  },
  illiquid_open_interest: {
    label: "Open interest too low",
    detail: "The contract does not clear the minimum liquidity standard.",
  },
  incomplete_or_crossed_quote: {
    label: "Quote incomplete or crossed",
    detail: "The bid/ask cannot support a conservative paper entry.",
  },
  illiquid_spread: {
    label: "Bid/ask spread too wide",
    detail: "Execution friction consumes too much of the modeled edge.",
  },
  outside_moneyness_window: {
    label: "Outside the underwriting strike window",
    detail: "The strike is too far from spot for the current model.",
  },
};

export function decisionPresentation(brief: OptionsDecisionBrief): DecisionPresentation {
  const canaryRemaining = Math.max(
    0,
    brief.readiness.canary.required_regular_sessions - brief.readiness.canary.qualified_regular_sessions,
  );
  if (brief.state === "PAPER_READY" && brief.strongest_candidate) {
    return {
      eyebrow: "Actionable paper setup",
      title: "Paper setup ready for review",
      detail: "The setup cleared the current evidence, thesis, liquidity, calibration, and canary gates. Re-quote before staging.",
      tone: "good",
      action: "record",
      actionLabel: "Review paper record",
    };
  }
  if (!brief.readiness.thesis.eligible) {
    return {
      eyebrow: "Current verdict",
      title: "No trade — QQQ thesis required",
      detail: "The chain and model are producing evidence, but the system will not underwrite a directional option without a current thesis and explicit invalidation.",
      tone: "warn",
      action: "thesis",
      actionLabel: "Create or update QQQ thesis",
    };
  }
  if (canaryRemaining > 0) {
    return {
      eyebrow: "Current verdict",
      title: `Wait — ${canaryRemaining} qualified session${canaryRemaining === 1 ? "" : "s"} remaining`,
      detail: "Research evidence is available, but the model revision has not completed its reliability gate for paper readiness.",
      tone: "info",
      action: "evidence",
      actionLabel: "Inspect market evidence",
    };
  }
  if (brief.state === "REJECT") {
    return {
      eyebrow: "Current verdict",
      title: "Pass — setup rejected",
      detail: "The current evidence does not justify the modeled risk or execution friction. Preserve the rejection and wait for a new capture.",
      tone: "warn",
      action: "evidence",
      actionLabel: "Inspect rejection evidence",
    };
  }
  if (!brief.strongest_candidate) {
    return {
      eyebrow: "Current verdict",
      title: "No trade — no setup cleared the filters",
      detail: "The system is healthy and evaluating the chain. None of the current structures has enough clean edge to deserve a paper entry.",
      tone: "muted",
      action: "evidence",
      actionLabel: "Inspect market evidence",
    };
  }
  return {
    eyebrow: "Current verdict",
    title: "Watch — setup still gated",
    detail: "A research candidate exists, but one or more underwriting or calibration gates remain open.",
    tone: "info",
    action: "evidence",
    actionLabel: "Review candidate evidence",
  };
}

export function blockerCopy(blocker: string): { label: string; detail: string } {
  return BLOCKER_COPY[blocker] ?? {
    label: sentence(blocker),
    detail: "This evidence row did not clear the current underwriting policy.",
  };
}

export function summaryNumber(brief: OptionsDecisionBrief, key: string): number {
  const value = brief.summary[key];
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

export function sentence(value: string): string {
  const text = value.replaceAll("_", " ").trim();
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : "Unknown";
}
