import type { components } from "@/generated/apiSchema";

export type ExpressionKind = components["schemas"]["ExpressionKind"];

const labels: Record<ExpressionKind, string> = {
  STOCK: "Stock",
  CALL: "Call",
  PUT: "Put",
  DEBIT_SPREAD: "Debit spread",
  CASH_SECURED_PUT: "Cash-secured put",
  CRYPTO_SPOT: "Crypto spot",
  CRYPTO_PERPETUAL: "Crypto perpetual",
  CASH: "Cash",
};

export function expressionLabel(kind: ExpressionKind): string {
  return labels[kind];
}
