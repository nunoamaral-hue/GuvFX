# Broker-validation timeout chain

Scope: the synchronous customer "Test connection" / "Retry validation" request, end to end. This documents
every timeout between the browser and the broker and shows they are consistent with the **maximum supported
validation duration**. Produced for the validation-UX packet (PR #288). Non-secret.

## The single invariant

For a synchronous request that legitimately runs up to the login window, each layer's *wait-for-result*
budget must be **greater than or equal to** the layer beneath it, so the customer receives the broker's
**real** result instead of an intermediate transport timeout:

```
browser fetch  ≥  reverse proxy (response wait)  ≥  gunicorn worker  ≥  backend→agent read
               ≥  agent result-wait floor        ≥  MT5 login + cleanup
```

## Measured / configured values

| # | Layer | Setting | Value | What it bounds | Source (verified) |
|---|-------|---------|-------|----------------|-------------------|
| 1 | Browser | `fetch()` (no `AbortController`) | none (∞ until socket closes) | client wait | `frontend/src/lib/api.ts` |
| 2 | Traefik (reverse proxy) | `respondingTimeouts.readTimeout` | 60s (v3 default) | reading the **request** (tiny POST body → n/a) | `docker inspect traefik` — no override flag; empirically the request survived to layer 4's 120s kill |
| 3 | Traefik | `respondingTimeouts.writeTimeout` | **0 = unlimited** (default) | writing the **response** (the long wait) | as above |
| 4 | Traefik | `forwardingTimeouts.responseHeaderTimeout` | **0 = unlimited** (default) | waiting for backend response headers | as above |
| 5 | Traefik | `forwardingTimeouts.dialTimeout` | 30s (default) | connecting to backend | as above |
| 6 | gunicorn | `--timeout` | **190s** (was 120s) | kills a worker whose request runs longer | `backend/Dockerfile` |
| 7 | Django view | — | none (synchronous) | — | `trading/views.py bc_test_connection` |
| 8 | Backend → agent | `OP_TRANSPORT_TIMEOUTS["VALIDATE_LOGIN"]` (read) | **175s** | HTTP read of the agent's reply | `terminal_provisioning/beta_worker.py` |
| 9 | Backend → agent | `CONNECT_TIMEOUT` | 10s | TCP connect to agent | `beta_worker.py` |
| 10 | Agent | result-wait floor | **165s** (login 120 + cleanup 45) | agent waits for the MT5 runner | `beta_worker.VALIDATE_LOGIN_AGENT_WAIT_FLOOR_S` (backend half); agent enforces its own half |
| 11 | MT5 runner | login window | 120s | broker login | agent config (ADR-0027 Phase 2) |
| 12 | MT5 runner | cleanup grace | 45s | terminal teardown | agent config |

## Consistency check

```
browser(∞) ≥ Traefik write/responseHeader(∞) ≥ gunicorn(190) ≥ backend read(175) ≥ agent floor(165) ≥ MT5(120+45=165)
                                                       190  >  175  >  165  ≥  165   ✓
```

- **gunicorn 190 > backend-read 175** — the worker is no longer killed before the backend's own read
  timeout fires. This is the root-cause fix for the customer-visible "Failed to fetch": at 120s gunicorn was
  killing the worker mid-`requests.post`, resetting the connection, and the browser surfaced the raw
  `TypeError: "Failed to fetch"`. Enforced at import by `assert_backend_timeout_contract` and guarded by
  `backend/terminal_provisioning/tests_gunicorn_timeout_contract.py` (reads the Dockerfile).
- **backend-read 175 > agent-floor 165** — the backend receives the runner's real result instead of a
  transport timeout (`validation_runner_timeout`). Enforced by the same assert + the runtime override floor.
- **agent-floor 165 ≥ MT5 120+45** — the agent waits long enough for a full login+cleanup.

## Reverse proxy: why no change is required (and the one latent item)

Traefik 3.6.4 runs with **no** `respondingTimeouts`/`forwardingTimeouts` overrides (verified by
`docker inspect traefik` — the command line sets entrypoints, ACME, docker provider and logging only). On
defaults:

- `writeTimeout = 0` and `forwardingTimeouts.responseHeaderTimeout = 0` — both **unlimited** — so the proxy
  does **not** cap the time spent waiting for / streaming the backend's response. A 175s backend response is
  not cut off.
- `readTimeout = 60s` bounds reading the **request** only. The POST body is a few hundred bytes sent
  immediately, so it never approaches 60s. **Positive control:** the observed failure occurred at gunicorn's
  120s kill, i.e. the request demonstrably lived well past 60s — so `readTimeout` does not abort these calls
  (per RULE 11, this is a verified positive result, not an assumption about the default's behaviour).

**Conclusion — the reverse proxy needs no change for this fix.** Deploying the fix is therefore a
backend-image rebuild (gunicorn 190) + a frontend rebuild (UX + safe errors) only; Traefik is untouched.

**Optional future hardening (NOT required, NOT in this packet):** make the https entrypoint's timeouts
explicit — e.g. `--entrypoints.https.transport.respondingTimeouts.readTimeout=300s` and `.writeTimeout=0` —
so the budget is stated rather than inherited from framework defaults. That edit lives in the Traefik stack
(outside this repo) and would be a separate, Sponsor-gated infrastructure change.
