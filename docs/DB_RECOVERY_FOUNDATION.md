# GuvFX — Production Database Recovery Foundation

Packet `GUVFX_PRODUCTION_DATABASE_RECOVERY_FOUNDATION` (P0, DARK). Engineering + design only.
**No production infrastructure change, backup creation, restoration, PITR change, or deployment
was performed.** Production inspection was strictly READ-ONLY. All tooling is DARK, non-mutating
by default, fail-closed, and permits a non-production mutation ONLY when production is not matched,
a positive operator-planted isolation marker DB is present, and the live cluster's
`system_identifier` matches the operator-attested isolated cluster (authoritative identity, not a
caller label — and not `system_identifier` alone, which a physical clone shares). A destructive
**restore** additionally requires the specific target **database** to be independently authorized
(explicit allowlist, non-admin/non-production, owned by a least-privilege non-superuser role) and
verified empty — cluster isolation alone does not authorize it. Production is refused.

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
- **Authoritative target identity — CLUSTER scope (HIGH-1/HIGH-4) — NOT an operator label, NOT `system_identifier`
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
- **Restore-target authority — DATABASE scope (R2 HIGH-2) — cluster isolation is necessary, NOT
  sufficient:** a valid cluster marker proves the *cluster* is isolated; it does not authorize
  destroying *every* database on it (a cluster may hold application DBs beside scratch ones). So
  `restore.sh` additionally requires, via `rec_require_authorized_target_db` (before and again
  immediately before the atomic restore): (a) `PGDATABASE` set and `current_database()` == `PGDATABASE`
  (no silent redirection); (b) the target in an **explicit exact allowlist**
  (`GUVFX_RECOVERY_ALLOWED_TARGET_DB`) — a class label is *not* authorization; (c) target **not** an
  admin/template DB (`postgres`/`template0|1`/common cloud-admin names) and **not**
  `GUVFX_RECOVERY_PRODUCTION_DB`; (d) a **least-privilege, non-superuser** connecting role that **owns**
  the target (a superuser has implicit rights over every DB → refused). A verified-empty DB that is
  not *also* positively authorized is refused. Unknown/unverified ⇒ fail closed.
- **Independent certification-harness isolation (R2 HIGH-1) — the destructive harness cannot
  manufacture its own isolation evidence:** the destructive harness (`tests/certify.sh`) connects **as a
  least-privilege non-superuser role** and calls `attest_independent_test_cluster`
  (`tests/attest_isolated.sh`) **before any mutation**: it requires a **mandatory** production denylist,
  an operator-attested isolated cluster id that **equals** the live `system_identifier` (never derived
  from the connection), a **pre-existing marker it did not create**, a non-superuser role, and an
  explicit loopback `PGHOST`; **localhost is not proof**. The harness never creates the marker or
  derives the authorized id, and cleanup drops **only databases it owns**.
- **Privileged provisioning trust boundary — DEDICATED EPHEMERAL CLUSTER (R3 HIGH):** the privileged
  provisioning that creates the harness role + marker was moved off any shared cluster entirely.
  `tests/setup_isolated_cluster.sh --up` **`initdb`s its own disposable cluster** (private data dir
  under a controlled root, private unix socket, `listen_addresses=` so **no TCP**). Disposability is
  therefore **intrinsic** — the cluster is brand-new (a fresh `system_identifier` sharing nothing with
  any existing/production cluster) and we created every byte of it. The **only** `CREATE ROLE` /
  `CREATE DATABASE` run inside that fresh cluster; the provisioner performs **no DDL against any shared
  cluster**. `--down` stops that postmaster, **verifies it is actually stopped (`pg_ctl status`) before
  any `rm`** — refusing and preserving the data dir + ownership stamp + state (non-zero exit) if
  shutdown is unconfirmed, so a live cluster is never deleted (R3 CLOSEOUT MEDIUM-B) — then `rm`s
  **only the files we created**, gated by an ownership stamp whose token must match the recorded state
  and by the data-dir's own `system_identifier` (via offline `pg_controldata`). The **same trust
  boundary** applies to setup and teardown: `GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID` is **mandatory on
  every privileged path**, libpq / `PGSERVICE` / `PGHOSTADDR` are **neutralised** (connections use an
  explicit private socket), an unidentified / unstamped / mismatched target is refused, and a
  failed/interrupted setup cleans up only the ephemeral paths it created (and likewise leaves a
  still-running cluster in place rather than deleting it). A placeholder production id authorizes
  nothing — authorization is the positive ownership of a cluster we `initdb`'d, never an id comparison.
- **Clean-target restore (HIGH-2):** restore goes ONLY into a freshly-created **empty** database
  (verified empty immediately before the atomic `--single-transaction` restore, which also detects a
  writer that populated it between preflight and execution). There is **no `--force`/`--clean`** (which
  would leave unrelated pre-existing objects, so the result would not be attributable to one backup).
  Re-restore = drop + recreate the target (explicit operator lifecycle).
- **Capacity (MEDIUM-2 + R2 §6) — estimate for sanity, `ulimit` for enforcement:** the preflight
  figure (DB size + 100 MiB) is a **conservative ESTIMATE, explicitly not a guaranteed maximum** (an
  incompressible or concurrently-growing DB can exceed it); it does not rely on a compression ratio.
  Enforcement is a hard runtime **output ceiling** applied with `ulimit -f` (512-byte blocks) set to
  `free − reserved_floor` (optionally capped lower by `GUVFX_BACKUP_MAX_ARTIFACT_BYTES`): if the dump
  would drive the FS below the reserved **floor** (`GUVFX_BACKUP_MIN_FREE_BYTES`, default 2 GiB,
  defending the 96%-full prod FS) the write fails like ENOSPC (SIGXFSZ), the trap removes the partial,
  and **no metadata is written** — an incomplete artifact is never certified. Non-integer env refused.
  Streaming means only one artifact is ever on disk (no plaintext+ciphertext peak).
- **Live-DB safety:** the (read-only) backup connection sets a bounded `lock_timeout`
  (`GUVFX_BACKUP_LOCK_TIMEOUT_MS`, default 60s) so a gated production backup errors rather than
  stalling the live trading DB on a lock.
- **Encrypted archive verification (R2 MEDIUM-1):** `verify.sh` streams an encrypted artifact through
  `gpg --decrypt | pg_restore --list` (no plaintext file) and **rejects content that decrypts but is
  not a non-empty PostgreSQL custom-format archive** — successful decryption alone is not verification.
  sha256 is checked first. So encrypted non-PostgreSQL / truncated content fails `verify.sh` and the
  usable-recovery audit gate.
- **Two distinct audit gates (R2 MEDIUM-2) — never conflated:** `audit_report.sh --gate` is
  **INVENTORY-ONLY** (presence / freshness / encryption-class / orphan) and explicitly does **not**
  certify a usable recovery point (a present+fresh+corrupt artifact passes it). `--gate-usable` is the
  **USABLE-RECOVERY** gate: it implies per-artifact integrity verification and **fails closed** on any
  present-but-corrupt artifact; the operating runbook requires it for recovery readiness. The audit
  distinguishes PRESENT / FRESH / INTEGRITY_VERIFIED; RESTORE_TESTED is explicitly **not** established
  by either gate (it requires an actual isolated restore).
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
  `system_identifier` cluster attestation, **per-database restore-target authorization**
  (`rec_require_authorized_target_db`: allowlist + ownership + non-superuser + admin/prod denials),
  double-gate, broadened empty-target check, no-secret config, preflight helpers.
- `backup.sh` — STREAMED `pg_dump -Fc | gpg` AES256 (no plaintext on disk) → temp → integrity →
  atomic finalize → metadata; conservative capacity estimate + **enforceable `ulimit -f` output
  ceiling** above the reserved floor; dry-run default.
- `verify.sh` — integrity (sha256) + **streamed `gpg | pg_restore --list` TOC for encrypted artifacts**
  (validates archive format, not just decryptability; no plaintext temp); read-only, no DB write.
- `restore.sh` — atomic `--single-transaction` restore into a verified-EMPTY, **positively-authorized**
  non-production target (no `--force`); production refused categorically; dry-run default.
- `audit_report.sh` — recovery-point observability; **`--gate` (inventory-only)** vs **`--gate-usable`
  (inventory + mandatory integrity, fail-closed on corruption)**.
- `tests/attest_isolated.sh` — the independent isolated-cluster attestation guard (R2 HIGH-1).
- `tests/setup_isolated_cluster.sh` — **privileged** provisioner that **`initdb`s a dedicated
  disposable cluster** (R3): private data dir/socket, no TCP, least-privilege role + marker inside the
  fresh cluster; `--down` **verifies the postmaster stopped before any `rm`** (R3 CLOSEOUT MEDIUM-B;
  refuses + preserves evidence otherwise) and removes only the files it created. No DDL against a
  shared cluster.
- `tests/eph_tracking.sh` — sourced helper: subshell-safe (file-based) ephemeral-root tracking + cleanup
  (R3 CLOSEOUT MEDIUM-A), used by the provisioner and cleanup certifications.
- `tests/certify_provisioner.sh` — the provisioner trust-boundary certification (R3; 18 cases) — setup/
  teardown symmetry, production-selection refusal, connection-redirection neutralisation, verified-
  shutdown teardown, cleanup safety.
- `tests/certify_cleanup.sh` — the harness cleanup-tracking certification (R3 CLOSEOUT; 11 cases) —
  proves tracking survives a subshell and the trap cleans an abnormal mid-suite exit.
- `tests/certify.sh` — the isolated recovery certification harness (Gate F/H; 64 cases), run **as the
  least-privilege role** and gated on independent attestation.

---

## Gate F — Isolated restore certification (evidence)

Run **as a least-privilege non-superuser role** against a **dedicated ephemeral cluster** (`initdb`'d
by `tests/setup_isolated_cluster.sh`; private socket, no TCP), with **synthetic data only** (no
production/app DB touched). Full evidence: `ops/recovery/tests/CERTIFICATION_EVIDENCE.txt`.

**Combined result: 93 / 93 PASS** = **recovery certification 64/64** (`tests/certify.sh`) **+ privileged-
provisioner trust-boundary certification 18/18** (`tests/certify_provisioner.sh`) **+ harness
cleanup-tracking certification 11/11** (`tests/certify_cleanup.sh`, R3 CLOSEOUT+FINAL). The provisioner suite
proves: verified disposable-cluster setup + teardown succeed; the production id is required on setup AND
teardown; setup refuses to adopt/clobber a pre-existing cluster; teardown refuses an identity mismatch,
an unstamped/unknown dir, a stamp-token/state mismatch, and a production-denylist match (before any
destruction); hostile `PGHOST`/`PGHOSTADDR`/`PGPORT`/`PGSERVICE`/`PGUSER` are neutralised; teardown
leaves the main cluster's databases and roles untouched; a failed setup performs no unsafe cleanup; the
recovery harness still passes on an ephemeral cluster (test 13); and **(R3 CLOSEOUT MEDIUM-B)** teardown
REFUSES to delete when the postmaster is not confirmed stopped (data dir + stamp + state preserved,
non-zero exit) while a subsequent real teardown succeeds. The cleanup suite proves **(R3 CLOSEOUT
MEDIUM-A)** that ephemeral-root tracking survives the command-substitution subshell and the EXIT trap
cleans up an abnormal mid-suite termination — stopping the owned postmaster, removing the temp dir, and
leaving the main cluster and unrelated directories untouched — and, **(FINAL)**, is NON-VACUOUS: it
requires POSITIVE startup evidence (a confirmed-running postmaster + matching sysid, recorded before the
abort) before certifying any cleanup, with a negative test that a forced startup failure records
`running=0` (never a fake success); and the cleanup suite's OWN teardown uses the shared
`eph_safe_remove_root` contract (stop + verify before `rm`; a cluster whose shutdown is unconfirmed is
preserved, with safe retry), tested with a failed-shutdown-preserves + retry-succeeds pair.

**Recovery certification — 64 / 64 PASS.** Exercised: backup (dry-run refusal + execute), integrity verify, restore
into a fresh empty DB, **row-count reconciliation across 9 tables**, **referential integrity**
(no orphans; FK constraints), **immutable-audit md5 match**, **encrypted STREAMING round-trip**
(+ assertion that NO plaintext/partial artifact exists; wrong-passphrase rejected), and the R1 + R2
regressions:
- **R2 HIGH-1 independent harness isolation** — attestation refuses a production-like local cluster,
  an absent production denylist, self-attestation against the wrong cluster, a missing attestation, a
  non-loopback `PGHOST`, a missing attest flag, an **absent pre-existing marker** (harness must not
  create it), and a **superuser** connecting role — each with the **precise** refusal reason;
- **R2 HIGH-2 restore-target DB authorization** — refuses when no allowlist is set, an admin DB
  (`postgres`), an unknown (non-allowlisted) target, a **non-owned** DB (the operator marker), and a
  `GUVFX_RECOVERY_PRODUCTION_DB` match even if allowlisted; and cleanup **cannot** drop a non-owned DB;
- **HIGH-2 clean-target** — non-empty refused for an ordinary table, matview-only, non-public-schema,
  and view/sequence/function-bearing targets; `--force` rejected; re-restore only via drop+recreate;
  a target **populated between preflight and execute** is detected and the restore aborts;
- **R2 MEDIUM-1 encrypted format verification** — `verify.sh` and `audit --gate-usable` reject an
  encrypted **non-PostgreSQL** artifact (valid sha256 + correct passphrase, decrypts fine, not an archive);
- **R2 MEDIUM-2 gate semantics** — a present+fresh+corrupt artifact **passes `--gate` (inventory)** but
  **fails `--gate-usable`**; `--verify-integrity --gate` fails on a corrupt artifact; unparseable
  timestamp fails closed; healthy dir passes `--gate-usable`;
- **§6 capacity/ENOSPC** — a dump exceeding the enforceable `ulimit -f` ceiling fails safely leaving no
  artifact/partial/metadata; non-integer reserved-floor and `GUVFX_BACKUP_MAX_ARTIFACT_BYTES` refused;
- plus HIGH-3 no-plaintext assertions and the production/undeclared refusals.

**Renewed adversarial review (R2):** a 54-agent find→skeptic-verify workflow over the changed files
produced 48 raw findings, 8 confirmed. All were fixed and re-certified: the `ulimit -f` unit (bash uses
**1024-byte** blocks, not 512 — the ceiling was 2× too large) → divide by 1024 (+ a positive/negative
capacity control in the harness, RULE 11); `--gate-usable` asserted "fresh" without evaluating
freshness → it now **requires `--max-age-hours`** and the PASS message claims only what ran;
`--max-age-hours` was unvalidated (non-integer silently skipped the staleness check, fail-open) → now
refused; the restore allowlist split was subject to **CWD globbing** → pathname expansion disabled; and
the encrypted `verify.sh` discarded gpg's decrypt/MDC exit status (a tail-corrupt/truncated archive with
a matching sha could pass once `pg_restore --list` read the front TOC) → a full decrypt-integrity pass
whose status is **enforced** now precedes the format pass.

**Not covered / untested production characteristics (stated):** synthetic data on a single local
**PG 17** server (Docker was unavailable here for a **PG 15** parity run — prod is PG 15; the backup
preflight enforces `pg_dump_major ≥ server_major`); not a production-representative dataset; no
off-host-transfer or PITR test; `RECOVERY_POINT_RESTORE_TESTED` against production data is never
established. These are supplementary engineering evidence, **not production parity** — a PG 15/Linux
certification against an ephemeral (non-production) cluster is a Sponsor-gated follow-up.

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
| `TOOLING_CERTIFIED_IN_TESTS` | **PASS** | isolated certification 93/93 (64 recovery + 18 provisioner + 11 cleanup) on synthetic data, local PG 17, least-privilege role (Gate F) |
| `PRIVILEGED_PROVISIONING_CERTIFIED` | **PASS** | dedicated ephemeral-cluster provisioner; no DDL against a shared cluster; setup/teardown symmetric; verified-shutdown teardown + subshell-safe cleanup tracking; 18/18 + 11/11 (Gate F) |
| `RECOVERY_DESIGN_CERTIFIED` | **PASS** | design (B/C/D) + the isolated certification above |
| `PRODUCTION_BACKUP_ACTIVE` | **BLOCKED** | no scheduled backup, no off-host storage provisioned; gated by the 96% FS risk; Sponsor-gated |
| `PRODUCTION_RECOVERY_VERIFIED` | **BLOCKED** | restore demonstrated only in isolation with synthetic data on PG 17; never against a production-representative dataset or a PG 15 cluster |
| `T1_PRODUCTION_DARK_DEPLOYMENT_READY` | **BLOCKED** | gated by `PRODUCTION_BACKUP_ACTIVE` (a real off-host recovery point) + `PRODUCTION_RECOVERY_VERIFIED` + the FS-capacity risk |

**Remaining Sponsor decisions:** (1) authorize + provision off-host storage (NAS or object store);
(2) resolve the 96% FS (expand and/or prune stale on-host dumps under governance); (3) authorize an
automated backup schedule + its secret (passphrase) in the secret store; (4) decide whether a
sub-hour RPO (→ PITR) is required; (5) authorize OVH snapshots for host-loss DR; (6) optionally
authorize enabling `data_checksums` and an audit-retention policy. None are performed here.
