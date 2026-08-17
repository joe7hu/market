const encoder = new TextEncoder();
const dbName = "network-custody-poc";
const storeName = "keys";
const user = "demo";
const apiOrigin = location.origin.includes(":8801") ? location.origin : "http://127.0.0.1:8801";

const b64url = (bytes) => {
  const data = new Uint8Array(bytes);
  let binary = "";
  for (const item of data) binary += String.fromCharCode(item);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
};
const fromB64url = (text) => Uint8Array.from(atob(text.replaceAll("-", "+").replaceAll("_", "/")), (char) => char.charCodeAt(0));
const sha256 = async (text) => new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(text)));
const fetchJson = async (path, init = {}) => {
  const response = await fetch(`${apiOrigin}${path}`, { ...init, headers: { "content-type": "application/json", ...(init.headers || {}) } });
  const payload = await response.json();
  if (!response.ok) throw Object.assign(new Error(payload.error || `http_${response.status}`), { code: payload.error, status: response.status, payload });
  return payload;
};

function openDb() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(dbName, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(storeName);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}
async function readKey() {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const request = db.transaction(storeName).objectStore(storeName).get(user);
    request.onsuccess = () => resolve(request.result || null);
    request.onerror = () => reject(request.error);
  });
}
async function writeKey(key) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const transaction = db.transaction(storeName, "readwrite");
    transaction.objectStore(storeName).put(key, user);
    transaction.oncomplete = resolve;
    transaction.onerror = () => reject(transaction.error);
  });
}
export async function clearLocalKey() {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const transaction = db.transaction(storeName, "readwrite");
    transaction.objectStore(storeName).clear();
    transaction.oncomplete = resolve;
    transaction.onerror = () => reject(transaction.error);
  });
}
async function pinMaterial(pin, saltText = "poc-pin-salt-v1") {
  const base = await crypto.subtle.importKey("raw", encoder.encode(pin), "PBKDF2", false, ["deriveKey", "deriveBits"]);
  const aesKey = await crypto.subtle.deriveKey({ name: "PBKDF2", salt: encoder.encode(saltText), iterations: 100_000, hash: "SHA-256" }, base, { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
  const proof = b64url(await crypto.subtle.deriveBits({ name: "PBKDF2", salt: encoder.encode(`${saltText}:proof`), iterations: 100_000, hash: "SHA-256" }, base, 256));
  return { aesKey, proof };
}
async function pinEnvelope(pin) {
  const { aesKey, proof } = await pinMaterial(pin);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, aesKey, encoder.encode("pin-approved"));
  return { proof, envelope: { iv: b64url(iv), ciphertext: b64url(ciphertext) } };
}
async function assertPin(pin, record) {
  const { aesKey, proof } = await pinMaterial(pin);
  await crypto.subtle.decrypt({ name: "AES-GCM", iv: fromB64url(record.envelope.iv) }, aesKey, fromB64url(record.envelope.ciphertext));
  return proof;
}

export async function enrol(pin) {
  const pair = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, false, ["sign", "verify"]);
  const publicKey = b64url(await crypto.subtle.exportKey("spki", pair.publicKey));
  const { proof, envelope } = await pinEnvelope(pin);
  await fetchJson("/enrol", { method: "POST", body: JSON.stringify({ user, publicKey, pinProof: proof }) });
  await writeKey({ privateKey: pair.privateKey, publicKey: pair.publicKey, envelope });
  return { publicKey };
}
export async function exportPrivateKey() {
  const record = await readKey();
  if (!record) throw new Error("key_missing");
  return crypto.subtle.exportKey("pkcs8", record.privateKey);
}
export async function attest(pin, { challengeOverride, actionIdOverride } = {}) {
  const record = await readKey();
  if (!record) throw Object.assign(new Error("key_missing"), { code: "key_missing" });
  const pinProof = await assertPin(pin, record);
  const challenge = challengeOverride || await fetchJson("/v1/action", { method: "POST", body: "{}" });
  const signature = await signChallenge(record.privateKey, challenge);
  const response = await fetchJson(`/v1/action/${encodeURIComponent(actionIdOverride || challenge.actionId)}/attest`, { method: "POST", body: JSON.stringify({ user, challengeId: challenge.challengeId, signature, pinProof }) });
  return { challenge, signature, response };
}
export async function issueAction() { return fetchJson("/v1/action", { method: "POST", body: "{}" }); }
async function signChallenge(privateKey, challenge) {
  const digest = await sha256(`${challenge.challengeId}${challenge.nonce}${challenge.canonicalAction}`);
  return b64url(await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, privateKey, digest));
}
async function rawAttest(actionId, payload) {
  const response = await fetch(`${apiOrigin}/v1/action/${encodeURIComponent(actionId)}/attest`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload) });
  return { status: response.status, payload: await response.json() };
}
export async function runT2Suite() {
  const checks = [];
  await clearLocalKey();
  await enrol("1234");
  const first = await attest("1234");
  checks.push({ test: "enrol_then_sign", ok: first.response.ok === true });
  try { await exportPrivateKey(); checks.push({ test: "private_export", ok: false, actual: "unexpected_success" }); }
  catch (error) { checks.push({ test: "private_export", ok: error.name === "InvalidAccessError", error: `${error.name}: ${error.message}` }); }
  const record = await readKey();
  const wrong = await pinMaterial("9999");
  const wrongChallenge = await issueAction();
  const wrongSignature = await signChallenge(record.privateKey, wrongChallenge);
  const wrongResult = await rawAttest(wrongChallenge.actionId, { user, challengeId: wrongChallenge.challengeId, signature: wrongSignature, pinProof: wrong.proof });
  checks.push({ test: "wrong_pin_server_rejects", ok: wrongResult.payload.error === "pin_invalid", error: wrongResult.payload.error });
  const replay = await rawAttest(first.challenge.actionId, { user, challengeId: first.challenge.challengeId, signature: first.signature, pinProof: (await pinMaterial("1234")).proof });
  checks.push({ test: "replay_rejected", ok: replay.payload.error === "challenge_already_used", error: replay.payload.error });
  const actionA = await issueAction();
  const actionB = await issueAction();
  const signatureA = await signChallenge(record.privateKey, actionA);
  const binding = await rawAttest(actionB.actionId, { user, challengeId: actionA.challengeId, signature: signatureA, pinProof: (await pinMaterial("1234")).proof });
  checks.push({ test: "canonical_action_bound", ok: binding.payload.error === "action_binding_mismatch", error: binding.payload.error });
  return { ok: checks.every((check) => check.ok), checks, browser: navigator.userAgent, origin: location.origin };
}

function render(result) {
  const pre = document.querySelector("#result");
  pre.textContent = JSON.stringify(result, null, 2);
  document.body.dataset.result = result.ok === false ? "fail" : "pass";
  window.__pocResult = result;
}
async function runScenario() {
  const scenario = new URLSearchParams(location.search).get("scenario");
  if (!scenario) return;
  try {
    if (scenario === "key-check") {
      const record = await readKey();
      return render({ ok: !record, expected: "key_missing", actual: record ? "key_present" : "key_missing", origin: location.origin });
    }
    if (scenario === "enrol-sign") {
      await clearLocalKey(); await enrol("1234");
      return render({ ok: true, ...(await attest("1234")), origin: location.origin });
    }
  } catch (error) { return render({ ok: false, error: error.code || error.message, origin: location.origin }); }
}
document.addEventListener("DOMContentLoaded", () => {
  document.querySelector("#enrol")?.addEventListener("click", async () => { try { render({ ok: true, ...(await enrol(document.querySelector("#pin").value)) }); } catch (error) { render({ ok: false, error: error.code || error.message }); } });
  document.querySelector("#attest")?.addEventListener("click", async () => { try { render({ ok: true, ...(await attest(document.querySelector("#pin").value)) }); } catch (error) { render({ ok: false, error: error.code || error.message }); } });
  document.querySelector("#suite")?.addEventListener("click", async () => { try { render(await runT2Suite()); } catch (error) { render({ ok: false, error: error.code || error.message }); } });
  runScenario();
});
