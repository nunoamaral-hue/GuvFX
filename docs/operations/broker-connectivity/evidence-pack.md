# Release Evidence Pack — Broker Connectivity Trusted Beta (WP5.4 Workstream K)

The evidence an authorised approver needs to sign off Trusted-Beta arming. This defines **what to collect,
where it lives, who owns it, how long it is kept, how it is redacted, and what must never be included.** It
does **not** collect evidence now (nothing is armed); it specifies the pack.

**Format.** Machine-readable evidence uses the existing manifest schema
`evidence/schema/evidence-manifest.schema.json` (JSON Schema draft-07). Required fields (16):
`schema_version, handoff_id, packet_id, created_at_utc, branch, base_commit, head_commit, commands,
expected_results, actual_results, status, limitations, artefact_locations, checksums, reviewer` (+ `status`
∈ `PASS/PARTIAL/FAIL`). Existing manifests live in `evidence/manifests/*.json`; the governance check
`scripts/check_evidence_manifests.py` + `tests/test_evidence_manifests.py` validate them (part of
`make governance-check` / CI `governance`). Follow `.claude/rules/evidence.md`: machine-readable, exact
commands + actual results, state limitations, checksums where integrity matters, `PASS` only when the
criteria actually ran.

---

## Pack contents

Each item lists **what it proves** and **source**. Repo-verifiable items are collectible from the
repository; host items are **HOST-VERIFIED / OUTSIDE REPOSITORY CONTROL** and collected on the host.

| # | Item | Proves | Source |
|---|------|--------|--------|
| 1 | Commit / PR references | The exact merged code under test | `git log`; PR list (#265,#268,#274,#275,#276,#277, WP4.x, WP5.4 PR) |
| 2 | CI evidence | Backend/frontend/governance/research/market-data all green on the release commit | GitHub Actions run URLs + statuses |
| 3 | Migration evidence | Migrations applied cleanly; no divergence | `showmigrations`; `makemigrations --check`; deploy log |
| 4 | Image / manifest fingerprints | The deployed images + agent bundle are the certified builds | backend/frontend image tags; `deploy/beta-agent/manifest.json` checksums; `validation_image_manifest.json` structural fingerprint; golden `.guvfx_golden_manifest` pin |
| 5 | Rollback references | Rollback image tags + validation 6073 baseline recorded | prior image tags; `rollback_identifier.build_6073_validation_baseline` |
| 6 | Feature-flag state | All six flags OFF at each pre-arming checkpoint; the exact flag set per arming stage | `feature-flags.json`; readiness test PASS; per-stage flag records (HOST-VERIFIED) |
| 7 | WP6 results | Multi-tenant certification PASS (prerequisite for stages 6–7) | WP6 packet output (separate increment) |
| 8 | Validation evidence | demo→`HEALTHY/demo_ok`, live→`live_detected`, invalid→`NEEDS_ATTENTION`, platform→`UNAVAILABLE`; no order placed | `BrokerAccountValidationAttempt` (masked); validation-runner diagnostics (HOST) |
| 9 | Multi-tenant results | Owner-scoping + isolation proven across tenants | WP6 isolation tests; operational-event owner-scoping transcript |
| 10 | Incident dry-run evidence | Each rollback path rehearsed; disable-flag proven | rollback dry-run notes (`OPS-6`) |
| 11 | Operator-console screenshots / test artefacts | The read-only operator UI renders correctly and leaks no internals | `operations/*` flag-gate + event-detail tests; a non-production flag-ON screenshot |
| 12 | Customer Zero no-drift evidence | CZ untouched throughout; golden STOP-check byte-identical | golden STOP-check before/after; CZ order-count = 0 |
| 13 | Production no-trade evidence | No order placed during any pre-arming / arming verification (except a sanctioned post-WP6 execution proof) | order/job records; shadow-worker `order_check`-only confirmation |
| 14 | Credential-access audit evidence | Credential access was point-of-use, redacted, and matched validation volume | `AuditEvent` `CREDENTIAL_ACCESSED` (redacted) |
| 15 | Sponsor approvals | Each Sponsor gate (per arming stage + Trusted-Beta) explicitly approved | Sponsor approval records with UTC timestamps |

---

## Location, owner, retention

- **Location:** repository evidence for repo-verifiable items → `evidence/manifests/*.json` (+ referenced
  artefacts under `evidence/`); **bulk/host artefacts (screenshots, host command output, large logs) live
  OUTSIDE Git** — reference them by location + checksum in the manifest (per `.claude/rules/data.md`: no
  large/raw data in Git). The canonical index for a release is a single evidence manifest per arming stage,
  plus a top-level WP5.4 handoff.
- **Owner:** Engineering owns repo-verifiable manifests + checksums; Operator owns host-collected evidence;
  Sponsor owns the approval records and the final sign-off.
- **Retention:** keep the full pack for the life of the Trusted Beta and until the subsequent broader-beta
  decision; negative/failed results are **retained**, not discarded (`.claude/rules/research.md`). Do not
  delete incident or PIR evidence.

## Redaction rules (mandatory)

- **Never include a secret VALUE** — no passwords, tokens, API keys, private keys, Fernet keys, keyring
  material, envelope private keys, broker credentials, or session strings. Record the file/path/**category**
  only (`.claude/rules/security.md` "Redact in evidence").
- **Never include env-var VALUES** — flag/secret **names** only.
- **Mask account identifiers** the way the code does: login → `***` + last 3; never the full account
  number/password.
- **Strip host internals** from customer-facing artefacts — no host paths, PIDs, IPC/TCP endpoints, raw
  exception strings, `state_version`, or internal job/plan ids in anything a customer could see.
- **Operational events are non-authoritative** — if an event projection is included, label it as a
  cache/projection, not authoritative business state.

## What must NEVER be included

- Any secret or env-var value (see above); the private management-channel token; the envelope private keys;
  the signing keyring.
- Plaintext customer credentials, in any field, at any point.
- Bulk/raw market data or binary artefacts committed into Git.
- Customer Zero's live broker credentials or any live-account credential.
- Ciphertext, DPAPI blobs, `accounts.dat`, broker history/deals/orders/positions files, or any
  forbidden-artefact from the validation-image denylist.
- Screenshots or logs that expose another owner's data (cross-tenant), even in an operator artefact.

## Sign-off gate

A Trusted-Beta arming sign-off (stage 7) requires items **1–15** present and, where applicable, `PASS`;
`PARTIAL`/`FAIL` items must be explained and cleared or explicitly accepted by the Sponsor. **WP6 PASS
(item 7) is a hard precondition** for the execution-gate and invitation stages. Overstated completion is
prohibited (SECURITY RULE 7): record partial remediation accurately rather than claiming "done".
