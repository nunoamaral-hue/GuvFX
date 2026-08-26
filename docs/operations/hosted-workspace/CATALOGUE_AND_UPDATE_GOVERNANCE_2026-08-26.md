# Broker Catalogue V1 + Managed Update Governance — Architecture (2026-08-26)

Companion to `BROKER_CATALOGUE_PRESEED_2026-08-26.md` (the empirical proof,
`BROKER_CATALOGUE_PRESEED_PROVEN`). This doc finalises the architecture for Objectives B/C/D of the
pre-beta acceptance-hardening packet. **Status: design + interfaces. Implementation is the immediate
follow-up packet** — deliberately NOT shipped in the acceptance-hardening packet so the verified
production image is unchanged for the Sponsor's acceptance test (which uses Pepperstone via the existing
native-discovery fallback and does not exercise the catalogue).

## Why implementation is deferred (not skipped)

1. The acceptance test uses **Pepperstone = unsupported**, so it exercises the **existing native-discovery
   fallback**, which already works: today's onboarding never restricts brokers — the customer logs their own
   broker in inside MT5. The catalogue is not on the acceptance-test path.
2. Shipping catalogue code requires a new backend image + a provisioning-stage change. Redeploying the
   image we just certified for the test would replace the very artefact under test. Amber (touches shared
   provisioning structure) → an explicit documented decision to defer.
3. The broker-selection UX (B4) and host catalogue-store population (arming) must land **together** with the
   preseed stage (B3) to be exercisable end-to-end; splitting them ships inert code. The follow-up packet
   implements B3+B4+arming cohesively.

## B — Broker Catalogue V1 (design)

**Golden stays broker-neutral** (empty `config`); the catalogue is a separate authority: `Golden vX` +
`Catalogue vY`, independent lifecycles.

### Storage (per-broker, versioned, on host)
```
C:\GuvFX\catalogue\v1\
  manifest.json                 # signed/hashed catalogue manifest (below)
  IS6\servers.dat               # certified, sha256 16600F67E3C4… (3788 B)
```
The binary `servers.dat` lives on the host store, **not in Git** (broker binary data). Git holds the
**manifest** (metadata + sha256) only.

### Manifest schema (non-secret only)
```json
{ "catalogue_version": 1, "created_at": "…", "brokers": [
  { "broker_id": "IS6", "display_name": "IS6 Technologies", "enabled": true,
    "environments": ["demo","live"],
    "servers": [ {"server_name":"IS6Technologies-Demo","type":"demo"},
                 {"server_name":"IS6Technologies-Live","type":"live"} ],
    "servers_dat_sha256": "16600F67E3C4…", "source":"branded-terminal-5833",
    "certified_mt5_build":"5833+", "certified_at":"…",
    "certification_evidence":"docs/operations/hosted-workspace/BROKER_CATALOGUE_PRESEED_2026-08-26.md" } ] }
```
**Never** contains: `accounts.dat`, login, password, account number, history, customer identity.

### DB (reuse, do not fork)
Reuse `trading.BrokerServer` (`server_name`, `broker_display_name`, `environment`, `aliases`) for
selection metadata. Add a single nullable `HostedMt5Workspace.catalogue_broker_id` (blank default) to
record the customer's selected broker. No parallel `BrokerServerDefinition` schema.

### Preseed (B3) — new provisioning stage, fail-closed, flag-gated
New `slot_preparation` stage after Stage 5 (populate_runtime), mirroring Stage 5a exactly:
```
if hosted_broker_catalogue_enabled() and ws.catalogue_broker_id:
    res = _call("preseed_broker_catalogue", ST_CATALOGUE, runtime_root, broker_id, expected_sha, …)
    fail-closed → PREP_CATALOGUE_FAILED
```
Host primitive `preseed_broker_catalogue` (PowerShell `Preseed-GuvfxBrokerCatalogue.ps1`, ASCII/ParseFile):
copy `catalogue\vN\<broker_id>\servers.dat` → `runtime\config\servers.dat`, **read-back verify sha256**
against the manifest value, refuse a missing/corrupt/mismatched artefact (never silently fall back), refuse
any `accounts.dat` in the source. Flag OFF or no broker selected ⇒ stage skipped ⇒ byte-identical (guarded
by a test).

### Onboarding selection (B4)
Ask broker **before** the hosted-terminal step: Broker → Environment (Demo/Live) → Continue. Provision drops
the chosen broker's `servers.dat`; MT5 opens with the broker already resolvable; the customer types their own
login/password **inside MT5**. GuvFX never requests/stores the MT5 password. No auto-login.

### Unsupported broker (B5) — already the status quo
No catalogue entry ⇒ the existing flow stands: MT5 native discovery ("Can't find your broker? Use another MT5
broker"). This is what the Pepperstone acceptance test exercises today. Discovered metadata is **never
auto-trusted** (see C).

## C — Future broker capture / certification (design; automatic promotion PROHIBITED)

Pipeline: **detect → capture → sanitise → certify → immutable candidate → Operations approval → publish
Catalogue vN+1 → verify → report.** Automatic *capture* may be built; automatic *production promotion* is
forbidden.

1. **Capture** a candidate `servers.dat` from a one-time supervised discovery on a disposable runtime (never a
   customer runtime). 2. **Sanitise/prove-clean:** assert no `accounts.dat`/login/password/history/customer
   state (the A–F classifier from the proof doc). 3. **Broker identity** = the manifest `broker_id` + the
   public server names, established by the certifier, not by customer input. 4. **Demo/Live** = separate
   `servers[]` entries. 5. **Candidate hash** = sha256 of the exact `servers.dat`. 6. **Approval binds to the
   exact immutable hash** (below). 7/8/9/10 see D (shared approval/expiry/rollback/provenance machinery).

## D — Managed MT5 update lifecycle (design)

Tenant MT5 cannot self-update (LiveUpdate executable-immutability + staging containment, ARMED). Golden
updates are governed: **detect MetaQuotes release → acquire → verify Authenticode → sanitise → clean golden →
automated regression/cert → immutable candidate → Operations YES/NO → promote exact candidate as Golden vN+1
→ verify → report.** **Existing tenants never auto-migrate** (a separate maintenance workflow).

- **D1 independence:** a catalogue update never requires an MT5 upgrade; an MT5 upgrade never forces broker
  re-cert (record a compatibility range where needed). 
- **D2 golden-drift gate:** stays DARK until exactly one canonical golden exists, build==manifest,
  materialisation uses it, a rollback golden exists, tests pass, and a production-safe proof passes — a
  separate bounded stream (golden reconciliation).

## Operations approval boundary (shared by C and D)

No dedicated "Operations bot" exists yet (`admin_ops` = EntitlementOverride console; `operational_events` =
read-only event log). Design the approval boundary on those:

- A **candidate** = an immutable record (`kind` = `broker_catalogue` | `golden_build`, `artefact_sha256`,
  `metadata`, `created_at`, `expires_at`, `superseded_by`, `status` = pending|approved|rejected|expired|
  promoted). Persist as an `OperationalEvent` + a small `PromotionCandidate` model in `admin_ops`.
- **Approval binds to the exact `artefact_sha256`.** Promotion re-verifies the on-host artefact hash equals
  the approved candidate's hash; a mismatch refuses to promote.
- **Expiry/staleness:** `expires_at` (configurable, default 48h). A candidate that is expired **or**
  `superseded_by` a newer candidate for the same target **cannot** promote — the promoter checks both.
- **Promotion is atomic + reversible:** publish `catalogue\vN+1\` (or `golden\<build>`) then flip a single
  active-version pointer; **rollback** flips the pointer back to `vN`. New tenants record both
  `golden_version` and `catalogue_version` (+ chosen broker) in provisioning provenance.
- Human approval only (Red/Nuno gate); model output may inform, never promote (governance overlay).

## Bounded next packet (implementation)

Catalogue V1 code (flag, manifest service, `Preseed-GuvfxBrokerCatalogue.ps1` + primitive, `slot_preparation`
Stage, `catalogue_broker_id` migration, B4 selection UX, tests incl. flag-off byte-identical) → PR → CI →
merge → deploy DARK → populate host `catalogue\v1\IS6\servers.dat` → arm for NEW tenants only. Then the
`PromotionCandidate` + Operations-approval machinery (C/D). Existing tenants are never migrated here.
