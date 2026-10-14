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
- **Authoritative target identity (NOT a caller label, NOT `system_identifier` alone).** A
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
- **Clean-target restore (no `--force`).** Restore goes ONLY into a freshly-created **empty** database
  (verified empty immediately before the atomic restore). To re-restore, drop + recreate the target.
- **No plaintext at rest (tool-wide).** An encrypted backup STREAMS `pg_dump | gpg` — no plaintext
  dump file is ever written. The artifact is written to a temp, integrity-checked, atomically
  renamed, then metadata is written last. A failure leaves at most a ciphertext fragment and no
  metadata. `verify.sh` decrypts to **stdout** and `restore.sh` STREAMS `gpg | pg_restore` — neither
  writes a decrypted temp, so no step depends on plaintext shredding.
- **No secrets in the tool.** DB auth via libpq (`PGPASSWORD`/`~/.pgpass`/`PGSERVICE`); passphrase via
  `GUVFX_BACKUP_PASSPHRASE[_FILE]`, fed to gpg on a dedicated fd. Nothing secret on argv or in logs.
- **Encryption.** Production-class backups must be encrypted (AES256). Non-production may opt out with
  `--no-encrypt`.
- **Capacity.** Backup preflight budgets a worst-case dump ~= DB size and refuses below a reserved free
  floor (`GUVFX_BACKUP_MIN_FREE_BYTES`, default 2 GiB).
- **On-host is not enough.** An on-host backup does not survive host/storage loss — replicate the
  artifact + its `.meta.json` sidecar **off-host** (NAS / object store). The tool does not transfer.

## Scripts

| Script | Purpose | Default |
|---|---|---|
| `backup.sh --out DIR [--label L] [--execute] [--no-encrypt]` | STREAMED `pg_dump -Fc` (+ AES256, no plaintext) → temp → integrity → atomic finalize → metadata | dry-run |
| `verify.sh --meta M.json` | integrity (sha256) + decrypt + `pg_restore --list` TOC (read-only) | acts (read-only) |
| `restore.sh --meta M.json [--execute]` | atomic restore into a verified-EMPTY non-production target (no `--force`) | dry-run |
| `audit_report.sh --dir DIR [--max-age-hours H] [--verify-integrity] [--gate]` | recovery-point observability; `--verify-integrity` proves each artifact; `--gate` fails closed | report |
| `tests/certify.sh` | isolated backup/verify/restore/reconcile + fail-closed certification (needs `GUVFX_RECOVERY_ATTEST_ISOLATED=1`) | runs |

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

createdb scratch_restore_target; export PGDATABASE=scratch_restore_target   # a FRESH, EMPTY DB
ops/recovery/restore.sh --meta /secure/backups/guvfx_predeploy_*.meta.json            # dry-run
ops/recovery/restore.sh --meta /secure/backups/guvfx_predeploy_*.meta.json --execute
```

## Certification

`GUVFX_RECOVERY_ATTEST_ISOLATED=1 bash ops/recovery/tests/certify.sh` attests a throwaway cluster
(refusing if it matches `GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID`), creates scratch databases, seeds
synthetic data, and asserts the full backup→verify→restore→reconcile round-trip (incl. the encrypted
streaming case) plus the HIGH-1/2/3/4 + MEDIUM-1/2 fail-closed matrix. Requires a local Postgres the
current user can `createdb`/`dropdb` on, and `gpg`. Evidence: `tests/CERTIFICATION_EVIDENCE.txt`.
Note: certified on local PG 17 (synthetic) — a PG 15 / Linux parity run is a Sponsor-gated follow-up.
