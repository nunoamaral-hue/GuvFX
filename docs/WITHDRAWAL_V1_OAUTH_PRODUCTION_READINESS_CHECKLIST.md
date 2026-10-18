# Withdrawal V1 — Google OAuth Production Readiness (Operator Checklist) (§12)

**Status:** DOCUMENTED / BLOCKED on Google Cloud Console actions + Google verification (human/governance).
**Date:** 2026-10-18 · **Packet:** GUVFX_WITHDRAWAL_V1_WEEKEND_COMPLETION_R2

This is an operator checklist, not an engineering task. **No publishing-status change and no scope expansion is
performed or authorized by this packet** — those require explicit security/governance approval. The GuvFX mailbox
connection uses **`gmail.readonly` only**; the forbidden-scope guard (`_FORBIDDEN_SCOPES`) and a positive
`gmail.readonly` assertion already fail closed on any write-capable grant.

---

## 1. Root cause this checklist exists to close

The ingestion outage recurred because the Google Cloud project is in **External / "Testing"** publishing mode, where
a refresh token expires after **7 days**. Two consecutive expiries (2026-10-09 → 2026-10-16) each required a human
Google re-consent. The durable fix is to **publish the app to Production** and complete Google's **restricted-scope
verification** for `gmail.readonly`. Publishing status is **not host-determinable from the repo** — it is read and
changed only in the Google Cloud Console by the project owner.

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
- [ ] (PD-gated) add the `credential_obtained_at` proximity signal to warn *before* expiry.

## 6. Do-NOT list (hard gates)

- Do **not** change Google publishing status or add any scope without security/governance approval.
- Do **not** claim "customer-ready production OAuth" until the app is Published **and** Google verification is
  established **and** the >7-day token-lifetime validation (§2) has passed.
- Do **not** store any token in the DB, logs, prompts or Notion.

**Marker:** `OAUTH_PRODUCTION_READINESS` = **DOCUMENTED / BLOCKED** on Console publish + Google restricted-scope
verification (human). Engineering side (read-only scope, fail-closed config, encrypted store, reconnect UX,
monitoring hook) is complete/DARK.
