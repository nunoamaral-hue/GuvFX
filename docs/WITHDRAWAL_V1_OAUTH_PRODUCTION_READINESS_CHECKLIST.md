# Withdrawal V1 — Google OAuth Production Readiness (Operator Checklist) (§12)

**Status:** DOCUMENTED / BLOCKED on Google Cloud Console actions + Google verification (human/governance).
**Date:** 2026-10-18 · **Packet:** GUVFX_WITHDRAWAL_V1_WEEKEND_COMPLETION_R2

This is an operator checklist, not an engineering task. **No publishing-status change and no scope expansion is
performed or authorized by this packet** — those require explicit security/governance approval. The GuvFX mailbox
connection uses **`gmail.readonly` only**; the forbidden-scope guard (`_FORBIDDEN_SCOPES`) and a positive
`gmail.readonly` assertion already fail closed on any write-capable grant.

---

## 1. Root cause this checklist exists to close

The ingestion outage is attributed to the Google Cloud project being in **External / "Testing"** publishing mode,
where a refresh token expires after **7 days**. The supporting evidence is a **single observed token expiry**: the
mailbox was consented on **2026-10-09** and refresh began failing (`invalid_grant`) around **2026-10-16** — about
seven days later — after which a human Google re-consent (2026-10-17) restored ingestion. The 7-day mechanism is
therefore **inferred** from that one observation plus Google's documented Testing-mode behaviour; this is **one
observed expiry, not two** (2026-10-09 is the consent date, not a second expiry), and the project's publishing status
has not been confirmed in the Google Cloud Console. The durable fix is to **publish the app to Production** and
complete Google's **restricted-scope verification** for `gmail.readonly`. Publishing status is **not host-determinable
from the repo** — it is read and changed only in the Google Cloud Console by the project owner.

> **Evidence labelling (pkt-149).** Two further together-expiries were observed since (both pilot grants failed
> together on **2026-11-08** and again on **2026-11-15**, each ~7 days after the prior re-consent), consistent with the
> same mechanism. Classify the claims so inference is never presented as confirmed fact:
> - **VERIFIED (from Google's published documentation):** an OAuth app in **Testing** publishing status issues refresh
>   tokens that **expire in 7 days**. This is Google's documented Testing-mode behaviour.
> - **HIGH-CONFIDENCE INFERENCE (pending console confirmation):** that THIS project is currently in Testing status and
>   that the repeated together-expiries are caused by it. The repeated 7-day-spaced together-failures strongly support
>   it, but the publishing status itself is confirmable **only** in the Google Cloud Console (§2, first checkbox) — do
>   not record "the app is in Testing" as established until that console read is done.

---

## 2. Operator checklist (Console — project owner only)

- [ ] **Confirm current publishing status** (Console → APIs & Services → OAuth consent screen). Record: Testing vs
      Production, user type (External), and the test-user list. (Do not screenshot secrets.)
- [ ] **OAuth consent screen completeness** for verification: app name, user-support email, app logo, application
      home page, privacy-policy URL, terms URL, **authorized domains** (guvfx.com), developer contact.
- [ ] **Scope registration** = exactly `https://www.googleapis.com/auth/gmail.readonly`. No additional scope.
- [ ] **Restricted-scope justification** prepared (why read-only Gmail: broker withdrawal-notification monitoring;
      data handling; no human reads mailbox content; retention; deletion on disconnect).
- [ ] **CASA security assessment** — budget for it; restricted Gmail scopes typically require a Tier-2 CASA
      assessment by an authorized third party. (Cost/scheduling is a governance decision — **no purchase is
      authorized here**.)
- [ ] **Publish to Production** (moves off the 7-day test-token expiry). Submit for verification.
- [ ] **Post-publish token-lifetime validation** — confirm a connected mailbox's refresh token survives > 7 days
      (the former failure window) before declaring customer-ready.
- [ ] **Redirect URI parity** — the production redirect URI
      (`https://api.guvfx.com/api/broker-intelligence/mailboxes/callback/`) is registered and matches
      `GMAIL_OAUTH_REDIRECT_URI` exactly.
- [ ] **Return-journey destination** — `BROKER_MAILBOX_CONNECT_RETURN_URL` set to the fixed GuvFX Profile URL
      (default `/profile`); never request-derived (open-redirect-safe by construction).

## 3. Secrets / config (operator, out of Git)

- [ ] `GMAIL_OAUTH_CLIENT_ID`, `GMAIL_OAUTH_CLIENT_SECRET`, `GMAIL_OAUTH_REDIRECT_URI` set in the secret store
      (settings→env), never in the repo (security RULE: no secrets in Git). Missing ⇒ the endpoints fail closed with
      `oauth_not_configured` (503).
- [ ] `BROKER_INTELLIGENCE_CREDENTIAL_KEY` (Fernet) present; tokens are stored **encrypted**, the DB keeps only a ref.
- [ ] Rotation starts from `docs/SECRET_INVENTORY.md` (security RULE 4); each consumer enumerated before any change.

## 4. Consent-recovery UX (shipped, DARK)

- [ ] The member Connected-Email-Accounts panel (PR #506) already provides a **targeted reconnect** (same-account
      re-consent that preserves cursor/id/bindings and rotates the credential) and an explicit disconnect — so a
      post-expiry recovery is a one-click member action once the feature is armed.

## 5. Monitoring hook

- [ ] Wire `broker_mailbox_health --exit-nonzero-on-degraded` (see
      `WITHDRAWAL_V1_MAILBOX_MONITORING_CLOSEOUT.md`) so a token expiry surfaces as an alert, not a silent stall.
- [ ] **(pkt-149 / ADR-0113)** wire the sharper early-warning signals so a re-consent is distinguished from a blip and
      a dead worker is caught: `broker_mailbox_health --oauth-health --exit-nonzero-on-reconsent
      --exit-nonzero-on-synchronizer-stalled` (the DARK `ops/monitoring/guvfx-broker-mailbox-health.*` timer is the
      delivery vehicle; do **not** `systemctl enable` it under this packet). The same seven-status model is exposed to
      operators at `GET /api/broker-intelligence/operator/mailbox-health/` (staff-only + DARK gate
      `BROKER_MAILBOX_OPERATOR_HEALTH_ENABLED`) and in the Operations console → **Mailbox Health**. See
      `WITHDRAWAL_V1_MAILBOX_OAUTH_OBSERVABILITY.md` for the model, statuses and thresholds.
- [ ] (PD-gated) add the `credential_obtained_at` proximity signal to warn *before* expiry.

## 6. Do-NOT list (hard gates)

- Do **not** change Google publishing status or add any scope without security/governance approval.
- Do **not** claim "customer-ready production OAuth" until the app is Published **and** Google verification is
  established **and** the >7-day token-lifetime validation (§2) has passed.
- Do **not** store any token in the DB, logs, prompts or Notion.

## 7. Testing → Production publishing runbook (pkt-149 additions)

These extend §2 for the actual publish. They are **Console / operator actions**; no step is performed by the pkt-149
packet, which is repository-only. Do each in order; abort on any step whose precondition is not met.

- [ ] **Confirm the project + client without exposing the secret.** Identify the Google Cloud project and OAuth client
      that back `GMAIL_OAUTH_CLIENT_ID` (names only). Confirm the client id's last characters match the configured
      value **without printing `GMAIL_OAUTH_CLIENT_SECRET`** — never echo the secret; use a value-blind presence check
      (e.g. `scripts/safe_container_env.py GMAIL_OAUTH_CLIENT_ID GMAIL_OAUTH_REDIRECT_URI`, which redacts any
      secret-named variable by NAME and never dumps the environment).
- [ ] **Internal vs External user type.** The project is **External** (members authorize their own personal Gmail).
      "Internal" is **not** an option unless every mailbox owner is in the same Google Workspace org (they are not), so
      Internal would silently exclude real members — do not switch to Internal to avoid verification. External +
      Published + restricted-scope verification is the only correct path.
- [ ] **Synthetic-mailbox dry run FIRST.** Before (or immediately after) publishing, connect a **non-production,
      operator-owned test Gmail** through the real flow and confirm it reaches `CONNECTED`/`HEALTHY`. Never use a real
      member mailbox to test a publish.
- [ ] **Test-user cleanup.** Once Published + verified, the Testing-mode test-user allowlist no longer gates access;
      review and remove stale test users from the consent screen so the Testing allowlist is not mistaken for an
      access-control surface. (Removing a test user does **not** revoke an already-granted member token.)
- [ ] **Do NOT revoke existing grants.** Publishing Testing → Production does **not** require revoking connected
      mailboxes and must not. Do not delete the OAuth client, rotate the client secret, or remove members' grants as
      part of publishing — any of these would force a mass re-consent. If a client-secret rotation is ever needed, it
      is a **separate** change that starts from `docs/SECRET_INVENTORY.md` (security RULE 4).
- [ ] **Confirm mailboxes stay owner-bound + cursors preserved (post-publish).** Publishing changes nothing in the
      GuvFX DB. Confirm value-blind, from the repo side, that each `ConnectedMailbox` keeps its owner (`user_id`),
      provider-stable id and `cursor_state` (never printed — confirm **presence/length**, not the value) and that
      `generation` did **not** change (a generation bump means a reconnect happened, which publishing must not cause):
      `GET /api/broker-intelligence/operator/mailbox-health/` and `broker_mailbox_health --oauth-health` both report
      per-mailbox `cursor_present` and the masked identity without exposing the cursor or address.
- [ ] **>7-day lifetime validation** (as §2) — the real success test: a grant obtained AFTER publish survives past the
      former 7-day failure window.
- [ ] **Rollback / abort conditions.** Publishing is reversible at the Console (switch the app back to Testing) and
      has **no code/deploy component** — so the pkt-149 rollback is trivial (there is nothing in this packet to revert
      on the host). Abort the publish and keep Testing if: the restricted-scope justification or CASA assessment is not
      ready; the consent screen is incomplete (missing privacy/ToS URL, authorized domain, logo); or the redirect-URI
      parity check (§2) fails. A mid-publish abort leaves existing grants untouched (they were never revoked).
- [ ] **Record evidence without secrets.** Capture the publishing status, user type, scope list, authorized domains and
      the >7-day validation **outcome** — never a token, authorization code, client secret, or a screenshot that
      includes any of them. Redact by category (security RULE: redact in evidence).

**Marker:** `OAUTH_PRODUCTION_READINESS` = **DOCUMENTED / BLOCKED** on Console publish + Google restricted-scope
verification (human). Engineering side (read-only scope, fail-closed config, encrypted store, reconnect UX,
monitoring hook, and the pkt-149 value-blind health/alerting + operator projection) is complete/DARK.
