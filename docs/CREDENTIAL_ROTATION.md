# Credential Rotation Framework (GFX-PKT-SEC-CREDENTIAL-ROTATION)

Last pre-E3 security must-fix. **Repo-only implementation + documentation.** No
production credential was rotated, no secret or `.env` content was read/printed
producing this document — only credential *surfaces* (code references, env-var
names, settings) were reviewed. Production rotation of any prod secret is a
**Nuno-held** action (he holds the credentials / prod access).

## 1. Secret inventory (redacted categories — values never recorded)

| # | Secret (category) | Where consumed | Rotation owner | Prior exposure |
|---|---|---|---|---|
| S1 | Postgres DB password | backend `DB_PASSWORD` (compose env) | Nuno (prod) | **exposed** (compose inline) |
| S2 | Django `DJANGO_SECRET_KEY` / `JWT_SECRET_KEY` | backend env | Nuno | S key exposed |
| S3 | MT5/Windows agent token (`GUVFX_WINDOWS_AGENT_TOKEN`) | worker→bridge `X-GuvFX-Agent-Token` | Nuno | **exposed** |
| S4 | Worker token (`MT5_WORKER_TOKEN`) + `WorkerIdentity` secrets | `execution/auth.py` (hashed) | ops + Nuno | — |
| S5 | Shadow-worker token (`MT5_SHADOW_WORKER_TOKEN`) | `deploy/shadow-worker/` (`.env`, 600) | ops | — |
| S6 | MT5 credential Fernet key (`GUVFX_FERNET_KEY`) | encrypts `TradingAccount` creds | Nuno | **exposed** |
| S7 | Guacamole admin/DB passwords, MT5 desktop password | Guacamole stack | Nuno | **exposed** (2nd incident) |

> "exposed" = flagged in a prior incident (compose stored secrets inline; two
> exposure incidents recorded in the programme notes). **All exposed secrets
> (S1, S2-key, S3, S6, S7) require rotation before E3** — Nuno-held.

## 2. Worker-token rotation (S4/S5) — zero-downtime

`WorkerIdentity` stores a SHA-256 hash + ACTIVE/REVOKED status, so **dual-identity
overlap** gives zero-downtime rotation:

1. Provision a NEW identity (new secret) — `provision_shadow_worker` (or the
   generic pattern): create with the new hash, ACTIVE. Old identity stays ACTIVE.
2. Roll the worker container onto the new `MT5_*_WORKER_TOKEN` + `MT5_*_WORKER_ID`.
3. Confirm the worker authenticates on the new identity (0 401s).
4. **Revoke** the old identity (`--revoke` → status REVOKED). Overlap window closed.

Every step emits a **`CREDENTIAL_CREATED` / `CREDENTIAL_ROTATED` / `CREDENTIAL_REVOKED`**
audit (item 8). The secret is supplied via env only and never logged.

## 3. MT5 bridge / agent token rotation (S3)

The agent token (`X-GuvFX-Agent-Token`) is a shared symmetric secret between the
worker (client) and the bridge (server, on the Windows box). Rotation is a
**coordinated 2-sided** change (no dual-token support today): (a) generate new
token, (b) update the bridge env + restart the bridge, (c) update the worker env +
recreate the worker — both from the deploy `.env` (600). Brief bridge downtime
window; schedule off-market. Improvement (deferred): teach the bridge to accept
`{old, new}` during an overlap window for zero-downtime (mirrors S4).

## 4. Fernet / encrypted-credential key rotation (S6)

`GUVFX_FERNET_KEY` encrypts stored `TradingAccount` broker credentials. Rotate via
**`MultiFernet`** key-versioning (never a hard swap that orphans ciphertext):
1. Set `GUVFX_FERNET_KEYS = new,old` (new first for encryption, old still decrypts).
2. Run a re-encrypt pass over stored credentials (decrypt-old → encrypt-new).
3. Drop `old` from the list once the pass completes.

Requires a small `EncryptedField`/settings change to read a **list** of keys — a
scoped follow-up packet (`SEC-FERNET-MULTIKEY`). Until then, a Fernet rotation is a
re-encrypt-with-downtime operation; do not hard-swap the key.

## 5. Legacy `X-Worker-Token` reduction / disablement plan (item 5)

**Current state:** `ENABLE_LEGACY_WORKER_TOKEN` defaults **`true`** in
`settings.py`; prod workers already use `X-Worker-Id` + `X-Worker-Secret` (the
modern path) and do **not** set `GUVFX_USE_LEGACY_AUTH`. So legacy auth is
enabled-but-unused.

**Plan (do NOT silently flip the default — verify first):**
1. Audit `WORKER_AUTH_*` audit events / access logs for any request that
   authenticated via the legacy `X-Worker-Token` path over a representative window.
2. If none: set `ENABLE_LEGACY_WORKER_TOKEN=false` in the prod deploy env (a
   deploy step, not a repo default change — flipping the code default could break
   a hypothetical legacy consumer on next deploy).
3. After a soak period with 0 legacy auths, remove the legacy path in a scoped
   code packet (`SEC-LEGACY-AUTH-REMOVAL`).

The repo default is left `true` (behaviour-preserving); the reduction is a
verified, staged operational change.

## 6. Emergency revoke process

- **Worker/shadow identity:** `provision_shadow_worker --revoke` (or set the
  `WorkerIdentity` REVOKED) → next_job/complete reject it immediately; emits
  `CREDENTIAL_REVOKED`.
- **All order flow (kill):** engage `ExecutionControl.kill_switch_engaged` /
  `GUVFX_EXECUTION_DISABLED=1` — stops every order-opening job (unified kill
  switch, Blueprint 06).
- **Agent/DB/Fernet/Guacamole:** rotate per §3/§1/§4/§7 (Nuno-held) + restart the
  affected service; revoke Telegram sessions per the intake runbook if relevant.

## 7. Leak incident playbook

On suspected exposure of any secret:
1. **Stop** — do not commit; do not print the secret; capture only redacted
   detail (file/category), per `.claude/rules/security.md`.
2. **Contain** — engage the kill switch if order-flow creds may be affected;
   revoke the exposed identity/session immediately.
3. **Rotate** — new secret via the relevant §2–§4 procedure (Nuno-held for prod).
4. **Audit** — confirm `CREDENTIAL_REVOKED`/`ROTATED` events recorded; scan git
   history (`scripts/check_no_secrets.py`) to confirm no committed secret.
5. **Report** — redacted summary to Nuno with the rotation evidence.
6. **Review** — how it leaked; tighten (env_file / secret store; the prod compose
   still stores some secrets inline — move to `env_file`, tracked follow-up).

## 8. Audit trail for credential lifecycle (implemented)

New `core.audit.log_credential_event(action, entity_type, entity_id, actor, **detail)`
emits an append-only `AuditEvent` (`CREDENTIAL_CREATED/ROTATED/REVOKED`) with the
secret-sanitising metadata guard — **no secret value is ever passed**. Wired into
`provision_shadow_worker` (create → CREATED, re-provision → ROTATED, `--revoke` →
REVOKED). Reusable for future worker/agent-token rotations.

## 9. Zero / minimal-downtime rotation summary

| Secret | Downtime | Mechanism |
|---|---|---|
| Worker / shadow token (S4/S5) | **Zero** | dual-identity overlap (§2) |
| Agent token (S3) | Minimal (bridge restart, off-market) | coordinated 2-sided (§3) |
| Fernet key (S6) | Minimal→Zero (after MultiFernet packet) | re-encrypt pass (§4) |
| DB / Django / JWT (S1/S2) | Service restart | env update + recreate |

## 10. Production actions still requiring Nuno

- Rotate all **exposed** prod secrets (S1, S2-key, S3, S6, S7) — Nuno holds prod
  access / the secret store.
- Set `ENABLE_LEGACY_WORKER_TOKEN=false` after the §5 audit.
- Deploy the `MultiFernet` change + run the re-encrypt pass (S6).
- Move inline compose secrets to an `env_file` / secret store.

None of the above is done in this repo-only packet; they are the recorded
deploy/ops steps gated on Nuno's credentials.
