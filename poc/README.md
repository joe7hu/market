# Network, Cross-Origin, and Key-Custody POC

The POC is isolated from Market. It has three independent servers and does not
use wallet, Circle, paid-provider, or browser-account credentials.

## Start local T2 and T3

```sh
node poc/poc.mjs
```

- T2 primary origin: `http://127.0.0.1:8801/custody.html`
- T3 page origin: `http://127.0.0.1:8802/t3.html?scenario=A`
- T2 second port: `http://127.0.0.1:8803/custody.html?scenario=key-check`
- T2 hostname separation: `http://localhost:8801/custody.html?scenario=key-check`

The private key is created with `extractable: false` and persisted directly as a
`CryptoKey` in IndexedDB. A PIN is used only as a mock signing gate: PBKDF2
derives an AES-GCM key that decrypts a local PIN verifier; the service also
checks a derived proof. The private key is not exported or reimplemented as
real wallet cryptography.

## T2 test sequence

1. On `127.0.0.1:8801`, enrol with PIN `1234`, then sign a fresh challenge.
2. Click **Run T2 protocol suite**. It records the expected `InvalidAccessError` when it calls `exportKey` on the non-extractable private key, plus the wrong-PIN, replay, and action-binding negative protocol tests.
3. Reload the primary origin, then sign again: the existing IndexedDB `CryptoKey` is used.
4. Open both second-origin URLs above: each must show `key_missing`.
5. Open the primary URL in a private window or a separate browser profile: it must show `key_missing`.
6. Enter a wrong PIN and sign: the result must be `pin_invalid` (or local AES-GCM decrypt failure before network submission).
7. Re-submit the recorded challenge/signature pair: it must return `challenge_already_used`.
8. Issue challenges A and B; submit A to B's attestation URL: it must return `action_binding_mismatch`.

## T3 test sequence

Open the page URL with each scenario:

- `?scenario=A` — JSON POST. It must reject after a preflight `OPTIONS` only.
- `?scenario=A-simple` — `text/plain` POST. It must reject in the browser but
  the API log must show the POST reached the API.
- `?scenario=B` — API echoes the page origin and permits `content-type`; it succeeds.
- `?scenario=C` — page server forwards to API; it succeeds with no API CORS headers.

Inspect the independent server record at `http://127.0.0.1:8801/_log`. Capture
browser-console CORS messages along with it. A CORS rejection is not proof that
a request never reached the server.

## T1 manual Actions test

With no VPN, Node 18+, `cloudflared`, and a personal paid ChatGPT account:

```sh
node poc/t1.mjs
```

It prints the live quick-tunnel OpenAPI schema. Paste it into a private Custom
GPT, call `probeHostReach` with a unique nonce, and record the builder result,
domain permission prompt, ChatGPT output, and `/_log`. The response contains an
absolute `approvalUrl`, which is the top-level-handoff test. Do not test this
with an enterprise workspace: its domain allowlist is a separate known gate.

Save evidence as `poc/evidence/t1-evidence.json`, `t2-evidence.json`, and
`t3-evidence.json`. The `poc/evidence/` directory is gitignored because it can
contain IP addresses, user-agent strings, screenshots, and manual ChatGPT
results.
