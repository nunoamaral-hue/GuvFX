# GuvFX recovery tooling (`ops/recovery/`) — DARK

Reproducible, **fail-closed**, **non-mutating-by-default** PostgreSQL backup/restore tooling for the
GuvFX recovery foundation. Design + rationale: [`docs/DB_RECOVERY_FOUNDATION.md`](../../docs/DB_RECOVERY_FOUNDATION.md).

> **Status: DARK.** This tooling is *not* wired to any scheduler and performs *no* production action
> on its own. Production backup/restore is separately Sponsor-gated. These scripts default to
> dry-run and refuse to mutate a production target.

## Safety model (read before use)

- **Declare the target class.** `GUVFX_RECOVERY_TARGET_CLASS` must be `dev`, `test`, or `production`.
  Unset ⇒ the tool refuses to run (fail-closed).
- **Dry-run by default.** `backup.sh` and `restore.sh` only print the plan + preflight unless you
  pass `--execute`.
- **Authoritative target identity — CLUSTER scope (NOT a caller label, NOT `system_identifier` alone).** A
  non-production mutation is permitted ONLY when, in order: (1) the live cluster/db do **not** match
  `GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID` / `GUVFX_RECOVERY_PRODUCTION_DB` (deny wins first); (2) a
  **positive isolation marker database** is present — `GUVFX_RECOVERY_ISOLATION_MARKER_DB` (default
  `guvfx_recovery_isolated_ok`), an operator-planted DB that production structurally lacks; and (3)
  the live `system_identifier` matches the operator-attested `GUVFX_RECOVERY_ISOLATED_CLUSTER_ID`.
  The marker is required because `system_identifier` is **preserved by physical clones / standbys /
  snapshots** — so a byte-for-byte prod clone shares prod's id, and id alone would accept it. The
  marker is a deliberate positive signal you create only on a genuine scratch cluster.
  `GUVFX_RECOVERY_TARGET_CLASS` is advisory and can never override this; an unset/unverifiable
  identity or an **absent marker** fails closed. (Read your isolated cluster's id once with
  `psql -tAqc 'select system_identifier from pg_control_system()'` and export it; create the marker
  with `createdb guvfx_recovery_isolated_ok` on that cluster.)
  - A **backup** of a `production` target is refused unless **both** `GUVFX_RECOVERY_ALLOW_PRODUCTION=1`
    **and** `--i-understand-production` (this double-gate is intentionally *not* used in the repo).
  - A **restore** into `production` is refused **categorically** — the tool never restores into
    production. A production restore is a separate, manually-governed operation.
- **Restore-target authority — DATABASE scope (cluster isolation is necessary, not sufficient).** A
  destructive restore ALSO requires `GUVFX_RECOVERY_ALLOWED_TARGET_DB` to list the exact target DB,
  `current_database()` to equal `PGDATABASE`, the target to be neither an admin/template DB nor
  `GUVFX_RECOVERY_PRODUCTION_DB`, and the connecting role to be **non-superuser and the owner** of the
  target. A verified-empty DB that is not also positively authorized is refused; a class label is not
  authorization. (This is re-checked immediately before the atomic restore.)
- **Clean-target restore (no `--force`).** Restore goes ONLY into a freshly-created **empty** database
  (verified empty immediately before the atomic restore, which also detects a writer that populated it
  after preflight). To re-restore, drop + recreate the target.
- **No plaintext at rest (tool-wide).** An encrypted backup STREAMS `pg_dump | gpg` — no plaintext
  dump file is ever written. The artifact is written to a temp, integrity-checked, atomically
  renamed, then metadata is written last. A failure leaves at most a ciphertext fragment and no
  metadata. `verify.sh` STREAMS `gpg --decrypt | pg_restore --list` and `restore.sh` STREAMS
  `gpg | pg_restore` — neither writes a decrypted temp, so no step depends on plaintext shredding.
- **Encrypted verification validates the ARCHIVE, not just decryptability.** `verify.sh` requires the
  decrypted stream to be a non-empty PostgreSQL custom-format archive (`pg_restore --list` TOC), so
  encrypted non-PostgreSQL content with a correct passphrase/sha256 is **rejected**.
- **No secrets in the tool.** DB auth via libpq (`PGPASSWORD`/`~/.pgpass`/`PGSERVICE`); passphrase via
  `GUVFX_BACKUP_PASSPHRASE[_FILE]`, fed to gpg on a dedicated fd. Nothing secret on argv or in logs.
- **Encryption.** Production-class backups must be encrypted (AES256). Non-production may opt out with
  `--no-encrypt`.
- **Capacity: conservative estimate + enforceable ceiling.** The preflight figure (DB size + 100 MiB)
  is a conservative **estimate, not a guaranteed maximum**. Enforcement is a hard `ulimit -f` output
  ceiling (`free − GUVFX_BACKUP_MIN_FREE_BYTES`, default floor 2 GiB; optionally capped lower by
  `GUVFX_BACKUP_MAX_ARTIFACT_BYTES`): a dump that would breach the floor fails like ENOSPC, the partial
  is removed, and no metadata is written (an incomplete artifact is never certified).
- **Audit gates (do not conflate).** `--gate` is **inventory-only** (presence/freshness/encryption/
  orphan) and does not certify usability; **`--gate-usable`** adds mandatory per-artifact integrity
  verification and fails closed on a present-but-corrupt artifact. Use `--gate-usable` for readiness.
- **On-host is not enough.** An on-host backup does not survive host/storage loss — replicate the
  artifact + its `.meta.json` sidecar **off-host** (NAS / object store). The tool does not transfer.

## Scripts

| Script | Purpose | Default |
|---|---|---|
| `backup.sh --out DIR [--label L] [--execute] [--no-encrypt]` | STREAMED `pg_dump -Fc` (+ AES256, no plaintext) → temp → integrity → atomic finalize → metadata; enforceable `ulimit -f` ceiling | dry-run |
| `verify.sh --meta M.json` | integrity (sha256) + for encrypted: streamed `gpg | pg_restore --list` TOC (archive-format, read-only) | acts (read-only) |
| `restore.sh --meta M.json [--execute]` | atomic restore into a verified-EMPTY, allowlisted+owned non-production target (no `--force`) | dry-run |
| `audit_report.sh --dir DIR [--max-age-hours H] [--verify-integrity] [--gate | --gate-usable]` | recovery-point observability; `--gate` inventory-only, `--gate-usable` adds mandatory integrity | report |
| `tests/setup_isolated_cluster.sh --up` / `--down` | **privileged** provisioner that **`initdb`s a dedicated ephemeral cluster** (private socket, no TCP, least-privilege role + marker inside it); `--down` **verifies the postmaster stopped before any `rm`** (refuses + preserves evidence otherwise) and removes only the files it created. No DDL against a shared cluster | acts (privileged) |
| `tests/attest_isolated.sh` | sourced guard: independent isolated-cluster attestation (no mutation) | n/a |
| `tests/eph_tracking.sh` | sourced helper: subshell-safe (file-based) ephemeral-root tracking + cleanup | n/a |
| `tests/certify_provisioner.sh` | privileged-provisioner trust-boundary certification (R3; 20 cases: setup/teardown symmetry, production-selection refusal, redirection neutralisation, verified-shutdown teardown, cleanup safety) | runs |
| `tests/certify_cleanup.sh` | harness cleanup-tracking certification (R3 CLOSEOUT+FINAL; 13 cases: tracking survives a subshell; the trap cleans an abnormal exit; main + unrelated dirs untouched) | runs |
| `tests/certify.sh` | isolated recovery backup/verify/restore/reconcile certification (64 cases), run **as the least-privilege role** after independent attestation | runs |

## Typical isolated use (dev/test)

```bash
export GUVFX_RECOVERY_TARGET_CLASS=test
export PGHOST=... PGPORT=5432 PGUSER=... PGDATABASE=mydb   # libpq; PGPASSWORD via env/.pgpass
export GUVFX_RECOVERY_ISOLATED_CLUSTER_ID="$(psql -tAqc 'select system_identifier from pg_control_system()')"
createdb guvfx_recovery_isolated_ok    # positive isolation marker (production structurally lacks it)
export GUVFX_BACKUP_PASSPHRASE='...'                        # from your secret store

ops/recovery/backup.sh --out /secure/backups --label predeploy          # dry-run (plan + preflight)
ops/recovery/backup.sh --out /secure/backups --label predeploy --execute
ops/recovery/verify.sh  --meta /secure/backups/guvfx_predeploy_*.meta.json
# copy the artifact + .meta.json OFF-HOST here (operator step; not done by the tool)

createdb scratch_restore_target; export PGDATABASE=scratch_restore_target   # a FRESH, EMPTY DB you OWN
export GUVFX_RECOVERY_ALLOWED_TARGET_DB=scratch_restore_target               # positive per-DB authorization
ops/recovery/restore.sh --meta /secure/backups/guvfx_predeploy_*.meta.json            # dry-run
ops/recovery/restore.sh --meta /secure/backups/guvfx_predeploy_*.meta.json --execute
```

## Certification

Certification is split so the destructive harness never establishes its own isolation evidence, and
the privileged provisioner never does DDL against a shared cluster — it **`initdb`s its own disposable
cluster**:

```bash
# 0) (optional) certify the privileged provisioner's trust boundary itself (20 cases).
bash ops/recovery/tests/certify_provisioner.sh

# 1) PRIVILEGED: provision a DEDICATED EPHEMERAL cluster (private data dir/socket, no TCP). This
#    creates the least-privilege role + marker INSIDE the fresh cluster it owns.
GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID=<main/prod sysid> GUVFX_RECOVERY_TESTER_PASSWORD=<throwaway> \
  GUVFX_RECOVERY_EPHEMERAL_ROOT=/path/to/new/dir  bash ops/recovery/tests/setup_isolated_cluster.sh --up
# (it prints the exact env to export: PGHOST=<socket> PGPORT PGUSER=<non-superuser role> ISOLATED_CLUSTER_ID ...)

# 2) LEAST-PRIVILEGE: run the destructive recovery certification AS the non-superuser role (64 cases).
GUVFX_RECOVERY_ATTEST_ISOLATED=1 bash ops/recovery/tests/certify.sh

# 3) teardown: stop + remove ONLY the ephemeral cluster's files (no shared-cluster DDL).
GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID=<main/prod sysid> \
  GUVFX_RECOVERY_EPHEMERAL_ROOT=/path/to/that/dir  bash ops/recovery/tests/setup_isolated_cluster.sh --down
```

`certify.sh` calls `attest_independent_test_cluster` **before any mutation** (mandatory production
denylist; operator-attested cluster id must equal the live `system_identifier`; pre-existing marker it
did NOT create; non-superuser role; loopback-only; localhost is not proof), then asserts the full
backup→verify→restore→reconcile round-trip (incl. the encrypted streaming case) plus the R1 + R2
fail-closed matrix (cluster isolation, per-DB target authorization, encrypted archive-format
verification, inventory-vs-usable gates, enforceable capacity ceiling). `certify_provisioner.sh` (R3)
certifies the provisioner itself: the production id is mandatory on setup AND teardown; setup never
adopts a pre-existing cluster; teardown refuses an unidentified/unstamped/mismatched/production-matching
target and removes only files it owns; `PGSERVICE`/libpq defaults cannot redirect it. Requires `gpg`,
`initdb`/`pg_ctl`/`pg_controldata`, and a local Postgres. Evidence: `tests/CERTIFICATION_EVIDENCE.txt`.
Note: certified on local PG 17 (synthetic, least-privilege role) — **not production parity**; a
PG 15 / Linux parity run is a Sponsor-gated follow-up.
