// Pure value -> Tone / label mappers extracted from optionsRadar.tsx. These map
// domain strings (states, tiers, validation statuses, reasons) to display tones
// and human labels; no JSX, no component deps.

import type { RowRecord } from "@/types";
import type { Tone } from "@/ui/tone";

import { textField, titleLabel, toneFromText } from "./rowFormat";

export function stateRank(state: string): number {
  if (state === "FIRE") return 0;
  if (state === "SETUP") return 1;
  if (state === "WATCH") return 2;
  return 3;
}

export function stateTone(state: string): Tone {
  const normalized = state.toUpperCase();
  if (normalized === "FIRE" || normalized === "HOLD") return "good";
  if (normalized === "SETUP" || normalized === "TRIM") return "warn";
  if (normalized === "INVALIDATED" || normalized === "EXIT") return "bad";
  if (normalized === "WATCH") return "info";
  return "muted";
}

export function tierTone(tier: string): Tone {
  if (tier === "Exceptional") return "good";
  if (tier === "Service Bug") return "bad";
  if (tier === "Research") return "info";
  return "muted";
}

export function thesisStateTone(state: string): Tone {
  const normalized = state.toLowerCase();
  if (normalized.includes("invalidated")) return "bad";
  if (normalized.includes("validated")) return "good";
  if (normalized.includes("tracking")) return "info";
  if (normalized.includes("weakening")) return "warn";
  if (!normalized) return "muted";
  return "info";
}

export function thesisValidationLabel(validation: RowRecord | undefined): string {
  const state = textField(validation, ["state"]);
  if (!state) return "No validation";
  if (state.toLowerCase() === "pending") return "Needs proof";
  return titleLabel(state);
}

export function validationStatusLabel(status: string): string {
  const normalized = status.toLowerCase();
  if (normalized === "hard_risk_triggered") return "Fundamental risk flagged";
  return titleLabel(status);
}

export function validationStatusTone(status: string): Tone {
  const normalized = status.toLowerCase();
  if (["supported", "scheduled", "source_confirmed", "clear", "source_backed"].includes(normalized)) return "good";
  if (["partial", "pending", "agent_cited", "source_context_available", "news_only", "agent_only"].includes(normalized)) return "warn";
  if (["breached", "missing", "hard_risk_triggered"].includes(normalized)) return "bad";
  return "muted";
}

export function attributionTone(label: string): Tone {
  const normalized = label.toLowerCase();
  if (normalized.includes("good") || normalized.includes("convexity")) return "good";
  if (normalized.includes("crush") || normalized.includes("decay") || normalized.includes("risk")) return "bad";
  if (normalized.includes("bleed") || normalized.includes("spread")) return "warn";
  return "info";
}

export function verdictTone(value: string): Tone {
  const normalized = value.toLowerCase();
  if (normalized === "pass" || normalized === "complete") return "good";
  if (normalized === "fail") return "bad";
  if (normalized.includes("collecting") || normalized.includes("pending") || normalized.includes("active")) return "warn";
  return toneFromText(normalized);
}

export function toneText(tone: Tone): string {
  if (tone === "good") return "text-green-700 dark:text-green-300";
  if (tone === "warn") return "text-amber-700 dark:text-amber-300";
  if (tone === "bad") return "text-destructive";
  if (tone === "info") return "text-blue-700 dark:text-blue-300";
  return "text-foreground";
}

const reasonLabels: Record<string, string> = {
  "10x_math_inside_cap": "10x target inside cap",
  asymmetry_below_exceptional_bar: "Asymmetry below top bar",
  conviction_below_exceptional_bar: "Conviction below top bar",
  convexity_inside_extreme_bar: "Convexity inside extreme bar",
  delta_in_range: "Delta in range",
  delta_outside_strategy_range: "Delta outside range",
  dte_outside_strategy_range: "DTE outside range",
  entry_quality_below_exceptional_bar: "Entry quality below top bar",
  entry_quality_supported: "Entry quality supported",
  hard_red_team_risk: "Fundamental risk flagged",
  iv_not_overpriced: "IV acceptable",
  iv_percentile_above_fire_threshold: "IV above fire limit",
  iv_percentile_reject: "IV too expensive",
  leap_survivability_not_exceptional: "LEAP survivability weak",
  leap_survivability_supported: "LEAP survivability supported",
  market_regime_hostile_to_long_premium: "Market regime hostile",
  missing_50d_context: "Missing 50D context",
  missing_delta: "Missing delta",
  missing_dte: "Missing DTE",
  missing_iv_percentile: "Missing IV rank",
  missing_open_interest: "Missing open interest",
  missing_rs_vs_qqq: "Missing RS context",
  missing_spread: "Missing spread",
  missing_volume: "Missing volume",
  bank_move_implausible_without_validated_catalyst: "Bank move needs validated catalyst",
  regulated_healthcare_move_implausible_without_validated_catalyst: "Regulated healthcare move needs validated catalyst",
  mega_cap_move_implausible_without_validated_catalyst: "Mega-cap move needs validated catalyst",
  no_printed_volume: "No volume print",
  option_chain_terms_sync_gap: "Option terms sync gap",
  option_contract_quote_sync_gap: "Option quote sync gap",
  option_data_conflict: "Option data conflict",
  option_iv_and_delta_sync_gap: "Option IV/Greek sync gap",
  option_liquidity_sync_gap: "Option liquidity sync gap",
  not_fire_state: "Wait for fire setup",
  open_interest_below_threshold: "Open interest too low",
  open_interest_not_exceptional: "Open interest not exceptional",
  open_interest_supported: "Open interest supported",
  premium_above_buy_under: "Option premium too high",
  premium_inside_buy_under: "Premium inside cap",
  provider_quality_flags_present: "Provider quality flags",
  required_move_too_high: "Required move too high",
  required_move_not_exceptional: "Required move not exceptional",
  rs_vs_qqq_20d_negative: "RS vs QQQ weak",
  rs_vs_qqq_improving: "RS vs QQQ improving",
  market_regime_sync_gap: "Market regime sync gap",
  source_evidence_cluster: "Source evidence cluster",
  source_evidence_sync_gap: "Source evidence sync gap",
  source_backed_thesis: "Source-backed thesis",
  spread_above_fire_threshold: "Spread above fire limit",
  spread_not_exceptional: "Spread not exceptional",
  spread_reject: "Spread too wide",
  spread_usable: "Spread usable",
  theme_ai_applications: "AI applications watch",
  theme_ai_biotech: "AI biotech watch",
  theme_ai_infrastructure: "AI infrastructure watch",
  theme_crypto_infrastructure: "Crypto infrastructure watch",
  theme_robotics_physical_ai: "Robotics / physical AI watch",
  theme_space_tech: "Space tech watch",
  stock_above_50d: "Above 50D",
  stock_below_50d: "Below 50D",
  supportive_market_regime: "Supportive market regime",
  strategy_only_tracks_calls: "Strategy tracks calls only",
  stock_context_sync_gap: "Stock context sync gap",
  thesis_synthesis_sync_gap: "Thesis synthesis sync gap",
  thesis_invalidated: "Thesis invalidated",
  thesis_validated: "Thesis validated",
  volume_below_threshold: "Volume too low",
  volume_seen: "Volume seen",
  wait_for_fire_setup: "Wait for fire setup",
};

export function reasonLabel(reason: string): string {
  return reasonLabels[reason] ?? titleLabel(reason);
}
