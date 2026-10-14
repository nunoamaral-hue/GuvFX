# GuvFX — Production Database Recovery Foundation

Packet `GUVFX_PRODUCTION_DATABASE_RECOVERY_FOUNDATION` (P0, DARK). Engineering + design only.
**No production infrastructure change, backup creation, restoration, PITR change, or deployment
was performed.** Production inspection was strictly READ-ONLY. All tooling is DARK, non-mutating
by default, fail-closed, and permits a non-production mutation ONLY when production is not matched,
a positive operator-planted isolation marker DB is present, and the live cluster's
`system_identifier` matches the operator-attested isolated cluster (authoritative identity, not a
caller label — and not `system_identifier` alone, which a physical clone shares); production is
refused.

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
  the tooling prints host/port/db/user but never the password or passphrase. The backup passphrase
  is fed to gpg on a dedicated fd (never argv).
- **No plaintext at rest (HIGH-3) — tool-wide:** an encrypted backup STREAMS `pg_dump -Fc | gpg` so a
  plaintext dump file is **never written to disk**; the ciphertext is written to a temp, a full
  decrypt-to-stdout integrity check runs, the temp is then **atomically renamed** to the final
  artifact, and only **then** is metadata written. A failed/interrupted encryption leaves at most a
  temp **ciphertext** fragment (removed by `trap`; even a fragment surviving SIGKILL/power-loss is
  encrypted, not plaintext) and **no metadata**. `verify.sh` decrypts to **stdout** (`→ /dev/null`,
  integrity via gpg MDC) and `restore.sh` STREAMS `gpg | pg_restore` — neither writes a decrypted
  temp. So confidentiality does **not** depend on best-effort plaintext shredding anywhere, and the
  `--verify-integrity` audit has no decrypt-to-/tmp footprint.
- **Restricted access & provenance:** scripts set `umask 077`, so artifacts + the metadata sidecar
  (timestamp, server/dump version, WAL LSN, sha256, bytes, git SHA, class) are created **owner-only
  (0600)**. Labels are validated (`[A-Za-z0-9_-]`), and the tool refuses to **overwrite** an existing
  recovery point. Integrity is `sha256` + decrypt + `pg_restore --list` TOC (verify.sh).
- **Authoritative target identity (HIGH-1/HIGH-4) — NOT an operator label, NOT `system_identifier`
  alone:** a non-production mutation is permitted ONLY when, in order, (1) the live cluster's
  `system_identifier` / database do **not** match the production denylist
  (`GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID` / `_DB`) — a deny wins first; (2) a **positive isolation
  marker database** is present (`GUVFX_RECOVERY_ISOLATION_MARKER_DB`, default
  `guvfx_recovery_isolated_ok`) — an operator-planted, cluster-scoped object that production
  **structurally lacks**; and (3) the live `system_identifier` (read from the connection; not
  forgeable via libpq env / `PGSERVICE`) matches the operator-attested
  `GUVFX_RECOVERY_ISOLATED_CLUSTER_ID`. The marker is required because `system_identifier` is
  **preserved by physical clones / `pg_basebackup` standbys / snapshots** — so a byte-for-byte copy
  of production shares prod's id, and identity-by-id alone would accept it. The marker is a
  *positive* signal the operator must deliberately create on a genuine scratch cluster; it
  distinguishes a clone-prepared-as-isolated from a bare production clone. A mislabelled
  `GUVFX_RECOVERY_TARGET_CLASS` (advisory only) can **never** override any of this; an unset /
  unverifiable identity or an absent marker **fails closed**. Restore into production is refused
  **categorically**; a production *backup* additionally needs the explicit **double-gate** (never set
  here).
- **Clean-target restore (HIGH-2):** restore goes ONLY into a freshly-created **empty** database
  (verified empty immediately before the atomic `--single-transaction` restore). There is **no
  `--force`/`--clean`** (which would leave unrelated pre-existing objects, so the result would not be
  attributable to one backup). Re-restore = drop + recreate the target (explicit operator lifecycle).
- **Capacity (MEDIUM-2):** preflight does not rely on an assumed compression ratio — it budgets a
  worst-case dump ~= the DB size and refuses if the backup would push the FS below a reserved free
  **floor** (`GUVFX_BACKUP_MIN_FREE_BYTES`, default 2 GiB), defending the 96%-full prod FS. A refusal
  leaves no artifact/metadata. Streaming means only one artifact is ever on disk (no plaintext+
  ciphertext peak).
- **Live-DB safety:** the (read-only) backup connection sets a bounded `lock_timeout`
  (`GUVFX_BACKUP_LOCK_TIMEOUT_MS`, default 60s) so a gated production backup errors rather than
  stalling the live trading DB on a lock.
- **Recovery-point audit (MEDIUM-1):** `audit_report.sh --verify-integrity --gate` runs verify.sh on
  each present point, so inventory presence is never conflated with integrity. It distinguishes
  PRESENT / FRESH / INTEGRITY_VERIFIED; RESTORE_TESTED is explicitly **not** established by the audit
  (it requires an actual isolated restore).
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

- `lib.sh` — safety core: production deny-first, positive isolation-marker requirement, authoritative
  `system_identifier` attestation, double-gate, broadened empty-target check, no-secret config,
  preflight helpers.
- `backup.sh` — STREAMED `pg_dump -Fc | gpg` AES256 (no plaintext on disk) → temp → integrity →
  atomic finalize → metadata; conservative capacity + reserved floor; dry-run default.
- `verify.sh` — integrity (sha256) + decrypt + `pg_restore --list` TOC (read-only; no DB write).
- `restore.sh` — atomic `--single-transaction` restore into a verified-EMPTY non-production target
  (no `--force`); production refused categorically; dry-run default.
- `audit_report.sh` — recovery-point observability; `--verify-integrity` + fail-closed `--gate`.
- `tests/certify.sh` — the isolated certification harness (Gate F/H), attestation-gated.

---

## Gate F — Isolated restore certification (evidence)

Run against isolated throwaway scratch databases on a local PostgreSQL with **synthetic data only**
(no production/app DB touched). Full evidence: `ops/recovery/tests/CERTIFICATION_EVIDENCE.txt`.

**Result: 41 / 41 PASS.** Exercised: backup (dry-run refusal + execute), integrity verify, restore
into a fresh empty DB, **row-count reconciliation across 9 tables**, **referential integrity**
(no orphans; FK constraints), **immutable-audit md5 match**, **encrypted STREAMING round-trip**
(+ assertion that NO plaintext/partial artifact exists; wrong-passphrase rejected), and the R1 +
renewed-review regressions: **HIGH-1** authoritative isolation (mutation refused when the attested
isolated cluster id ≠ live `system_identifier`, when no attestation is set, when the **isolation
marker DB is absent** — the clone-shares-prod-id defence — and when the live cluster/db matches the
production denylist; a mislabelled class cannot override); **HIGH-2** clean-target contract (non-empty
refused for an ordinary table, a matview-only target, a non-public-schema target, and a
view/sequence/function-bearing target; `--force` removed/rejected; re-restore only via drop+recreate);
**HIGH-3** backup/verify/restore no-plaintext (streamed round-trip; assertion no decrypted temp
exists); backup safety (invalid label refused; **non-integer reserved-floor refused**; reserved-floor
refusal leaves no artifact/metadata); **MEDIUM-1** audit `--verify-integrity --gate` fails on a
present-but-corrupt artifact and on an unparseable timestamp, and passes on a healthy dir; plus the
production/undeclared refusals.

**Not covered / untested production characteristics (stated):** synthetic data on a single local
**PG 17** server (Docker was unavailable here for a **PG 15** parity run — prod is PG 15; the backup
preflight enforces `pg_dump_major ≥ server_major`); not a production-representative dataset; no
off-host-transfer or PITR test. These are supplementary evidence, **not production parity** — a
PG 15/Linux certification against an ephemeral (non-production) cluster is a Sponsor-gated follow-up.

---

## Gate G — T1 deployment recovery integration

See `docs/T1_PRODUCTION_DARK_DEPLOYMENT_RUNBOOK.md` §B/§G/§H (updated) for the per-failure recovery
decision logic (migration fails pre-commit; app start fails; listener catch-up fails; faulty after
writes; schema reversal blocked by coexistence). Routine responses preserve committed data
(forward-fix / migration reverse); full-DB restoration is a separately-authorized last resort;
immutable ledger rows are never deleted to force an older uniqueness constraint.

---

## Gate I — Operational readiness milestones

These are DISTINCT milestones and must not be conflated (synthetic in-test passing is NOT production readiness):

| Milestone | Status | Basis |
|---|---|---|
| `ENGINEERING_READY` | **PASS** | tooling implemented; parses; dry-run + fail-closed behaviour verified |
| `TOOLING_CERTIFIED_IN_TESTS` | **PASS** | isolated certification 41/41 on synthetic data, local PG 17 (Gate F) |
| `RECOVERY_DESIGN_CERTIFIED` | **PASS** | design (B/C/D) + the isolated certification above |
| `PRODUCTION_BACKUP_ACTIVE` | **BLOCKED** | no scheduled backup, no off-host storage provisioned; gated by the 96% FS risk; Sponsor-gated |
| `PRODUCTION_RECOVERY_VERIFIED` | **BLOCKED** | restore demonstrated only in isolation with synthetic data on PG 17; never against a production-representative dataset or a PG 15 cluster |
| `T1_PRODUCTION_DARK_DEPLOYMENT_READY` | **BLOCKED** | gated by `PRODUCTION_BACKUP_ACTIVE` (a real off-host recovery point) + `PRODUCTION_RECOVERY_VERIFIED` + the FS-capacity risk |

**Remaining Sponsor decisions:** (1) authorize + provision off-host storage (NAS or object store);
(2) resolve the 96% FS (expand and/or prune stale on-host dumps under governance); (3) authorize an
automated backup schedule + its secret (passphrase) in the secret store; (4) decide whether a
sub-hour RPO (→ PITR) is required; (5) authorize OVH snapshots for host-loss DR; (6) optionally
authorize enabling `data_checksums` and an audit-retention policy. None are performed here.
