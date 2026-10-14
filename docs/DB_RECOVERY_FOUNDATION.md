# GuvFX — Production Database Recovery Foundation

Packet `GUVFX_PRODUCTION_DATABASE_RECOVERY_FOUNDATION` (P0, DARK). Engineering + design only.
**No production infrastructure change, backup creation, restoration, PITR change, or deployment
was performed.** Production inspection was strictly READ-ONLY. All tooling is DARK, non-mutating
by default, fail-closed, and refuses to act on a target **declared** (or **DSN-detected**, when the
optional production denylist is configured) as production.

> **GO / NO-GO for operational activation: NO-GO.** The recovery *design* is certified in
> isolation, but **no production recovery capability is active**: there is no off-host backup, no
> automated schedule, and no PITR. See §9 milestones. Activation is separately Sponsor-gated.

---

## Gate A — Verified production recovery inventory (read-only, 2026-10-14)

Captured live from `ubuntu@100.119.23.29` with read-only commands (no config change, no backup,
no data/secret copied; `docker inspect` Env deliberately not dumped).

- **Engine / topology:** PostgreSQL **15.15** (`postgres:15-alpine`) in container `guvfx-postgres`
  (role/db = `guvfx`), single OVH VPS (`vps-ab87f20f`, Linux 6.14). Data on a Docker **named
  volume** `guvfx-prod_guvfx-prod_guvfx_pg` → `/var/lib/postgresql/data`. restart=unless-stopped.
  (A second, unrelated `guac-db` postgres:15 serves Guacamole only.)
- **Database size:** `guvfx` = **2835 MB**. Composition is dominated by telemetry:
  `core_auditevent` **2237 MB (~79%, ~4.79M rows)**, `reliability_tradinghealthsnapshot` 441 MB,
  `execution_executionjob` 87 MB, `reliability_recoveryattempt` 26 MB. **The financial / trading /
  withdrawal / customer / provider-checkpoint tables are tiny (KB–MB).** → the *critical-data*
  recovery footprint is small; bulk growth is audit/health telemetry.
- **WAL / PITR:** `archive_mode=off`, `archive_command=(disabled)`, `archive_timeout=0`,
  `restore_command=` (empty), `wal_keep_size=0`, `wal_level=replica`. → **No PITR capability.**
- **Durability:** `fsync=on`, `full_page_writes=on`, `synchronous_commit=on` → committed
  transactions are durable to disk; a process/container crash loses **no committed data**.
- **Integrity:** `data_checksums=off` → silent on-disk corruption is **not** detected by Postgres.
- **Backups:** **no automated backup** (no backup cron; systemd timers are only the unrelated
  `guvfx-broker-ingest.timer` and OS `dpkg-db-backup.timer`). Only **ad-hoc `pg_dump` `.sql.gz`**
  artifacts — **104 files, ~1.7 GB** across `/home/ubuntu{,/backups,/db_backups,/guvfx-prod}`
  (newest `db_backups/guvfx_pre_is6_correction_20261012T201154Z.sql.gz`, 160 MB). All **on-host**.
- **Storage (headline risk):** root `/dev/sda1` **193 GB, 184 GB used, 9.2 GB free (96%)**. The DB
  volume **and** every backup share this one filesystem. There is **no separate or off-host
  storage**. A 2.8 GB+ logical restore on-host is marginal and the margin shrinks as the audit
  table grows.
- **Clients:** `pg_dump`/`pg_restore` **15.15** available in-container.
- **Service dependencies (write/read the DB):** `guvfx-backend`, workers (`mt5-shadow`,
  `mt5-node2-order`, `mt5-trade-ingest`, `mt5-validate`, `tp-protection-watcher`,
  `customer-notification`, `beta-provisioner`), `guvfx-wayond-listener` (sole signal-intake
  writer), `guvfx-frontend`, `traefik`. The listener is the acquisition writer that T1 deployment
  must quiesce (see the T1 runbook).

---

## Gate B — Recovery scenarios, RPO/RTO, and honest capability

RPO = max acceptable data loss; RTO = max acceptable downtime. **Zero-data-loss is NOT currently
available for disaster scenarios** — this is stated, not assumed. The design objective is to
preserve *committed trading and financial evidence*. Where that objective cannot be met today the
scenario is declared **BLOCKED** with the missing capability.

| Scenario | RPO objective | RTO objective | Current capability | Status |
|---|---|---|---|---|
| **Ordinary deployment rollback** (code only) | 0 (no DB change) | minutes | image rollback + `--force-recreate` | **MET** |
| **Failed migration, pre-commit** | 0 | minutes | migrations are transactional → auto-rollback | **MET** |
| **Faulty new code after writes** | 0 (keep committed data) | minutes–hours | forward-fix OR Django migration *reverse* (T1 runbook §G/§H) — NOT a full rewind | **MET (routine)**; full restore = last resort |
| **Schema-reversal blocked by genuine/non-genuine coexistence** | 0 (never delete evidence) | hours | forward-fix only; last-resort restore under full stop | **GOVERNED** (never drop immutable rows to force an old constraint) |
| **DB process/container crash** | 0 (fsync on) | minutes | restart container → Postgres crash recovery | **MET** |
| **Accidental logical data corruption** | since last backup — today **manual, hours–days** | ~restore time (minutes for 2.8 GB) | last logical backup (manual) | **PARTIAL** — RPO poor (no schedule) |
| **Host / storage / VPS loss** | **TOTAL** for anything not off-host | unbounded | **none** (all backups on-host) | **BLOCKED** → off-host storage |
| **Complete production DR** | **TOTAL** (no off-host, no PITR) | unbounded | **none** | **BLOCKED** → off-host + (optionally) PITR |

**Required capabilities to lift the BLOCKED rows:** off-host backup storage; an automated backup
schedule (shrinks the corruption-RPO); and — only if a sub-hour RPO for the critical tables is
required — PITR. Sponsor acceptance of any residual data-loss window is **not** assumed here.

---

## Gate C — Architecture options and recommendation

| Option | RPO | Restore reliability | Complexity | Storage | Fit |
|---|---|---|---|---|---|
| **A. Encrypted logical backup** (`pg_dump -Fc` + gpg) | = backup cadence | high; selective (`-L`/`-t`); cross-version tolerant | low | ~0.1–0.6× DB/backup | deploy rollback-point + corruption recovery |
| **B. Physical base backup + WAL archiving → PITR** | seconds–minutes | high but operationally heavy; whole-cluster | high | continuous WAL (off-host) | low-RPO DR |
| **C. OVH VPS / storage snapshot** | = snapshot cadence | crash-consistent whole-VM (Postgres crash-recovers given fsync/full_page_writes); off-VPS | low–medium (provider-managed) | provider-side | host/storage-loss DR |
| **D. Combination** | — | — | — | — | **recommended** |

**Recommendation — the smallest reliable production-grade solution (Option D):**

1. **Primary recovery point = encrypted logical backup (Option A)** via the certified DARK tooling
   (`ops/recovery/`): `pg_dump -Fc` + AES256, integrity-verified, with a recovery-point metadata
   sidecar. Taken **pre-deploy** *and* on an **automated schedule**, then **replicated OFF-HOST**
   (the single most important gap). This is the primary rollback/corruption recovery point.
2. **Routine deployment rollback = forward-fix / Django migration *reverse*** (T1 runbook §G/§H),
   **never** a full-database rewind — a rewind would discard unrelated trading and Withdrawal V1
   activity committed since the recovery point. The logical restore is the **last resort** only.
3. **Host/storage-loss DR = OVH automated snapshots (Option C)** — off-VPS, whole-VM, low-ops,
   cheap; the smallest reliable DR layer. (Design only; not provisioned here.)
4. **PITR (Option B) is NOT recommended as the immediate foundation.** Its continuous-WAL
   complexity and off-host-WAL requirement are not justified by the tiny critical-data footprint,
   and the 96%-full FS is a hard blocker. Revisit only if a sub-hour RPO for financial tables is
   formally required.

**Prerequisite operational risks (surfaced, not actioned here):**
- **P0 — root FS at 96% (9.2 GB free).** A full filesystem halts Postgres writes (an availability
  incident) and makes on-host backup/restore unsafe. Needs capacity expansion and/or governed
  pruning of the ~1.7 GB of stale on-host dumps. **This gates any on-host backup activation.**
- **Off-host storage is unprovisioned.** Candidate: the existing Synology NAS (Tailscale peer
  `100.73.130.98`, SMB auth-gated) or OVH Object Storage. Design in §Gate D; provisioning is
  Sponsor-gated.
- **`data_checksums=off`.** Enable (needs dump/restore or `pg_checksums` with downtime) so
  corruption is detectable — hardening, Sponsor-gated.
- **Audit-table growth (2.2 GB / 79%).** A retention/archival policy (separate packet) would bound
  DB and backup size. Immutable evidence (data.md) is **archived off-DB, never deleted**.

---

## Gate D — Backup isolation and security contract

- **Encryption at rest:** AES256 (`gpg --symmetric`). Passphrase from `GUVFX_BACKUP_PASSPHRASE[_FILE]`
  (secret store / env), **never** hard-coded or logged, **never** on argv. Production-class backups
  **must** be encrypted (the tool refuses `--no-encrypt` for a production target).
- **No secrets in Git / scripts / logs:** DB auth via standard libpq (`PGPASSWORD`/`~/.pgpass`);
  the tooling prints host/port/db/user but never the password or passphrase. A transient plaintext
  dump (pre-encryption) and any decrypted temp are shredded on **every** exit path (`trap EXIT INT
  TERM`), so a failed/interrupted encryption never leaves plaintext at rest. (`shred` is best-effort
  on copy-on-write / overlay / tmpfs filesystems — encryption + restricted storage is the primary
  confidentiality control, not erasure.)
- **Restricted access & provenance:** scripts set `umask 077`, so artifacts + the metadata sidecar
  (timestamp, server/dump version, WAL LSN, sha256, bytes, git SHA, class) are created **owner-only
  (0600)**; integrity is `sha256` + a `pg_restore --list` TOC check on verify.
- **Production protection (honest model):** the guard keys primarily on the operator-declared
  `GUVFX_RECOVERY_TARGET_CLASS` — **declaring the class correctly is an operator responsibility.** As
  a configured backstop against a *mislabelled* class, set `GUVFX_RECOVERY_PRODUCTION_HOST` /
  `GUVFX_RECOVERY_PRODUCTION_DB` in the production operator environment: if the live DSN matches them
  while the declared class is not `production`, every mutating op **fails closed**. restore into a
  production target is refused **categorically**; a non-empty target is refused without `--force`;
  a production *backup* needs an explicit **double-gate** (never set in this packet). The tool does
  not auto-detect production from an unconfigured DSN — hence the operator-declaration + optional
  denylist model above.
- **Live-DB safety:** the (read-only) backup connection sets a bounded `lock_timeout`
  (`GUVFX_BACKUP_LOCK_TIMEOUT_MS`, default 60s) so a gated production backup errors rather than
  stalling the live trading DB on a lock.
- **Capacity monitoring:** `audit_report.sh --gate` fails closed on no recovery point, staleness
  beyond a threshold, an unencrypted production point, a broken point (missing artifact), or an
  orphan artifact.
- **Retention & cleanup:** documented policy (e.g. keep N daily + all pre-deploy points off-host;
  prune on-host copies once replicated). Cleanup is operator-run and governed; **immutable evidence
  is never deleted to reclaim space.**
- **On-host is INSUFFICIENT:** an on-host backup cannot protect against host/storage loss. **Off-host
  replication is mandatory** for the DR objective; it is *designed* here, not *provisioned* (no
  production data is uploaded/transferred under this packet).

---

## Gate E — DARK recovery tooling (`ops/recovery/`)

All scripts are **non-mutating by default** (dry-run), **fail-closed**, and never target production
without the explicit gates above. See `ops/recovery/README.md` for the operator contract.

- `lib.sh` — safety core: target-class gate, production guard, no-secret config, preflight helpers.
- `backup.sh` — consistent `pg_dump -Fc` + optional AES256 + recovery-point metadata (dry-run default).
- `verify.sh` — integrity (sha256) + decrypt test + `pg_restore --list` TOC (read-only; no DB write).
- `restore.sh` — restore into a NON-production target (dry-run default; atomic `--single-transaction`;
  production refused categorically).
- `audit_report.sh` — recovery-point observability + fail-closed `--gate`.
- `tests/certify.sh` — the isolated certification harness (Gate F/H).

---

## Gate F — Isolated restore certification (evidence)

Run against isolated throwaway scratch databases on a local PostgreSQL with **synthetic data only**
(no production/app DB touched). Full evidence: `ops/recovery/tests/CERTIFICATION_EVIDENCE.txt`.

**Result: 35 / 35 PASS.** Exercised: backup (dry-run refusal + execute), integrity verify,
restore into a fresh DB, **row-count reconciliation across 9 tables** (customer, trading_account,
trade, execution_job, acquired_message, provider_checkpoint, broker_event, withdrawal_history,
audit_event), **referential integrity** (no orphans; FK constraints restored), **immutable-audit
content preserved (md5 match)**, **encrypted round-trip** (backup→encrypt→verify-decrypt→restore→
reconcile; wrong-passphrase rejected), and the **Gate H fail-closed matrix** (undeclared class;
production-backup double-gate; production-restore categorical refusal; non-empty-target refusal;
idempotent `--force` re-restore; corrupt/truncated-artifact rejection; missing-metadata rejection).

**Not covered (stated):** certification used synthetic data on a single local PG 17 instance; it is
**not** a production-representative dataset, not cross-major-version (prod is PG 15; the preflight
enforces `pg_dump_major ≥ server_major`), and not a test of off-host transfer or PITR.

---

## Gate G — T1 deployment recovery integration

See `docs/T1_PRODUCTION_DARK_DEPLOYMENT_RUNBOOK.md` §B/§G/§H (updated) for the per-failure recovery
decision logic (migration fails pre-commit; app start fails; listener catch-up fails; faulty after
writes; schema reversal blocked by coexistence). Routine responses preserve committed data
(forward-fix / migration reverse); full-DB restoration is a separately-authorized last resort;
immutable ledger rows are never deleted to force an older uniqueness constraint.

---

## Gate I — Operational readiness milestones

| Milestone | Status | Basis |
|---|---|---|
| `ENGINEERING_READY` | **PASS** | tooling implemented; parses; dry-run + fail-closed behaviour verified |
| `RECOVERY_DESIGN_CERTIFIED` | **PASS** | design (B/C/D) + isolated certification 35/35 (Gate F) |
| `PRODUCTION_BACKUP_ACTIVE` | **BLOCKED** | no scheduled backup, no off-host storage provisioned; gated by the 96% FS risk; Sponsor-gated |
| `PRODUCTION_RESTORE_TESTED` | **BLOCKED** | certified only in isolation with synthetic data; not against a production-representative dataset / prod-equivalent infra |
| `T1_PRODUCTION_DARK_DEPLOYMENT_READY` | **BLOCKED** | gated by `PRODUCTION_BACKUP_ACTIVE` (a real off-host recovery point) + the FS-capacity risk |

**Remaining Sponsor decisions:** (1) authorize + provision off-host storage (NAS or object store);
(2) resolve the 96% FS (expand and/or prune stale on-host dumps under governance); (3) authorize an
automated backup schedule + its secret (passphrase) in the secret store; (4) decide whether a
sub-hour RPO (→ PITR) is required; (5) authorize OVH snapshots for host-loss DR; (6) optionally
authorize enabling `data_checksums` and an audit-retention policy. None are performed here.
