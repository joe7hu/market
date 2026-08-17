#!/usr/bin/env node
import assert from "node:assert/strict";
import { setPublicBase, startPoc } from "./poc.mjs";

const port = 8811;
const poc = await startPoc({ apiPort: port, pagePort: port + 1, secondPort: port + 2 });
const endpoint = `http://127.0.0.1:${port}/mcp`;
const call = async (message, sessionId) => {
  const response = await fetch(endpoint, { method: "POST", headers: { "content-type": "application/json", ...(sessionId ? { "mcp-session-id": sessionId } : {}) }, body: JSON.stringify(message) });
  return { status: response.status, sessionId: response.headers.get("mcp-session-id"), body: response.status === 202 ? null : await response.json() };
};
try {
  setPublicBase("https://native-chat-poc.example.test");
  const initialized = await call({ jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-03-26", capabilities: {}, clientInfo: { name: "verifier", version: "1" } } });
  assert.equal(initialized.status, 200); assert.ok(initialized.sessionId); assert.equal(initialized.body.result.serverInfo.name, "native-chat-host-reach-poc");
  const sessionId = initialized.sessionId;
  const listed = await call({ jsonrpc: "2.0", id: 2, method: "tools/list", params: {} }, sessionId);
  assert.deepEqual(listed.body.result.tools.map((tool) => tool.name), ["poc_probe_host_reach", "poc_get_approval_status"]);
  const probed = await call({ jsonrpc: "2.0", id: 3, method: "tools/call", params: { name: "poc_probe_host_reach", arguments: { nonce: "mcp-local-verification" } } }, sessionId);
  assert.equal(probed.body.result.structuredContent.approvalUrl, "https://native-chat-poc.example.test/approve/mcp-local-verification");
  const status = await call({ jsonrpc: "2.0", id: 4, method: "tools/call", params: { name: "poc_get_approval_status", arguments: { nonce: "mcp-local-verification" } } }, sessionId);
  assert.equal(status.body.result.structuredContent.status, "awaiting_out_of_band_approval");
  const rejected = await call({ jsonrpc: "2.0", id: 5, method: "tools/list", params: {} });
  assert.equal(rejected.status, 401); assert.equal(rejected.body.error.message, "Unknown MCP session");
  console.log(JSON.stringify({ ok: true, checks: ["initialize", "tool discovery", "read-only probe", "read-only status", "session rejection"] }));
} finally { await poc.close(); }
