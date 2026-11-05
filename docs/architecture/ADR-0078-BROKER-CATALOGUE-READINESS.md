# ADR-0078 — Broker Catalogue Readiness: DB-level artefact immutability, readiness evaluator, and the NEEDS_CAPTURE candidate workflow

- **Status:** Accepted — repository-only, synthetic-only (GFX-BROKER-INTELLIGENCE-FOUNDATION-001, Stream A).
  DARK/default-OFF. No artefact capture/creation/approval/build/activation/staging and no production mutation
  are performed or authorised here. The PR is opened with auto-merge OFF and is **not merged** without a new
  Sponsor authorisation.
- **Date:** 2026-11-05
- **ADR number:** chosen against BOTH `docs/ADRs/` and `docs/architecture/` (shared number space, max 0076 on
  `main`); 0077 is held by the open Copier host-emitter PR (#615), so Stream A takes **0078**.
- **Builds on, does not replace:** ADR-0025 (deterministic broker-server resolution), ADR-0068 (catalogue
  carry-forward + governed host staging/attestation). The Broker Catalogue V1 mechanism (`broker_catalogue` +
  `approvals`) is unchanged in behaviour; this ADR adds a DB hardening, a read-only evaluator, and a governed
  entry point for not-yet-captured brokers.

## Context

Reconciliation established that the catalogue mechanism is governance-complete but ships **no artefact data**
(every `CatalogueVersion`/`CatalogueArtefact`/`ArtefactApproval` row is prod-DB/VPS/Notion state). Of the four
in-scope brokers, Pepperstone has an approved artefact in the ACTIVE catalogue; TradersWay and Vantage have
approved artefacts recorded only in docs/VPS pins (a DARK v3 carry-forward would activate them); **Dominion
Markets has no approved immutable artefact and no authoritative server id captured (NEEDS_CAPTURE)**. Two
repo-side gaps remained: (a) the `CatalogueArtefact` immutability was enforced only at the ORM `save()` layer
(a raw `QuerySet.update()`/`bulk_update` bypassed it — self-documented as a tracked follow-up), and (b) there
was no reviewable, read-only way to report which brokers are eligible for a future additive build, nor a
governed front door for a broker whose `servers.dat` has not been captured.

## Decision

1. **DB-level artefact immutability (migration `broker_catalogue/0005`).** A PostgreSQL `BEFORE UPDATE` trigger
   on `broker_catalogue_catalogueartefact` refuses any UPDATE of a row whose `CatalogueVersion` is not DRAFT,
   mirroring the ORM save-guard exactly and closing the raw-`update()`/`bulk_update` bypass. DRAFT artefacts
   remain freely editable while a version is assembled; a frozen artefact requires a NEW version. The migration
   is vendor-guarded (no-op on non-PostgreSQL, where the ORM guard still applies) and fully reversible.
2. **Read-only catalogue-readiness evaluator (`broker_catalogue/readiness.py`).** `evaluate_catalogue_readiness`
   reports, per broker and without mutating anything, a stable eligibility + reason code for a FUTURE additive
   build: `ELIGIBLE`, `ALREADY_ACTIVE`, `SANITISER_NOT_PASS`, `NO_APPROVED_ARTEFACT`, `NEEDS_CAPTURE`,
   `NOT_CAPTURED`, `DUPLICATE_SERVER_CONFLICT`. It reads the stored approval/catalogue/candidate state directly
   (independent of the DARK `is_artefact_approved` consumption gate, since it reports governance state, not what
   provisioning may consume) and detects server-name collisions that would make an additive build ambiguous.
   Exposed read-only via `manage.py report_catalogue_readiness` (never builds/activates/approves).
3. **Governed NEEDS_CAPTURE candidate workflow (`CatalogueCaptureCandidate` + `capture.py` +
   `register_capture_candidate`).** A broker with no captured `servers.dat` (e.g. Dominion Markets) enters the
   pipeline through a governed placeholder that records IDENTITY + a non-PII server hint ONLY — never bytes, a
   SHA, an approval, a version or any host/production state. Writing a candidate is DARK (gated by
   `BROKER_CATALOGUE_CAPTURE_WORKFLOW_ENABLED`, default OFF); the command is a dry-run otherwise. The workflow
   **STOPS at NEEDS_CAPTURE**: promotion to CAPTURED (and any artefact capture/approval) is a separate,
   authorised step this packet does not perform. This satisfies the packet rule: for a broker lacking an
   approved immutable artefact, create only the governed candidate/approval workflow and STOP before creating
   or approving an artefact.

## Consequences

- A frozen catalogue artefact can no longer be mutated by ANY DB write path; integrity is now defence-in-depth
  (ORM guard + DB trigger + manifest re-verification). One existing test,
  `test_byte_mutation_after_activation_detected`, modelled a post-activation tamper via a raw `update()`; it is
  renamed `test_byte_mutation_after_activation_prevented` and now asserts the mutation is REFUSED (a strictly
  stronger guarantee). Out-of-band host-byte drift remains covered by `test_manifest_field_tamper_detected` and
  preseed-time re-hashing. This is the only existing-test change in Stream A.
- The readiness evaluator gives a reviewable, read-only basis for planning a future additive v3 build; on a
  fresh/repo DB it honestly reports `NOT_CAPTURED`/`NO_APPROVED_ARTEFACT` because no artefact data is committed.
- Dominion Markets can be recorded as NEEDS_CAPTURE under the DARK flag without any artefact being created or
  approved. No catalogue build/activation, host staging, terminal action, or production mutation occurs.
- **Not changed / not authorised:** no artefact capture/creation/approval, no catalogue build/activation, no
  host staging/attestation, no terminal staging/preseed/restart/login, no real Gmail/MT5/broker/NAS/Wayond,
  no credentials/PII, no deployment, no public publication. Streams B–D are separate PRs, not begun here.
