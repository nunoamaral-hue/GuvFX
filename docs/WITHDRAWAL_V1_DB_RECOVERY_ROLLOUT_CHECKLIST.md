# Withdrawal V1 — Database Recovery Rollout Checklist (§13)

**Status:** BLOCKED (NO-GO for production activation). Foundation exists and is CERTIFIED_IN_TESTS (DARK).
**Date:** 2026-10-18 · **Packet:** GUVFX_WITHDRAWAL_V1_WEEKEND_COMPLETION_R2

**Scope guard:** this is a readiness **dependency to be scheduled, not executed** under this packet. **No paid
subscription or purchase is authorized. No production restore is performed. No migration (incl. broker_intelligence
0011) is applied.** Withdrawal V1 cannot be declared durable until an off-host, verified recovery point exists, because
the member-facing withdrawal history is durable financial evidence.

---

## 1. What exists (foundation, DARK, CERTIFIED_IN_TESTS)

- Tooling under `ops/recovery/` (PR #498 squash `26c8eeb`, deflake PR #499 `556335b`): streamed `pg_dump | gpg`
  backup; a dedicated **ephemeral `initdb` certification cluster** (no DDL on the shared cluster); per-DB restore
  authorization (allowlist + owner + non-superuser); `--gate` vs `--gate-usable` modes; `pg_ctl status`
  delete-only-on-exit-3 semantics. Certified **in tests** only (Docker-less host ⇒ PG17 synthetic, not PG15/Linux
  parity).

## 2. Gate A facts (production, observed — redacted)

- Engine **PostgreSQL 15.15**; role/db both `guvfx` (role is **not** `postgres`; non-superuser paths must assume this).
- Primary DB ~**2.84 GB** (dominated by `core_auditevent` ≈ 79%).
- **No PITR, no automated backup, no off-host copy.** Root filesystem was ~**96% full** at Gate A (later relieved to
  ~88% / ~25 GB free by build-cache prune only — image/volume prune FORBIDDEN).
- Host access is single-VPS (`ssh ubuntu@<vps>`); macOS provisioner must use `LC_ALL=C`.

## 3. Rollout checklist (each item = a GO gate; none executed here)

- [ ] **Off-host encrypted destination.** A storage target that is NOT the production VPS (so a host loss is
      survivable), holding only `gpg`-encrypted dumps. **No paid subscription is authorized** — the destination
      decision (existing NAS peer `100.73.130.98` vs other) is the Sponsor's.
- [ ] **Encryption key custody.** The `gpg` recipient key is managed out of Git / out of the backup host; key loss =
      total recovery loss, so custody + a tested key-restore path are part of the gate.
- [ ] **Schedule.** A backup cadence (e.g. daily full) started via a supported service mechanism only — **never an
      interactive SSH `nohup`/`Start-Process`** (security RULE 1).
- [ ] **Integrity verification.** Every backup verified (checksum + a `--gate-usable` restore into the ephemeral cert
      cluster) — a backup is not "done" until a restore of it has succeeded. Record exact commands + results
      (evidence rule), including what was NOT covered.
- [ ] **Isolated restore rehearsal.** A full restore into an isolated cluster (never the production DB) producing a
      **certified recovery point** (RPO/RTO measured, not asserted). **No production restore.**
- [ ] **Retention + capacity.** Documented retention window and the disk headroom it needs off-host; reconcile
      against the ~2.84 GB (growing) primary and the 96%→88% root-FS pressure. Capacity plan BEFORE scheduling, so
      backups cannot themselves fill a disk.
- [ ] **Recovery monitoring + alerts.** A missed/failed backup raises an alert (reuse the exit-code pattern from
      `WITHDRAWAL_V1_MAILBOX_MONITORING_CLOSEOUT.md`; no new interactive process).
- [ ] **Ownership.** A named owner for the backup, the key, and the restore runbook; recorded in the runbook
      (`docs/RUNBOOK.md`).
- [ ] **Cost sign-off.** Any recurring cost is a Sponsor decision — this checklist proposes, it does not purchase.

## 4. Parity caveat (must be closed before trusting a cert)

The in-test certification runs on a Docker-less macOS host and falls back to a **synthetic PG17** cluster, which is
**not** PG15/Linux parity. A production-representative restore rehearsal (§3) on PG15/Linux is required before the
recovery point is trusted. Treat a negative/positive operational finding as unproven until the same measurement path
has produced a known control result (security RULE 11).

## 5. Do-NOT list (hard gates)

- Do **not** run a production restore. Do **not** apply any migration. Do **not** purchase storage.
- Do **not** prune Docker images/volumes to make room (build-cache only, already done).
- Do **not** start the scheduled backup from an interactive SSH session.

**Marker:** `DATABASE_RECOVERY_READINESS` = **BLOCKED** — foundation exists (DARK, in-test certified); off-host
encrypted destination + PG15/Linux restore rehearsal + certified recovery point are owed before Withdrawal V1 can be
called durable. Ties to memory `project_db_recovery_foundation`.
