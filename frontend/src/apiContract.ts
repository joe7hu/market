/** Stable names for the generated FastAPI contract. Keep adapters here. */

import type { components, paths } from "./generated/apiSchema";

export type ApiSchema = components["schemas"];
export type ApiPath = keyof paths;

export type JsonResponse<
  Path extends ApiPath,
  Method extends keyof paths[Path],
> = paths[Path][Method] extends { responses: { 200: { content: { "application/json": infer Payload } } } }
  ? Payload
  : never;
