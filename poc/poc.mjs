#!/usr/bin/env node
/**
 * Local-only POC harness for browser key custody (T2), CORS (T3), and the
 * optional ChatGPT Actions host-reach exercise (T1). It uses only Node APIs.
 */
import { createHash, randomBytes, webcrypto } from "node:crypto";
import { createServer, request as httpRequest } from "node:http";
import { fileURLToPath } from "node:url";

const subtle = webcrypto.subtle;
const encoder = new TextEncoder();
const store = {
  enrolments: new Map(),
 actions: new Map(),
 challenges: new Map(),
  mcpSessions: new Map(),
  events: [],
  publicBase: process.env.PUBLIC_BASE_URL || null,
};

const here = new URL(".", import.meta.url);
const html = (name) => new URL(`./${name}`, here);
const b64url = (bytes) => Buffer.from(bytes).toString("base64url");
const nonce = () => b64url(randomBytes(24));
const json = (res, status, value, headers = {}) => {
  res.writeHead(status, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...headers });
  res.end(JSON.stringify(value, null, 2));
};
const text = (res, status, value, headers = {}) => {
  res.writeHead(status, { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store", ...headers });
  res.end(value);
};
const bodyJson = async (req) => {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const raw = Buffer.concat(chunks).toString();
  return raw ? JSON.parse(raw) : {};
};
const event = (kind, request, detail = {}) => {
  store.events.push({ at: new Date().toISOString(), kind, method: request.method, path: request.url, origin: request.headers.origin || null, ...detail });
  if (store.events.length > 500) store.events.shift();
};
const canonical = (actionId) => JSON.stringify({ kind: "mock-transfer", amount: "1.00", currency: "USD", actionId });
const digestFor = async (challenge) => new Uint8Array(await subtle.digest("SHA-256", encoder.encode(`${challenge.id}${challenge.nonce}${challenge.canonicalAction}`)));
const origin = (req) => `http://${req.headers.host}`;
const clientIp = (req) => req.headers["cf-connecting-ip"] || req.socket.remoteAddress || null;

async function serveFile(res, filename) {
  const { readFile } = await import("node:fs/promises");
  const type = filename.endsWith(".js") ? "text/javascript" : "text/html";
  res.writeHead(200, { "content-type": `${type}; charset=utf-8`, "cache-control": "no-store" });
  res.end(await readFile(html(filename)));
}

async function verifyAttestation(payload, actionId) {
  const enrolled = store.enrolments.get(payload.user || "demo");
  const challenge = store.challenges.get(payload.challengeId);
  if (!enrolled) return [400, { error: "not_enrolled" }];
  if (!challenge) return [400, { error: "unknown_challenge" }];
  if (challenge.used) return [409, { error: "challenge_already_used" }];
  if (challenge.actionId !== actionId) return [400, { error: "action_binding_mismatch" }];
  if (payload.pinProof !== enrolled.pinProof) return [401, { error: "pin_invalid" }];
  const publicKey = await subtle.importKey("spki", Buffer.from(enrolled.publicKey, "base64url"), { name: "ECDSA", namedCurve: "P-256" }, true, ["verify"]);
  const valid = await subtle.verify({ name: "ECDSA", hash: "SHA-256" }, publicKey, Buffer.from(payload.signature, "base64url"), await digestFor(challenge));
  if (!valid) return [400, { error: "signature_invalid" }];
  challenge.used = true;
  return [200, { ok: true, actionId, challengeId: challenge.id, state: "attested" }];
}

function apiCorsHeaders(req, mode) {
  if (mode !== "B") return {};
  return {
    "access-control-allow-origin": req.headers.origin || "null",
    "access-control-allow-headers": "content-type",
    "access-control-allow-methods": "POST, OPTIONS",
    vary: "Origin",
  };
}

function jsonRpc(res, status, id, result, headers = {}) {
  return json(res, status, { jsonrpc: "2.0", id, result }, headers);
}

function jsonRpcError(res, id, code, message, status = 400) {
  return json(res, status, { jsonrpc: "2.0", id: id ?? null, error: { code, message } });
}

const mcpTools = [
  {
    name: "poc_probe_host_reach",
    title: "Probe host reach",
    description: "Read-only POC: records a unique nonce and returns a top-level mock approval URL.",
    inputSchema: { type: "object", additionalProperties: false, required: ["nonce"], properties: { nonce: { type: "string", minLength: 1, maxLength: 128, description: "Unique test identifier." } } },
  },
  {
    name: "poc_get_approval_status",
    title: "Get mock approval status",
    description: "Read-only POC: returns the mock status for a previously issued nonce.",
    inputSchema: { type: "object", additionalProperties: false, required: ["nonce"], properties: { nonce: { type: "string", minLength: 1, maxLength: 128, description: "Unique test identifier." } } },
  },
];

function validNonce(value) {
  return typeof value === "string" && value.length > 0 && value.length <= 128;
}

async function handleMcp(req, res) {
  if (req.method === "GET") return text(res, 405, "MCP endpoint accepts JSON-RPC POST requests only.", { allow: "POST" });
  if (req.method !== "POST") return text(res, 405, "Method not allowed", { allow: "POST" });
  const message = await bodyJson(req);
  if (message.jsonrpc !== "2.0" || typeof message.method !== "string") return jsonRpcError(res, message.id, -32600, "Invalid JSON-RPC request");
  const id = message.id;
  const sessionId = req.headers["mcp-session-id"];
  if (message.method === "initialize") {
    const newSessionId = `mcp_${nonce()}`;
    store.mcpSessions.set(newSessionId, { createdAt: new Date().toISOString() });
    event("mcp_initialize", req, { clientIp: clientIp(req), protocolVersion: message.params?.protocolVersion || null });
    return jsonRpc(res, 200, id, {
      protocolVersion: "2025-03-26",
      capabilities: { tools: { listChanged: false } },
      serverInfo: { name: "native-chat-host-reach-poc", version: "1.0.0" },
      instructions: "Read-only host-reach POC. It never executes a payment or approval action.",
    }, { "mcp-session-id": newSessionId });
  }
  if (!sessionId || !store.mcpSessions.has(sessionId)) return jsonRpcError(res, id, -32001, "Unknown MCP session", 401);
  if (message.method === "notifications/initialized") return res.writeHead(202).end();
  if (message.method === "tools/list") return jsonRpc(res, 200, id, { tools: mcpTools });
  if (message.method !== "tools/call") return jsonRpcError(res, id, -32601, "Method not found", 404);
  const { name, arguments: args = {} } = message.params || {};
  if (!validNonce(args.nonce)) return jsonRpc(res, 200, id, { content: [{ type: "text", text: "nonce must be a non-empty string no longer than 128 characters" }], isError: true });
  const base = store.publicBase || origin(req);
  if (name === "poc_probe_host_reach") {
    const result = { ok: true, nonce: args.nonce, approvalUrl: `${base}/approve/${encodeURIComponent(args.nonce)}`, statusUrl: `${base}/status/${encodeURIComponent(args.nonce)}` };
    event("mcp_probe", req, { nonce: args.nonce, clientIp: clientIp(req), userAgent: req.headers["user-agent"] || null });
    return jsonRpc(res, 200, id, { content: [{ type: "text", text: JSON.stringify(result) }], structuredContent: result });
  }
  if (name === "poc_get_approval_status") {
    const result = { nonce: args.nonce, status: "awaiting_out_of_band_approval", readOnly: true };
    event("mcp_status", req, { nonce: args.nonce, clientIp: clientIp(req), userAgent: req.headers["user-agent"] || null });
    return jsonRpc(res, 200, id, { content: [{ type: "text", text: JSON.stringify(result) }], structuredContent: result });
  }
  return jsonRpc(res, 200, id, { content: [{ type: "text", text: `Unknown tool: ${String(name)}` }], isError: true });
}

async function handler(role, req, res) {
  const url = new URL(req.url, origin(req));
  const pathname = url.pathname;
  if (pathname === "/" || pathname === "/custody.html") return serveFile(res, "custody.html");
  if (pathname === "/t3.html") return serveFile(res, "t3.html");
  if (pathname === "/poc-client.js") return serveFile(res, "poc-client.js");
  if (pathname === "/mcp") return handleMcp(req, res);
  if (pathname === "/_log") return json(res, 200, { events: store.events, publicBase: store.publicBase, enrolments: store.enrolments.size, challenges: store.challenges.size });
  if (pathname === "/health") return json(res, 200, { ok: true, role });

  if (pathname === "/enrol" && req.method === "POST") {
    const payload = await bodyJson(req);
    if (!payload.publicKey || !payload.pinProof) return json(res, 400, { error: "publicKey_and_pinProof_required" });
    store.enrolments.set(payload.user || "demo", { publicKey: payload.publicKey, pinProof: payload.pinProof, enrolledAt: new Date().toISOString() });
    event("t2_enrol", req, { user: payload.user || "demo" });
    return json(res, 201, { ok: true });
  }
  if (pathname === "/v1/action" && req.method === "POST") {
    const actionId = `act_${nonce()}`;
    const challenge = { id: `chl_${nonce()}`, nonce: nonce(), actionId, canonicalAction: canonical(actionId), used: false };
    store.actions.set(actionId, { id: actionId, state: "challenge_issued" });
    store.challenges.set(challenge.id, challenge);
    event("t2_challenge", req, { actionId, challengeId: challenge.id });
    return json(res, 201, { actionId, challengeId: challenge.id, nonce: challenge.nonce, canonicalAction: challenge.canonicalAction });
  }
  const attestMatch = pathname.match(/^\/v1\/action\/([^/]+)\/attest$/);
  if (attestMatch && req.method === "POST") {
    const payload = await bodyJson(req);
    const [status, response] = await verifyAttestation(payload, decodeURIComponent(attestMatch[1]));
    event("t2_attest", req, { actionId: decodeURIComponent(attestMatch[1]), challengeId: payload.challengeId, status, error: response.error || null });
    return json(res, status, response);
  }

  const corsMatch = pathname.match(/^\/t3\/echo\/(A|B)$/);
  if (corsMatch && (req.method === "OPTIONS" || req.method === "POST")) {
    const mode = corsMatch[1];
    event("t3_api", req, { mode, requestType: req.method, contentType: req.headers["content-type"] || null });
    if (req.method === "OPTIONS") return res.writeHead(204, apiCorsHeaders(req, mode)).end();
    return json(res, 200, { ok: true, mode, reached: true }, apiCorsHeaders(req, mode));
  }

  if (pathname === "/proxy/echo" && role === "page" && req.method === "POST") {
    const payload = await bodyJson(req);
    event("t3_proxy", req, { target: "api" });
    const upstream = await new Promise((resolve, reject) => {
      const forward = httpRequest({ hostname: "127.0.0.1", port: Number(process.env.POC_API_PORT || 8801), path: "/t3/echo/A", method: "POST", headers: { "content-type": "application/json" } }, (upstreamRes) => {
        const chunks = [];
        upstreamRes.on("data", (chunk) => chunks.push(chunk));
        upstreamRes.on("end", () => resolve({ status: upstreamRes.statusCode, body: Buffer.concat(chunks).toString() }));
      });
      forward.on("error", reject);
      forward.end(JSON.stringify(payload));
    });
    return json(res, upstream.status, { proxied: true, upstream: JSON.parse(upstream.body) });
  }

  if (pathname === "/openapi.json" && req.method === "GET") {
    const base = store.publicBase || origin(req);
    return json(res, 200, openApi(base));
  }
  const probeMatch = pathname.match(/^\/probe\/([^/]+)$/);
  if (probeMatch && req.method === "GET") {
    const id = decodeURIComponent(probeMatch[1]);
    event("t1_probe", req, { nonce: id, ip: req.socket.remoteAddress, userAgent: req.headers["user-agent"] || null });
    return json(res, 200, { ok: true, nonce: id, approvalUrl: `${store.publicBase || origin(req)}/approve/${encodeURIComponent(id)}`, statusUrl: `${store.publicBase || origin(req)}/status/${encodeURIComponent(id)}` });
  }
  const statusMatch = pathname.match(/^\/(approve|status)\/([^/]+)$/);
  if (statusMatch && req.method === "GET") {
    event(`t1_${statusMatch[1]}`, req, { nonce: decodeURIComponent(statusMatch[2]), ip: req.socket.remoteAddress, userAgent: req.headers["user-agent"] || null });
    if (statusMatch[1] === "approve") return text(res, 200, `Approval handoff opened for ${decodeURIComponent(statusMatch[2])}. This mock does not execute a real payment.`);
    return json(res, 200, { nonce: decodeURIComponent(statusMatch[2]), status: "awaiting_out_of_band_approval" });
  }
  return json(res, 404, { error: "not_found", path: pathname });
}

function openApi(base) {
  return {
    openapi: "3.1.0", info: { title: "Host Reach POC", version: "1.0.0" }, servers: [{ url: base }],
    paths: {
      "/probe/{nonce}": { get: { operationId: "probeHostReach", parameters: [{ name: "nonce", in: "path", required: true, schema: { type: "string" } }], responses: { "200": { description: "Probe receipt and absolute approval URL" } } } },
      "/status/{nonce}": { get: { operationId: "getApprovalStatus", parameters: [{ name: "nonce", in: "path", required: true, schema: { type: "string" } }], responses: { "200": { description: "Mock approval status" } } } },
    },
  };
}

export function startPoc({ apiPort = Number(process.env.POC_API_PORT || 8801), pagePort = Number(process.env.POC_PAGE_PORT || 8802), secondPort = Number(process.env.POC_SECOND_PORT || 8803), host = "127.0.0.1" } = {}) {
  const listen = (role, port) => new Promise((resolve) => {
    const server = createServer((req, res) => handler(role, req, res).catch((error) => json(res, 500, { error: "internal_error", detail: error.message })));
    server.listen(port, host, () => resolve(server));
  });
  return Promise.all([listen("api", apiPort), listen("page", pagePort), listen("second", secondPort)]).then((servers) => ({ servers, close: () => Promise.all(servers.map((server) => new Promise((resolve) => server.close(resolve)))) }));
}

export function setPublicBase(publicBase) {
  store.publicBase = publicBase;
}

async function main() {
  const poc = await startPoc();
  console.log(`POC T2 API/page: http://127.0.0.1:${process.env.POC_API_PORT || 8801}/custody.html`);
  console.log(`POC T3 page: http://127.0.0.1:${process.env.POC_PAGE_PORT || 8802}/t3.html`);
  console.log(`POC T2 second origin: http://127.0.0.1:${process.env.POC_SECOND_PORT || 8803}/custody.html`);
  const shutdown = async () => { await poc.close(); process.exit(0); };
  process.on("SIGINT", shutdown); process.on("SIGTERM", shutdown);
}

if (process.argv[1] === fileURLToPath(import.meta.url)) main();
