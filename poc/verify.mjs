#!/usr/bin/env node
/** Protocol-level verifier. Browser-only assertions are run by poc/browser-verify.mjs. */
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { setPublicBase, startPoc } from "./poc.mjs";

const poc = await startPoc();
const base = "http://127.0.0.1:8801";
const post = async (path, body) => {
  const response = await fetch(`${base}${path}`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  return [response.status, await response.json()];
};
try {
  setPublicBase("https://live-poc.example.test");
  const schema = await (await fetch(`${base}/openapi.json`)).json();
  assert.equal(schema.openapi, "3.1.0");
  assert.ok(schema.paths["/probe/{nonce}"]);
  assert.equal(schema.servers[0].url, "https://live-poc.example.test");
  const probe = await (await fetch(`${base}/probe/verification-nonce`)).json();
  assert.equal(probe.approvalUrl, "https://live-poc.example.test/approve/verification-nonce");
  const keyPair = await webcrypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, false, ["sign", "verify"]);
  const publicKey = Buffer.from(await webcrypto.subtle.exportKey("spki", keyPair.publicKey)).toString("base64url");
  const [enrolStatus] = await post("/enrol", { user: "verifier", publicKey, pinProof: "correct-pin-proof" });
  assert.equal(enrolStatus, 201);
  const [challengeStatus, challenge] = await post("/v1/action", {});
  assert.equal(challengeStatus, 201);
  const digest = await webcrypto.subtle.digest("SHA-256", new TextEncoder().encode(`${challenge.challengeId}${challenge.nonce}${challenge.canonicalAction}`));
  const signature = Buffer.from(await webcrypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, keyPair.privateKey, digest)).toString("base64url");
  const [attestStatus, attestation] = await post(`/v1/action/${challenge.actionId}/attest`, { user: "verifier", challengeId: challenge.challengeId, signature, pinProof: "correct-pin-proof" });
  assert.equal(attestStatus, 200); assert.equal(attestation.state, "attested");
  const [replayStatus, replay] = await post(`/v1/action/${challenge.actionId}/attest`, { user: "verifier", challengeId: challenge.challengeId, signature, pinProof: "correct-pin-proof" });
  assert.equal(replayStatus, 409); assert.equal(replay.error, "challenge_already_used");
  const [, unsignedChallenge] = await post("/v1/action", {});
  const [missingStatus, missing] = await post(`/v1/action/${unsignedChallenge.actionId}/attest`, { challengeId: unsignedChallenge.challengeId, signature: "x", pinProof: "x" });
  assert.equal(missingStatus, 400); assert.equal(missing.error, "not_enrolled");
  const apiLog = await (await fetch(`${base}/_log`)).json();
  assert.ok(apiLog.events.some((item) => item.kind === "t2_challenge"));
  console.log(JSON.stringify({ ok: true, checks: ["live OpenAPI base", "probe absolute URL", "ECDSA attestation", "replay rejection", "not-enrolled rejection", "server log"] }));
} finally { await poc.close(); }
