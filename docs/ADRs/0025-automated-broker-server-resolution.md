# ADR-0025 — Automated broker-server resolution for provisioning login

- Status: Accepted (engineering)
- Date: 2026-08-02
- Related: ADR-0021 PR B (broker-login validation during provisioning), ADR-0023, ADR-0024

## Context

The intended beta customer journey is: the customer enters broker **login, server and password once**
through the frontend "Add Trading Account" form; GuvFX stores them; provisioning retrieves them and logs in
to MT5 with no further re-entry.

The frontend "Broker server name" field is persisted to the free-text `TradingAccount.broker_name` column
(the create serializer accepts **either** the normalised `broker_server` FK **or** `broker_name`; a beta
account that never links a normalised server keeps only `broker_name`). Customer Zero (prod `TradingAccount`
pk 12) is exactly this shape: `account_number=1302575`, `broker_name="IS6Technologies-Demo"`,
`broker_server` FK **null**, `password_enc` present, `is_demo=True`.

**Defect:** the provisioning login path (`_expected_login_server`) read **only** the normalised
`broker_server` FK's `server_name` and returned `server=None` when the FK was absent. Under
`PROVISIONING_REQUIRE_BROKER_LOGIN=1` that failed closed with `broker_server_required` — the customer's
already-submitted server (`broker_name`) was never consulted, so the automated journey could not log in
without manual normalisation.

## Decision

Introduce `resolve_broker_server(account) -> (server, reason)` and route the login path through it.
Deterministic precedence:

1. the normalised `broker_server` FK's `server_name` (admin-curated / canonical) when non-empty — **it wins
   unconditionally**;
2. otherwise the trimmed free-text `broker_name`;
3. otherwise `(None, "broker_server_missing")` — fail closed, non-retryable; never invent a server.

`_expected_login_server` now returns the resolved server (used by the `configure` step); the
`require_login` gate in `_start_and_verify` raises `broker_server_missing` when no server resolves. The
existing downstream checks are unchanged: the connected account's **login**, **server** and **demo/live
classification** are still verified exactly against the resolved values before RUNNING.

### Mismatch policy — FK wins deterministically (not fail-closed)

`broker_name` is **dual-use** free text: on a normalised account it may legitimately hold a broker *display*
name (e.g. "IS6 Technologies LTD"), not an MT5 server. So a normalised `server_name` and a populated
`broker_name` routinely and correctly **differ**. Failing closed on that disagreement would false-block
every normalised account carrying a display name (a real regression — surfaced by
`tests_verification.test_report_generated_on_verified_running_with_full_evidence`). The normalised binding
is authoritative by design; `broker_name` is a fallback used **only** when no FK exists. The rule is
therefore a documented deterministic precedence (FK wins), **strictly additive** to the prior FK-only
behaviour — no normalised account's resolution changes.

## Consequences

- The free-text beta journey (incl. Customer Zero) logs in automatically from stored data — no operator or
  customer re-entry, no production DB edit.
- No behaviour change for any account that already has a normalised `broker_server`.
- Removed the `broker_server_required` code; added `broker_server_missing` (a persisted account can never be
  both-absent — the `brokeridentity_present` DB constraint + `broker_name` stripping + the serializer all
  require a server — so this path is defense-in-depth, covered by in-memory unit tests).
- No credential is read, printed, logged, or persisted in plaintext by the resolver; it reads only non-secret
  server identifiers.
- Security note: the free-text server is a customer-controlled string, but it only ever reaches the signed
  `configure` op and the exact server-identity comparison; a login that connects to a different server than
  the resolved value still fails closed with `broker_identity_mismatch`.

## Alternatives considered

- **Fail closed on FK↔broker_name disagreement** — rejected: regresses normalised accounts with a display
  `broker_name`; the disagreement is expected, not an integrity error.
- **Normalise `broker_name` into a `BrokerServer` row at intake** — larger change, defers the fix, and still
  needs a resolver; out of scope for this bounded change (can be a later enhancement).
