# WV1 / ADR-0054 F-8 — Production-prep execution return (Stage-8 packet, 2026-10-23)

**Authorized work executed:** docs-only PR + governed merge; fresh production recovery point (read-only dump,
streamed encryption, off-host); read-only production manifest; restored-copy migration rehearsal; audit-control
investigation. **Production role/ownership/migration cutover: NO-GO (unchanged).** **Erasure: OFF / UNPROVISIONED.**
**Return status: `WV1_RUNTIME_ROLE_CHANGES_REQUIRED`** — pgAudit is unavailable in production, which by the PD's
own condition 7 mandates CHANGES REQUIRED; a **tested** alternative is supplied below (an unavailable control is
not claimed). All forward mutation happened ONLY on disposable containers.

## 1. Documentation PR + merge evidence
PR **#557** (`docs/wv1-runtime-role-prod-prep` → `main`), docs-only: 4 files, **+384 / −0**
(`WV1_RUNTIME_ROLE_{TRUNCATE_DECISION,PRODUCTION_CUTOVER_PLAN,PRODUCTION_PREP_RETURN}_2026-10-23.md` +
`docs/architecture/ADR-0054-CONTROLLED-ERASURE.md` amendment). CI **5/5 green** on head `fdb37114`
(backend/frontend/governance/market-data-foundation/research-foundation). **0 unresolved review threads.**
No secrets/production values. **Auto-merge OFF.** Governed squash-merge → **MERGED** 2026-10-23T11:33:40Z.

## 2. Current canonical-main SHA
`fb69adad9d0b8a660b1d8c158afcb8f194eba273` (squash of #557; parent was `3586b540`; F-8 `75e5469a` remains in
history). No dashboard/MT5/Copy-Trader content absorbed.

## 3. Fresh recovery-point certification
| Field | Value |
|---|---|
| Artifact | `guvfx_prodrecovery_20261023T113706Z.dump.gpg` |
| NAS path (off-host) | `/Volumes/Backups/Backups/GuvFX/production/database-recovery/2026-10-23/` (SMB, ~10 TiB free, owner-only) |
| Bytes | 238,029,667 |
| SHA-256 | `bfc3bd7470f13dbe9b8dfe23d8d38ab2d45272f74bd03a7df8957a4d43c4034c` (== sidecar) |
| Encryption | gpg symmetric **AES256**, passphrase on fd (never argv/env/log/chat) |
| Format / parity | `pg_dump -Fc -Z6`, **pg_dump 15.19 == server 15.15 (client_server_major_parity: true)**, 1451 TOC entries |
| Source | DB `guvfx` @ sysid `7620331966590783522`, server 15.15, WAL LSN `2A/970B31E0`, source 4,104,680,239 B |
| Tooling | `ops/recovery/backup.sh` (production double-gate fired), `git_sha fdb37114`, `0.1.0-dark` |
| Verify | `verify.sh`: sha256 OK + AES256 MDC OK + PostgreSQL archive TOC OK (1451) → **VERIFY PASSED** |
| Restore-cert | restored into disposable PG15 (sysid `7699826844207898668` ≠ prod — isolation confirmed): 137 user tables, evidence tables present; representative counts consistent (`trading_trade`=2277 static vs the 2026-10-22 cert; `core_auditevent`=7,260,548 monotone up from 7.08M) |

**Off-host:** streamed `pg_dump | gpg` directly to the NAS via an SSH local-forward from the operator host to the
DB container's compose-network address (the container is not host-published); **no plaintext or ciphertext written
on the VPS**; production services not
stopped or mutated; the tunnel + all secret temp files were shredded by a trap. New no-overwrite timestamped path.

## 4. Reviewed production manifest + digest (read-only)
Captured via `cutover.sh --capture` (read-only; `SET default_transaction_read_only`). **272 objects = 137 tables
+ 130 sequences + 5 functions** (0 views/matviews) — matches the live inventory exactly. **270 application / 2
protected.** The 2 protected rows are exactly `broker_intelligence_evidenceblob` and `broker_intelligence_brokerevent`.
**Every object owned by bootstrap `guvfx`; all in `public`.** No missing / unexpected / differently-owned objects.
**Reviewed SHA-256 `1ffe99ac920238d5cdc7d188218888f5d753f39520d979054aa089cd2997d539`** (recorded; independently
recomputed-equal).

## 5. Named-reviewer status — **UNMET (production-cutover prerequisite)**
The agent does not count as an independent human approver. **Two named human reviewers must review the manifest +
digest before it is used for a production cutover.** None recorded yet → this is an open prerequisite.

## 6. Migration-by-migration risk table 0011→0022
Method: a per-migration classification workflow with adversarial verification, cross-checked against the
empirical Phase-4 apply on the restored prod copy. Plan contained **exactly** `broker_intelligence` 0011→0022 —
**no cross-app dependencies** pulled.

| Migration | Class | Static risk | Empirical apply (restored copy) | Notes |
|---|---|---|---|---|
| 0011 consecutive_failures | schema-only (6 AddField + inline CHECK≥0) | low | ~1185 ms* | metadata-only defaults; no scan |
| 0012 tradingaccountbrokercontact | CreateModel×2 + FKs/indexes | low (lock) | ~1354 ms* | brief FK SHARE-ROW-EXCLUSIVE on hot parents (users, trading_tradingaccount) — quiet window + `lock_timeout` |
| 0013 brokermessageobservation | CreateModel + 4 FKs | low (lock) | ~1034 ms* | FK locks on brokerevent/evidenceblob; redundant index nit on rfc822_message_id |
| 0014 owner_scoped_dedup | AddField + AlterField + AddConstraint×2 | low (lock) | ~1435 ms* | new unique is strictly weaker; metadata-only |
| 0015 c3_association_safeguards | schema + constraints | low | ~1145 ms* | — |
| 0016 withdrawal_owner_attribution | AddField+AlterField+AddIndex+AddConstraint×2 | low | ~1382 ms* | small table; no rewrite |
| 0017 erasurehold_erasurerequest | CreateModel×2 + **RunSQL (DDL)** | **medium** | ~1086 ms* | the only data/DDL `RunSQL`; new tables, low data risk |
| 0018 erasure_lifecycle_outbox | schema + constraints | low | ~1435 ms* | — |
| 0019 erasurerequest_consistency | constraints | low | ~1148 ms* | — |
| 0020 audit_anonymisation_carveout | trigger/constraint, ownership-sensitive | low | ~1070 ms* | — |
| 0021 erasure_execution_grant | AddField + CreateModel | low | ~1146 ms* | — |
| 0022 connectedmailbox_generation | schema-only | low | ~1095 ms* | — |

*timings include ~1 s Django boot per invocation; actual DDL is sub-100 ms on the small broker_intelligence
tables. **No table rewrites, no non-CONCURRENT index on a large table, no RunPython backfill.** Post-apply leaf =
`0022`; **every new `broker_intelligence_*` table owned by `guvfx_schema_owner`** (F-8 ownership held). Overall:
**11 low / 1 medium**, the medium being 0017's `RunSQL`. Material cutover action: short `lock_timeout` + quiet
window for 0012/0013/0014; confirm F-8 runtime grants (default privileges auto-apply — confirmed in rehearsal).

## 7. Restored-copy rehearsal — timings + results
On a prod-faithful disposable PG15 restored from the §3 artifact (bootstrap `guvfx` owns all, at bi 0010):
- **Separation** (`cutover.sh --execute`, manifest SHA matched `1ffe99ac…`): rc=0 → `public` owned by
  `guvfx_schema_owner`, **app owns 0**.
- **Migrations 0011→0022** applied as `guvfx_migrate` (auto `SET ROLE guvfx_schema_owner`): all rc=0, leaf 0022,
  all new tables owned by `schema_owner` (timings in §6).
- **Erasure provisioning** + `verify_erasure_privilege` → both rc=0, **VERIFY OK** (flag OFF).
- **Runtime smoke as `guvfx_app`:** `manage.py check` OK; WV1 ORM reads OK (users=8, evidence=10, events=3,
  mailbox=1, observations=0).
- **Evidence immutability:** app SELECT OK; **DELETE and TRUNCATE on both evidence tables DENIED**; app cannot
  `SET ROLE guvfx_schema_owner`.
- **Migration-credential isolation:** runtime connected as `guvfx_app`; `guvfx_migrate` used only for the migrate
  step — never carried by the runtime identity.
- **Rollback from every boundary (clean, correct order):** erasure teardown (`production_rollback_erasure.sql`)
  rc=0 → proc gone, ownership boundary preserved; then separation reversal (`cutover.sh --rollback`) rc=0 →
  **return-to-start: ownership fully reversed to bootstrap `guvfx`, app owns 0, `public` owner restored**. _(The
  first pass surfaced the fail-closed ordering guard: the separation rollback refuses while erasure is still
  provisioned — correct.)_
- **Return-to-start determination (step 13):** the **role separation is fully reversible**; the **0011→0022
  schema migrations are NOT auto-reversible** — returning to the exact certified 0010 starting state requires
  **restore-from-backup** (the §3 recovery point now exists for exactly this).

## 8. Audit-control availability — **pgAudit UNAVAILABLE → CHANGES REQUIRED (tested alternative supplied)**
Read-only prod inspection: **no `pgaudit` in `pg_available_extensions`** (image `postgres:15-alpine` does not
bundle it), `shared_preload_libraries` **empty**, `log_statement=none`, `logging_collector=off`, `log_destination=stderr`.
So pgAudit cannot capture TRUNCATE/DROP/DELETE on protected evidence today.

**Tested alternative (proven on disposable PG15, `scratchpad/audit/audit_alt.sh`):**
1. `ALTER SYSTEM SET log_statement='mod'` + reload (no restart) **captured DELETE + TRUNCATE + DROP** on the
   evidence tables in the server log; `log_statement` is **SUSET**, so `guvfx_migrate` **cannot** suppress it
   (both session `SET` and `ALTER SYSTEM` denied) — non-bypassable by the audited identity.
2. A **bootstrap-owned SECURITY DEFINER event trigger on `sql_drop`** records every DROP (actor = `session_user`,
   correct even through `SET ROLE`); `guvfx_migrate` **cannot DROP or DISABLE** it.
3. Caveat: `ALTER ROLE … PASSWORD` is also logged under `'mod'`, so the cutover's existing
   `SET LOCAL log_statement='none'` wrapper remains required (and `guvfx_migrate` never sets passwords).

**Why this is a blocker, not a done control:** enabling the alternative in production is itself a production
configuration change (`ALTER SYSTEM`/reload) + durable off-host log shipping + retention/access controls — all of
which are **hard-stopped in this packet**. It must be done + verified in a **separate Sponsor-gated step before
cutover** (disposition condition 7).

## 9. Credential-custody readiness
- **Backup passphrase:** approved Keychain source confirmed (`login` keychain, service `GuvFX Production Backup`,
  account `guvfx-backup`); retrieved into a `0600` temp file via the native approval dialog, **never printed**;
  shredded on exit.
- **Prod DB credential:** read from the container env into a `0600` `.pgpass` over SSH, **never printed**, shredded.
- **Runtime/migration credentials (future cutover):** mechanism proven — SCRAM **verifiers** minted in a secret
  store, env-only into psql (`\getenv`/`docker exec -e`), two-person custody, revoked/rotated after the window
  (interim-decision conditions). Rehearsal used disposable throwaway verifiers. **Production credential custody
  (secret store + two-person) is an operational cutover-window step, not executed here.**

## 10. Exact remaining blockers (production cutover)
1. **Audit control (condition 7):** pgAudit unavailable → enable + verify the tested alternative in prod via a
   separate Sponsor-gated config step (reload + off-host log shipping + retention). **Open.**
2. **Named human reviewers (×2)** of the manifest + digest `1ffe99ac…`. **Open** (agent cannot stand in).
3. **Per-stage production GO** for every mutating stage (recovery already done; then separation → app-scoped
   migration → erasure provisioning → config cutover), each a separate Sponsor authorization. **Not authorized here.**
4. **Interim-decision enforcement:** migrate-credential custody + per-use recording + rotation; acceptance
   **expires before GA**; **Option D′** is the required stronger target before general release.

## 11. Updated completion percentages
- F-8 runtime-role separation (engineering/review/**restored-copy** rehearsal): **~97%**, merged DARK; **production
  operability 0% / NO-GO**.
- TRUNCATE governance: interim **ACCEPT recorded + merged**; D′ target recorded. Decision **done**; operational
  enforcement pending.
- Fresh recovery point: **100%** (created, verified, restore-certified, off-host).
- Production manifest: capture + machine review + digest **done**; **human-reviewer sign-off 0%**.
- Migration rehearsal (restored copy): **done** (all pass).
- Audit control in production: **0%** (unavailable); tested alternative **ready**.
- **Overall production-cutover readiness: BLOCKED** (audit control + 2 human reviewers + per-stage GO).

## 12. Confirmation — production data + configuration unchanged
Production received **only**: read-only catalog/manifest inspection and read-only baseline counts (read-only
transactions), the read-only pgAudit investigation, and the **authorized read-only logical dump** (streamed
off-host; nothing written to the VPS; services not stopped or mutated). **No** production role / membership /
ownership / grant / password / configuration / migration / deployment / restart / feature-flag / erasure-provisioning
/ erasure-execution / OAuth change. **MT5, dashboard, and Copy-Trader untouched.** `guac-db` untouched. Every
forward separation / migration / erasure / rollback ran **only on disposable containers** (`guvfx_restore`,
`guvfx_restore2`, and the audit/privilege test containers), all removed.

---
**RETURN STATUS: `WV1_RUNTIME_ROLE_CHANGES_REQUIRED`**
(pgAudit unavailable in production per condition 7; tested alternative supplied; plus the unmet two-human-reviewer
prerequisite. The authorized prep — docs merge, fresh verified off-host recovery point, read-only manifest,
restored-copy rehearsal — all completed successfully. Production mutation remains NO-GO pending the blockers in §10.)
