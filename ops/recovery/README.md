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
- **Production is protected (operator-declaration + configured backstop).** Protection keys on the
  declared class — **declaring it correctly is an operator responsibility.** Set
  `GUVFX_RECOVERY_PRODUCTION_HOST` / `GUVFX_RECOVERY_PRODUCTION_DB` in the production operator
  environment: if the live DSN matches them while the class is not `production`, every mutating op
  **fails closed** (a mislabel cannot silently permit a production mutation).
  - A **backup** of a `production` target is refused unless **both** `GUVFX_RECOVERY_ALLOW_PRODUCTION=1`
    **and** `--i-understand-production` are set (this double-gate is intentionally *not* used anywhere
    in the repo).
  - A **restore** into a `production` target is refused **categorically** — the tool never restores
    into production. A production restore is a separate, manually-governed operation.
- **No secrets in the tool.** DB auth comes from standard libpq (`PGPASSWORD` / `~/.pgpass` /
  `PGSERVICE`). The backup encryption passphrase comes from `GUVFX_BACKUP_PASSPHRASE` or
  `GUVFX_BACKUP_PASSPHRASE_FILE`. Nothing secret is printed or written to argv.
- **Encryption.** Production-class backups must be encrypted (AES256). Non-production may opt out with
  `--no-encrypt`.
- **On-host is not enough.** An on-host backup does not survive host/storage loss — replicate the
  artifact + its `.meta.json` sidecar **off-host** (NAS / object store). The tool does not transfer.

## Scripts

| Script | Purpose | Default |
|---|---|---|
| `backup.sh --out DIR [--label L] [--execute] [--no-encrypt]` | consistent `pg_dump -Fc` + optional AES256 + recovery-point metadata | dry-run |
| `verify.sh --meta M.json` | integrity (sha256) + decrypt test + `pg_restore --list` TOC (read-only) | acts (read-only) |
| `restore.sh --meta M.json [--execute] [--force] [--jobs N]` | restore into a NON-production target (atomic by default) | dry-run |
| `audit_report.sh --dir DIR [--max-age-hours H] [--gate]` | recovery-point observability; `--gate` fails closed on risk | report |
| `tests/certify.sh` | isolated backup/verify/restore/reconcile + fail-closed certification (synthetic data, scratch DBs) | runs |

## Typical isolated use (dev/test)

```bash
export GUVFX_RECOVERY_TARGET_CLASS=test
export PGHOST=... PGPORT=5432 PGUSER=... PGDATABASE=mydb   # libpq; PGPASSWORD via env/.pgpass
export GUVFX_BACKUP_PASSPHRASE='...'                        # from your secret store

ops/recovery/backup.sh --out /secure/backups --label predeploy          # dry-run (plan + preflight)
ops/recovery/backup.sh --out /secure/backups --label predeploy --execute
ops/recovery/verify.sh  --meta /secure/backups/guvfx_predeploy_*.meta.json
# copy the artifact + .meta.json OFF-HOST here (operator step; not done by the tool)

export PGDATABASE=scratch_restore_target   # a fresh, empty DB you created
ops/recovery/restore.sh --meta /secure/backups/guvfx_predeploy_*.meta.json            # dry-run
ops/recovery/restore.sh --meta /secure/backups/guvfx_predeploy_*.meta.json --execute
```

## Certification

`bash ops/recovery/tests/certify.sh` creates throwaway scratch databases, seeds synthetic data
(trading/trade/execution/acquisition/checkpoint/broker-event/withdrawal/customer/immutable-audit),
and asserts the full backup→verify→restore→reconcile round-trip (incl. encrypted) plus the
fail-closed matrix. Requires a local Postgres the current user can `createdb`/`dropdb` on, and `gpg`
for the encrypted case. Captured evidence: `tests/CERTIFICATION_EVIDENCE.txt`.
