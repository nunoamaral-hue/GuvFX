# Withdrawal Intelligence V1 — Email / Multi-Mailbox Architecture (design)

**Status:** DESIGN (Sponsor packet 2026-10-08, Part B). Extends — does not replace — WP1/WP2/WP3a. No code in this
doc; the models/OAuth land as governed DARK PRs. Security-sensitive items (OAuth consent, token storage) are
human-gated.

## 1. Three separate identities (B2) — do NOT conflate
| Concept | What it is | Key fields | Relationships |
|---|---|---|---|
| **ConnectedMailbox** | a real mailbox GuvFX reads (e.g. a Gmail account) | owning user (FK), provider (`gmail`/`imap`/…), provider-stable mailbox id (e.g. Gmail historyId anchor / account id), primary email, connection status, **OAuth/credential reference** (pointer to an encrypted token in a separate store — never the token inline), granted scopes, created/updated, last_successful_sync, cursor/history state (Gmail `historyId` / IMAP UIDVALIDITY+UID) | `user` 1→N mailboxes |
| **BrokerEmailIdentity** | an email address registered *at a broker* | email address, owning user (FK), associated BrokerAccount lifecycle (FK, **NULLABLE** — pending/unbound), verified routing relationship (which ConnectedMailbox actually receives it — FK, nullable), status (PENDING/VERIFIED/UNRESOLVED/RETIRED), origin (BROKER_REGISTERED / GUVFX_ALIAS), created/updated | `user` 1→N identities; identity N→1 BrokerAccount (nullable); identity N→1 ConnectedMailbox (nullable, the mailbox that receives it) |
| **BrokerEmailAlias** (WP1, exists) | the permanent opaque GuvFX-controlled alias `<opaque>@accounts.guvfx.com` | unchanged — opaque, unique, non-enumerable, never reused/reassigned (Model-A) | OneToOne BrokerAccount instance |

A **ConnectedMailbox** is the *inbox GuvFX reads*; a **BrokerEmailIdentity** is an *address a broker sends to* (which
may be a pre-existing broker email OR a future GuvFX alias); a **BrokerEmailAlias** is *one specific GuvFX-minted
address*. One mailbox can receive mail for many identities; the GuvFX login email need not equal any of them.

## 2. Binding model (B1)
- `User` 1→N `ConnectedMailbox` (multiple mailboxes per user).
- `ConnectedMailbox` receives mail for 1→N `BrokerEmailIdentity` (e.g. a Gmail catch-all / `+` aliases / several
  broker registrations landing in one inbox).
- `BrokerEmailIdentity` → 0/1 `BrokerAccount` lifecycle (nullable until onboarded; then bound).
- Ingestion resolves an inbound message's `To:`/`Delivered-To:` → `BrokerEmailIdentity` → BrokerAccount (reusing the
  WP3a resolver pattern, now identity-aware as well as alias-aware). Ambiguous → UNRESOLVED (never guess — B4).

## 3. Six IS6 registration-email mapping (B4) — live read 2026-10-08 (read-only)
| Registration email | GuvFX user? | Mapped BrokerAccount | Disposition |
|---|---|---|---|
| guvfx02@gmail.com | none | none | **PENDING / UNBOUND** identity |
| support@guvfx.com | user 29 | accts 25 (IS6-hosted 1302587), 35, 36, 46 exist under this user, but none proven to be the *IS6 registration* tied to this email | **UNRESOLVED** binding (needs verified recipient + broker acct number) |
| nrfda1111@googlemail.com | none | none | **PENDING / UNBOUND**; may equal the @gmail form — verify via provider identity, not string |
| nrfda1111@gmail.com | none | none | **PENDING / UNBOUND**; see above |
| approvals@guvfx.com | none | none | **PENDING / UNBOUND** |
| guvfx01@gmail.com | none | none | **PENDING / UNBOUND** (and the chosen **pilot mailbox**) |

**Finding:** GuvFX holds exactly **one** IS6-broker TradingAccount today (id 1, demo, owner `nuno.amaral@live.com`) —
not six. The six broker-registration emails are **not** represented as six GuvFX BrokerAccount rows. Per B4, all six
are preserved as **unbound/pending BrokerEmailIdentity** records; binding to a BrokerAccount happens only on verified
(recipient identity + broker identity + account number) evidence, else UNRESOLVED. The `googlemail`≡`gmail`
same-mailbox question **cannot** be answered from GuvFX data — it requires the Gmail OAuth identity/consent (B).

## 4. Gmail OAuth plan (B3/B6/#8)
- **Flow:** OAuth 2.0 Authorization-Code with offline access (refresh token). **Scope: `gmail.readonly` only** (read;
  no send, no modify, no delete). Consent screen authorised by the **user** (human-gated — Claude cannot grant it).
- **Credential boundary:** a Google Cloud OAuth *client id/secret* (app-level, one per deploy) resolved via
  `core.credentials.resolve_secret`; per-user **refresh tokens** stored **encrypted** (Fernet, same as existing
  credential stores) in the ingestion service's own store, referenced by `ConnectedMailbox.credential_ref`. **Never**
  in Git/logs/normal config; never the user's Gmail password.
- **Isolation:** the ingestion service holds mailbox tokens ONLY — no trading/MT5 credential, no execution authority,
  no send/delete scope. Per-user tokens; no cross-user access; independent revocation (delete the `ConnectedMailbox`
  + revoke at Google); evidence retained after disconnect (append-only).
- **Sync:** Gmail `users.history.list` from the stored `historyId` cursor (incremental, idempotent); first sync seeds
  the cursor. Read bodies via `users.messages.get` (raw) → EvidenceStore (WP2) → BrokerEvent (WP2) → Withdrawal.
- **First connector = Gmail only** (B7); Outlook/Yahoo/IMAP later behind the same `MailSource` (WP3a) abstraction.

## 5. Security controls (B6) — all mandatory
mailbox-ownership verification; user-scoped consent; read-only scope; independent revocation; encrypted token
storage; no cross-user mailbox access; no trading/MT5 credential access; no execution authority; no delete/send
authority; historical evidence retained after disconnect; no shared credentials between users.

## 6. Sequencing (B7)
WP1✓ WP2✓ WP3a✓(merged+DARK-deployed). Next governed DARK PRs, in order:
1. **Multi-mailbox foundation** — `ConnectedMailbox` + `BrokerEmailIdentity` models + identity-aware resolver
   (additive; no live-flow touch; backfill the six emails as PENDING identities).
2. **WP3b** — standalone ingestion worker + Gmail `MailSource` (OAuth, read-only), gated by
   `BROKER_INTELLIGENCE_INGEST_ENABLED` (OFF) + per-mailbox connection state.
3. **WP4** — deterministic parser for the chosen pilot broker, validated against **real** REQUESTED+COMPLETED
   samples (synthetic fixtures only for labelled dev tests).
4. **WP5/WP6** — correlation + metrics projection + member UX (sample-independent; proceeds in parallel with OAuth).

No email-ingestion failure may impair trading (ingestion is a separate service with no trading authority).
