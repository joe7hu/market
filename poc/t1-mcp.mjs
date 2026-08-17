#!/usr/bin/env node
/** Starts the read-only native regular-chat MCP POC behind a quick tunnel. */
import { spawn } from "node:child_process";
import { setPublicBase, startPoc } from "./poc.mjs";

const apiPort = Number(process.env.POC_MCP_PORT || 8787);
const poc = await startPoc({ apiPort, pagePort: apiPort + 1, secondPort: apiPort + 2 });
const tunnel = spawn("cloudflared", ["tunnel", "--url", `http://127.0.0.1:${apiPort}`], { stdio: ["ignore", "pipe", "pipe"] });
let announced = false;
const onLine = (chunk) => {
  const line = chunk.toString();
  process.stderr.write(line);
  const match = line.match(/https:\/\/[-a-z0-9]+\.trycloudflare\.com/i);
  if (!match || announced) return;
  announced = true;
  setPublicBase(match[0]);
  console.log(`\nNative regular-chat MCP endpoint: ${match[0]}/mcp`);
  console.log("In ChatGPT web: Settings → Apps → Advanced Settings → Developer mode; Apps → Create; use this endpoint; choose no authentication; Scan Tools; Create the draft app.");
  console.log("In a new ordinary chat, select the draft app from the + tools menu and ask: Use poc_probe_host_reach with nonce t1-mcp-20260817-a. Then inspect /_log.");
};
tunnel.stdout.on("data", onLine);
tunnel.stderr.on("data", onLine);
tunnel.on("error", async (error) => { console.error(`MCP tunnel could not start: ${error.message}`); await poc.close(); process.exitCode = 1; });
const stop = async () => { tunnel.kill("SIGTERM"); await poc.close(); process.exit(0); };
process.on("SIGINT", stop);
process.on("SIGTERM", stop);
