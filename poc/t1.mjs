#!/usr/bin/env node
/** Starts the T1 server and a quick Cloudflare tunnel, then prints live OpenAPI. */
import { spawn } from "node:child_process";
import { setPublicBase, startPoc } from "./poc.mjs";

const apiPort = Number(process.env.POC_T1_PORT || 8787);
const poc = await startPoc({ apiPort, pagePort: apiPort + 1, secondPort: apiPort + 2 });
const tunnel = spawn("cloudflared", ["tunnel", "--url", `http://127.0.0.1:${apiPort}`], { stdio: ["ignore", "pipe", "pipe"] });
let announced = false;
const onLine = (chunk) => {
  const line = chunk.toString(); process.stderr.write(line);
  const match = line.match(/https:\/\/[-a-z0-9]+\.trycloudflare\.com/i);
  if (match && !announced) {
    announced = true;
    const url = match[0];
    setPublicBase(url);
    console.log(`\nT1 live tunnel: ${url}`);
    console.log(`OpenAPI schema (paste this into a private Custom GPT):\n${JSON.stringify({ openapi: "3.1.0", info: { title: "Host Reach POC", version: "1.0.0" }, servers: [{ url }], paths: { "/probe/{nonce}": { get: { operationId: "probeHostReach", parameters: [{ name: "nonce", in: "path", required: true, schema: { type: "string" } }], responses: { "200": { description: "Probe and absolute approval URL" } } } }, "/status/{nonce}": { get: { operationId: "getApprovalStatus", parameters: [{ name: "nonce", in: "path", required: true, schema: { type: "string" } }], responses: { "200": { description: "Mock status" } } } } } }, null, 2)}`);
    console.log(`\nAfter every GPT test, inspect ${url}/_log and save screenshots under poc/evidence/.`);
  }
};
tunnel.stdout.on("data", onLine); tunnel.stderr.on("data", onLine);
tunnel.on("error", async (error) => {
  console.error(`T1 cannot start cloudflared: ${error.message}`);
  await poc.close();
  process.exitCode = 1;
});
const stop = async () => { tunnel.kill("SIGTERM"); await poc.close(); process.exit(0); };
process.on("SIGINT", stop); process.on("SIGTERM", stop);
