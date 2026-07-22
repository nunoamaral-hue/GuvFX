# Post-incident review — bridge agent-token leak and rotation (2026-07-22)

Programme learning, not blame. Every claim here is drawn from what was actually measured during the
incident and the maintenance window.

## 1. What happened

A **live** MT5 bridge agent token sat in plaintext in `docs/OPERATIONS_RUNBOOK.md` (5 places, commit
`67de147`) in a **public** GitHub repository. It was found incidentally while preparing B3P-2. It was
verified active, revoked by rotation on 2026-07-22 in a controlled window, and proven to return `401`.

## 2. Original assumptions

| # | Assumption | Held? |
|---|---|---|
| A1 | The bridge required authentication on all protected routes | **No** — see A2 |
| A2 | A missing credential would deny requests | **No** — it *allowed all* |
| A3 | Removing the literals from Git would reduce exposure | Partly — it does not revoke |
| A4 | A client `401` during rotation is a benign, retried condition | **No** |
| A5 | There were four bridge clients | **No** — five |
| A6 | The token existed in ~2 places on the hosts | **No** — 16 |
| A7 | Each service used its own credential | **No** |
| A8 | The deployed bridge matched the repo | **Yes** (verified byte-identical) |

## 3. Assumptions proven incorrect (and how)

- **A2 — the bridge failed OPEN.** `_validate_token()` ended with an allow-all fallback when no token was
  configured. Because one validator gates both `do_GET` and `do_POST`, an unconfigured bridge would have
  served `/mt5/order`, `/mt5/close-position` and `/mt5/modify-position` **unauthenticated**. This was the
  most serious finding of the whole exercise and it was *not* the reported bug — it was found while fixing
  the leak, and it made the rotation itself hazardous (a missing env var at restart = total bypass).
- **A4 — a `401` is not benign.** `_margin_guard_reason` converts any bridge failure into
  `margin_unverifiable`; `signal_promotion` then writes a **terminal `PROMOTION_REJECTED` with no replay
  path** — for exactly the live `ti_signals` sizing. The first rotation plan would have left clients 401ing
  for an unbounded period awaiting approval. An adversarial review caught this before execution.
- **A7 — credentials were silently coupled.** `mt5_validate_worker` resolved
  `MT5_WORKER_TOKEN or GUVFX_WORKER_TOKEN or GUVFX_AGENT_TOKEN` and had only the *agent* token. It had been
  authenticating with the **bridge's** credential for as long as the two values happened to be equal.
- **A6 — the blast radius was much larger than mapped.** 16 plaintext copies across both hosts, including
  three `.bat` launchers and `bridge_watchdog.ps1`, all readable by `BUILTIN\Users` — i.e. by
  `guvfx_u_{1,6,7}` and, in future, by every beta pool identity.

## 4. Issues discovered *only because of* the rotation

1. **Fail-open authentication** (above) — invisible while the env var happened to be set.
2. **The validate-worker credential coupling** — invisible while the two secrets were equal.
3. **`bridge_watchdog.ps1` held the token** and used it for its own health probe: after rotation it would
   have judged the bridge unhealthy and **restart-looped** it.
4. **Three launcher `.bat` files** (`guvfx_autostart.bat`, `guvfx_autostart_bridge_only.bat`,
   `start_signal_bridge_is6.bat`) each set the token inline. Deleting the lines without adding the
   `call` would have prevented the bridge starting at next logon and crash-looped it under the watchdog.
5. **A fifth bridge client** (`guvfx-mt5-shadow-worker`, `restart: unless-stopped`, up 13 days) that the
   first consumer map missed.
6. **World-readable secret files** — `Users:(RX)` on Windows; `664` on the VPS.
7. **`/mt5/supervision` is absent in production**, so supervision probes already report UNKNOWN. A
   pre-existing observability gap, surfaced while checking for drift.
8. **A non-ASCII credential produced a `TypeError` → 500** instead of `401` (`hmac.compare_digest` rejects
   non-ASCII `str`; headers decode as latin-1).
9. **Auth rejections were never logged**, so "no auth errors after the window" was unverifiable as written.

## 5. Governance improvements that worked

- **Staged increments + adversarial review before execution.** The security review returned **6 MUST_FIX**
  on the rotation plan, two of which would have damaged production: a rollback that would have
  **reinstated the leaked token**, and the benign-401 error. Both were fixed *on paper*, before touching
  anything.
- **Refusing a finding on evidence.** The same review claimed the deployed bridge carried an in-place
  `/mt5/supervision` patch that overwriting would delete. Direct measurement (0 matches; identical hashes)
  **refuted** it. Reviews are input, not instruction.
- **Deploy-to-disk before the window.** Merging and deploying the fail-closed code *without* restarting
  meant the single restart carried both the code and the new credential — one interruption, not two.
- **A hard abort criterion.** "Abort if a trade operation is in flight" made the go/no-go decision on
  measured state (0 positions, 0 in-flight ops), not judgement.
- **Digest-only verification.** Length + a 12-hex SHA-256 prefix proved cross-host agreement without the
  operator's secret ever entering chat, argv, logs or evidence.

## 6. What went wrong during execution (two self-inflicted)

- **The first restart used `Start-Process` over SSH.** The bridge came up correctly, held the listener for
  25 s, then died with the session. Caught immediately by the Step 7 probes (`000` on every request).
  → **Permanent Rule 1.**
- **Rotating the agent token broke the validate worker's heartbeat** (401s: 0 before, 10 after) because of
  the coupling in §3. Root-caused and fixed in-window by giving the worker its own `MT5_WORKER_TOKEN`.
  → **Permanent Rule 3**, and the WS1 code change.

Both were detected by the packet's own verification steps, which is the point of having them.

## 7. Future prevention

| Prevention | Status |
|---|---|
| Fail-closed auth + startup refusal | **Done** (merged `182374e`) |
| Constant-time, total (byte) credential comparison | **Done** |
| Auth-rejection logging so the proof is observable | **Done** |
| Secret scanner detects this credential shape | **Done** — verified against the real leaked commit, 5/5, no false positives |
| No cross-credential fallback anywhere | **Done** (WS1) |
| Startup self-validation incl. placeholder rejection | **Done** (WS3) |
| Canonical secret inventory to start every rotation from | **Done** (WS2) |
| Consolidate remaining inline compose secrets into `600` env files | **Open** (inventory §Gaps 1) |
| Rotate `MT5_WORKER_TOKEN` | **Open** — not leaked, but shares the hardened storage |
| Restore `/mt5/supervision` so probes stop reporting UNKNOWN | **Open** — pre-existing |
| Remove the stale `GUVFX_AGENT_TOKEN` from `backend/.env` | **Open** — inert |

## 8. The one-line lesson

**Rotation is a diagnostic.** Every latent coupling, fail-open path and forgotten copy of a credential
becomes visible the moment the value changes — which is precisely why rotations should be routine, planned,
and treated as architectural discovery rather than a chore.
