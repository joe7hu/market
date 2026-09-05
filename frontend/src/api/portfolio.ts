/** Portfolio transaction requests and their backend-owned contracts. */

import type { components } from "../generated/apiSchema";
import { getJson, sendJson } from "../apiTransport";

type ApiSchema = components["schemas"];

export type PortfolioTransactionInput = Omit<ApiSchema["PortfolioTransactionInput"], "account" | "currency"> & {
  account?: ApiSchema["PortfolioTransactionInput"]["account"];
  currency?: ApiSchema["PortfolioTransactionInput"]["currency"];
};
export type PortfolioTransactionPreview = ApiSchema["PortfolioTransactionPreviewResponse"];
export type PortfolioTransactionResult = ApiSchema["PortfolioTransactionResultResponse"];
export type ManualAccountResponse = ApiSchema["ManualAccountResponse"];
export type ManualAccountPreview = ApiSchema["ManualAccountPreviewResponse"];
export type ManualAccountInput = Omit<ApiSchema["ManualAccountReconciliationInput"], "account" | "currency" | "expected_reconciliation_version"> & {
  expected_reconciliation_version?: number;
};

export function getManualAccount(): Promise<ManualAccountResponse> {
  return getJson<ManualAccountResponse>("/api/portfolio/account");
}

export function previewManualAccount(input: ManualAccountInput): Promise<ManualAccountPreview> {
  return sendJson<ManualAccountPreview>("/api/portfolio/account/reconciliation/preview", "POST", normalizeManualAccount(input));
}

export function recordManualAccount(input: ManualAccountInput): Promise<ManualAccountResponse> {
  return sendJson<ManualAccountResponse>("/api/portfolio/account/reconciliation", "POST", normalizeManualAccount(input));
}

export async function previewPortfolioTransaction(
  transaction: PortfolioTransactionInput,
): Promise<PortfolioTransactionPreview> {
  return sendJson<PortfolioTransactionPreview>("/api/portfolio/transactions/preview", "POST", normalizeTransaction(transaction));
}

export async function recordPortfolioTransaction(
  transaction: PortfolioTransactionInput,
): Promise<PortfolioTransactionResult> {
  return sendJson<PortfolioTransactionResult>("/api/portfolio/transactions", "POST", normalizeTransaction(transaction));
}

function normalizeTransaction(transaction: PortfolioTransactionInput): ApiSchema["PortfolioTransactionInput"] {
  return {
    account: "manual",
    currency: "USD",
    ...transaction,
  };
}

function normalizeManualAccount(input: ManualAccountInput): ApiSchema["ManualAccountReconciliationInput"] {
  return { account: "manual", currency: "USD", ...input };
}

export async function reversePortfolioTransaction(
  transactionId: string,
  idempotencyKey: string,
): Promise<PortfolioTransactionResult> {
  return sendJson<PortfolioTransactionResult>(
    `/api/portfolio/transactions/${encodeURIComponent(transactionId)}/reverse`,
    "POST",
    { idempotency_key: idempotencyKey, notes: "Reversed from portfolio activity" },
  );
}
