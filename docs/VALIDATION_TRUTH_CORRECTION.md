# Broker-Login Validation — Programme Truth Correction (2026-08-05)

**Purpose.** Reconcile a genuine inconsistency in the programme record: some summaries read as if a
**live broker login** was certified, when the evidence proves only a **credential-free validation path**
was certified and **no live credentialed broker validation has ever succeeded**. This record **corrects
the wording; it does not rewrite history** — the original historical entries in `STATUS.md` are retained
verbatim. Where a statement is corrected below, treat THIS document as authoritative.

## The one consistent truth (evidence-anchored)

- **Engineered correctness: PROVEN.** WP6A executed 387 backend + 46 frontend tests (2026-08-04 @ `b3e0bba`),
  including `terminal_provisioning.tests_validate_login_agent` (×40) where a **mock** agent returns
  `demo_ok`/`HEALTHY`/`is_demo=true`. This proves the **code**, not a live login.
- **Credential-free host path: PROVEN.** On the production host (`100.79.101.19 guvfx-windows-mt5`,
  Session-0 agent `NT SERVICE\GuvFXBetaAgent`), PR #258/#260 (2026-08-03) proved the task-launched runner
  chain — `Agent → handoff.write_request → win.run_task → GvfxValidationRunner → runner → result → cleanup`
  — with the MT5 **GUI built** (`mdi_create_failed=0`) and the task **triggered**, using **synthetic**
  envelopes (returning `credential_unsealable`/`credential_scrub_unverified`). **No real credential, no
  broker login, no trade.**
- **Live credentialed broker validation: NEVER SUCCEEDED.** Every live attempt failed:
  `login_timeout` (130.22s, 2026-08-02) then `validation_runner_unavailable` (2026-08-03, a task-permission
  gap later fixed by PR #260). The production `BrokerAccountValidationAttempt` table contains **0 `HEALTHY`,
  0 `demo_ok`** across all time (4 rows total, all `UNAVAILABLE/validation_unconfigured`, all acct #12).
- **The single open item** (already stated correctly in `wp6a-pilot-recommendation.md` condition 1 and
  `ADR-0027` Phase 5) is: **one successful live demo `VALIDATE_LOGIN` on the host — not yet achieved.**

## Correction table (historical → evidence → correct wording → reason)

| # | Historical statement (location) | Evidence | Correct wording | Reason |
|---|---|---|---|---|
| 1 | `STATUS.md` 2026-08-02: *"Broker Login Validation Primitive: HOST CERTIFICATION COMPLETE — fully deployed & production-ready (DARK)"* | Cert was the **credential-free** primitive (GUI build via SYSTEM task, no credential); every live credentialed attempt failed; DB 0 live success | *"Broker-login validation **primitive (credential-free)** host-certified — lifecycle/task-trigger/GUI-build/cleanup proven with **synthetic** envelopes. A **live** credentialed broker login has **not** succeeded and is **not** production-ready."* | "HOST CERTIFICATION COMPLETE / production-ready" reads as a live-login certification |
| 2 | Summaries/handoff notes implying *"broker login certified / demo_ok / is_demo=true"* | `demo_ok`/`HEALTHY`/`is_demo=true` occur only in **mocked** tests; 0 live in prod DB | *"The `demo_ok`/`HEALTHY` path is proven by **mocked** tests (engineered correctness), **not** by any live broker login."* | Conflates mock-test success with live success |
| 3 | `PACKET_VALIDATION_ENV_PROVISIONING.md` (DRAFT): *"dedicated validation VM required"* | PR #258/#260 later proved the MT5 GUI **builds** and the runner **triggers** in a Session-0 task — partly superseding the Session-0-IPC rationale for the VM | *"A dedicated VM is **one** option. The existing task-launched host path is credential-free-certified; its **live-login** capability is **UNPROVEN, not disproven**. Decide after one live attempt on the existing host."* | The VM was proposed **before** the task-launch fix and the doc does not reconcile with it |
| 4 | `wp6a-certification.json` (verdict_scope_note) / `wp6a-pilot-recommendation.md` (condition 1) | Both explicitly state *"a live broker login has NOT been proven on the host"* | **No correction — accurate. Use as the anchor.** | Already correct |
| 5 | `ADR-0027` §Phase notes: *"Live certification against a real demo login remains gated (Phase 5)"* | Matches evidence | **No correction — accurate.** | Already correct |

## Classification of the historical certification (WS-B taxonomy)

`demo_ok`/`HEALTHY` origin = **MOCK** (engineered test correctness) + **IMAGE/PRIMITIVE HOST-CERT
(credential-free)**. It is **NOT** `HOST_CERTIFICATION` of a live `VALIDATE_LOGIN`, **NOT** a
`DIRECT_MT5_TEST` broker login, **NOT** a real customer-flow success.

## What this changes going forward

The next infrastructure decision must be made on this corrected record: the existing task-launched host
path is credential-free-certified and its live-login capability is untested since the PR #260 handoff-ACL
fix. The evidence-based next step is therefore **one bounded live credentialed `VALIDATE_LOGIN` on the
existing host** (gated on operator provisioning of the backend↔worker signing channel), **before** any
dedicated-VM build.
