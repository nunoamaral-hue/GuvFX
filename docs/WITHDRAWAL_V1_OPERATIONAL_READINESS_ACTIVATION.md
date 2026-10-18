# Withdrawal V1 — Operational-Readiness Activation Runbook

**Packet:** GUVFX Post-Weekend Security & Readiness Closeout · **Date:** 2026-10-18
**Status:** PREPARATIONS ENGINEERING_COMPLETE (DARK). **No storage purchased, no Google publishing change, no
production code deployed, no migration applied.** Every item below is a *prepared* activation whose final GO is an
operator/Sponsor action, not performed here.

This runbook advances four readiness items using the **existing** recovery (`ops/recovery/`) and monitoring
(`broker_mailbox_health` + `ops/monitoring/`) tooling. For each: the prepared state, the exact existing-tooling
commands, the prerequisites that require an operator/Sponsor, the GO/NO-GO gate, and validation evidence captured
against the safe/dry-run paths.

> **ENGINEERING_COMPLETE ≠ PRODUCTION_ACTIVATION.** "Prepared" means the tooling, templates and procedure exist and
> have been exercised in their safe modes. Each item still has a human/infra prerequisite called out explicitly.

---

## 1. Off-host encrypted backup — deployment PREPARATION

**Prepared:** `ops/recovery/backup.sh` produces a consistent `pg_dump -Fc` recovery point **streamed directly through
`gpg` (AES256)** — no plaintext ever touches disk — plus a JSON metadata sidecar (provenance + sha256). It is
**dry-run by default**, refuses a production target unless an explicit double-gate is set (this packet never sets it),
and takes the DB password only from standard libpq channels and the passphrase from `GUVFX_BACKUP_PASSPHRASE[_FILE]`
(no secret in Git).

**Activation command (operator, when an off-host destination exists):**
```
export GUVFX_RECOVERY_TARGET_CLASS=prod          # declares intent; backup of prod also needs the explicit double-gate
export GUVFX_BACKUP_PASSPHRASE_FILE=/run/secrets/guvfx_backup_passphrase
PGHOST=100.119.23.29 PGUSER=guvfx PGDATABASE=guvfx \
  bash ops/recovery/backup.sh --out /mnt/offhost/guvfx/recovery --execute   # --out MUST be the OFF-HOST mount
bash ops/recovery/verify.sh --artifact /mnt/offhost/guvfx/recovery/<label>.dump   # integrity: sha256 + decrypt + TOC
bash ops/recovery/audit_report.sh --dir /mnt/offhost/guvfx/recovery --verify-integrity
```

**Prerequisites (operator/Sponsor — blocking):**
- An **off-host destination** that is NOT the production VPS (host loss must be survivable). The Synology NAS
  Tailscale peer `100.73.130.98` (`reference_nas_and_dr_backup`) is a candidate; **no storage purchase is authorized
  here** — the destination decision is the Sponsor's.
- Custody of the `gpg` recipient key OFF the backup host (key loss = total recovery loss) + a tested key-restore path.
- A scheduled cadence installed via a **supported service mechanism** (systemd timer / cron), **never** an
  interactive SSH `nohup`/`Start-Process` (security RULE 1).

**GO/NO-GO:** NO-GO until the off-host destination + key custody + schedule exist. The tooling is GO.

**Validation evidence (safe paths, 2026-10-18):**
- `backup.sh` with no `--out` → fail-closed: `[recovery][ERROR] --out DIR is required` (dry-run refuses to act on
  incomplete config).
- `audit_report.sh --dir <empty>` → read-only inventory table + `NO recovery points found` + explicit
  `RECOVERY_POINT_RESTORE_TESTED is NOT established by this tool` (the 4-property discipline is intact).
- Logic certified in tests: PR #498/#499, CERTIFIED_IN_TESTS 97/97 (DARK).

## 2. Isolated PostgreSQL 15 restore CERTIFICATION — preparation

**Prepared:** `ops/recovery/restore.sh` restores a **verified** artifact into a NON-production target, dry-run by
default, refusing a production target categorically; it requires BOTH an independently-attested isolated cluster
(operator-planted marker, `tests/attest_isolated.sh`) AND an independently-authorized target DB (allowlisted,
non-superuser owner, not admin/template/prod). `ops/recovery/tests/certify.sh` runs the full isolated restore
certification (Gate F) + failure containment (Gate H) against a least-privilege role on an attested cluster;
`tests/setup_isolated_cluster.sh` is the operator's out-of-band provisioner.

**Certification command (operator, on a PG15/Linux host):**
```
# 1. Operator provisions an isolated, disposable PG15 cluster OUT OF BAND (privileged), planting the marker + role:
bash ops/recovery/tests/setup_isolated_cluster.sh          # creates least-privilege role + isolation marker
# 2. Operator attests identities (so the destructive harness cannot run on an unknown cluster):
export GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID=<prod cluster id>     # production positively identifiable
export GUVFX_RECOVERY_ISOLATED_CLUSTER_ID=<the isolated cluster id>
# 3. Run the certification (non-superuser role, synthetic data; never touches prod):
bash ops/recovery/tests/certify.sh
# 4. Restore a REAL recovery point into the attested cluster + authorized DB to establish RECOVERY_POINT_RESTORE_TESTED:
bash ops/recovery/restore.sh --artifact <offhost>/<label>.dump --target-db guvfx_restore_cert --execute
```

**Prerequisites (operator/infra — blocking):**
- A **PostgreSQL 15 / Linux** isolated cluster that matches production (engine 15.15 per Gate A). The macOS
  provisioner host is Docker-less and falls back to a **synthetic PG17** cluster — **not PG15/Linux parity**, so a
  cert produced there is NOT authoritative (security RULE 11: a measurement path must be shown to produce a known
  result on the real parity before it is trusted).
- The off-host recovery point from §1 (restore-tested needs a real artifact).

**GO/NO-GO:** NO-GO for an authoritative certificate until a PG15/Linux isolated cluster exists. The harness +
procedure are GO; a synthetic-PG17 dry-run may be used for rehearsal only, clearly labelled non-authoritative.

## 3. Mailbox alert ACTIVATION — preparation

**Prepared:** `ops/monitoring/guvfx-broker-mailbox-health.{service,timer}` (DARK templates, NOT installed) wire the
existing `broker_mailbox_health --json --exit-nonzero-on-degraded` command to a 5-minute systemd timer. A non-zero
exit (any mailbox STALE / RECOVERY_REQUIRED) is the alert; the report always prints first; de-duplicated by
construction (current-state read). Read-only, secret-free (masked email + stable state only), no broker contact.

**Activation command (operator):**
```
install -m0644 ops/monitoring/guvfx-broker-mailbox-health.service /etc/systemd/system/
install -m0644 ops/monitoring/guvfx-broker-mailbox-health.timer   /etc/systemd/system/
# edit WorkingDirectory + wire OnFailure= to the estate notifier, then:
systemctl daemon-reload && systemctl enable --now guvfx-broker-mailbox-health.timer
```

**Prerequisites (operator — blocking):** the estate notifier to wire `OnFailure=` to; confirmation of the compose
`WorkingDirectory`. **No new production timer is installed by this packet.**

**GO/NO-GO:** GO for the operator to install when armed by the PD; the templates + command are ready.

**Validation evidence:** the health read-model + the `--exit-nonzero-on-degraded`/`-recovery` exit semantics are
covered by the `broker_intelligence` suite (health tests; 518 OK on the security branch). Live-DB run on the dev host
is not meaningful (dev DB unmigrated); the test DB is the authoritative exercise.

## 4. Google OAuth production-verification — HANDOFF

**Prepared:** `docs/WITHDRAWAL_V1_OAUTH_PRODUCTION_READINESS_CHECKLIST.md` is the operator checklist (OAuth consent
screen completeness, `gmail.readonly` restricted-scope justification, likely CASA assessment, publish-to-Production,
post-publish >7-day token-lifetime validation, redirect-URI parity). The consent-recovery UX (targeted reconnect) and
the alert hook (§3) are built. The root cause is restated honestly: **one observed token expiry** (2026-10-09 consent
→ ~2026-10-16 failure); the 7-day mechanism is inferred; publishing status is **unconfirmed** (Console-only).

**Handoff (to operator/governance — blocking, I cannot perform):**
- Confirm the project's publishing status in the Google Cloud Console (not host-determinable).
- Decide + budget the CASA security assessment (cost = Sponsor decision; **no purchase authorized here**).
- Publish to Production + submit `gmail.readonly` for verification. **Do NOT change publishing status or expand
  scopes without security/governance approval.**
- After publish, run the >7-day token-lifetime validation before any "customer-ready OAuth" claim.

**GO/NO-GO:** NO-GO for sustainable production OAuth until Google verification is established; the handoff package is
complete.

---

## 5. Summary — prepared vs blocked

| Item | Prepared (ENGINEERING_COMPLETE) | Blocked on (human/infra) |
|------|---------------------------------|---------------------------|
| Off-host encrypted backup | `backup.sh`/`verify.sh`/`audit_report.sh`, dry-run-safe, cert-in-tests | off-host destination + key custody + schedule (no purchase) |
| Isolated PG15 restore cert | `restore.sh` + `certify.sh` + attestation harness + procedure | a PG15/Linux isolated cluster (macOS = synthetic PG17, non-authoritative) |
| Mailbox alert | systemd service+timer templates + exit-code CLI | operator install + notifier wiring (no prod timer installed here) |
| Google OAuth verification | operator checklist + consent-recovery UX + alert hook | Google Console publish + restricted-scope verification (governance) |

Nothing here is activated. No storage purchased, no Google publishing change, no production code deployed, no
migration applied, no financial action.
