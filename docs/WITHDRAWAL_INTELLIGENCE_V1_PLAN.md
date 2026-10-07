# Withdrawal Intelligence V1 — Implementation Plan (P0 after the LIVE-monitoring cert)

**Status:** PLAN (the required first return). Not yet implemented. Targets: **investor-demonstrable V1 in 5–7
working days; production pilot in 7–10**. These are engineering targets, not guaranteed dates — the single hard
external dependency is **real pilot-broker withdrawal email samples** (§6/§13); if that slips, the dates slip and
I will report it immediately. Scope is deliberately narrow (private per-account metrics from 1–2 broker templates);
WAYOND public scoring, deposits, KYC, support/leverage/promotions, universal parsing, LLM parsing, Cold-Boot V2 and
LIVE execution are **explicitly out of V1**.

Refines `docs/STREAM_D_LIVE_MT5_AND_BROKER_INTELLIGENCE_DESIGN.md §8–§12` and reuses
`docs/BROKER_EMAIL_IDENTITY_DESIGN.md`.

## 1. Work packages (each a governed DARK PR → tests → adversarial review → CI → deploy)
- **WP1 — `broker_intelligence` app + `BrokerEmailAlias`** (opaque `<opaque>@accounts.guvfx.com`, OneToOne per
  Model-A instance, DEMO+LIVE, nullable FK for Journey B, never reused/reassigned; mint/retire hooks; DARK flag
  `BROKER_EMAIL_IDENTITY_ENABLED`). Per `BROKER_EMAIL_IDENTITY_DESIGN.md`.
- **WP2 — Evidence + event/withdrawal schema** (`BrokerEvent` append-only, `Withdrawal` durable; raw-message
  evidence store, write-once + hashed, pointer not inline).
- **WP3 — Isolated ingestion service** (standalone worker; owns the mailbox credential; `To:`-alias → BrokerAccount
  resolve; raw-store; invoke parser; emit `BrokerEvent` via a narrow least-privilege path).
- **WP4 — Deterministic per-broker parser** (1–2 templates; versioned + auditable; unknown→unresolved).
- **WP5 — Correlation + metrics projection** (reference-id preferred, bounded fallback; read-only projection).
- **WP6 — Member-facing Withdrawals UX** (summary card + history table + evidence drill-down).
- **WP7 — Demo harness + hardening** (replay real samples end-to-end; governance/tests).

## 2. Schema / models (`broker_intelligence` app)
- **`BrokerEmailAlias`** — `alias_local` (opaque, all-rows unique), `trading_account` (OneToOne, PROTECT, NULLABLE),
  `user`, `origin_journey` (CONNECT_EXISTING|OPEN_NEW), `status` (PENDING|ACTIVE|RETIRED), `domain_at_creation`,
  timestamps. Immutable token + write-once bind.
- **`BrokerEvent`** (append-only evidence) — `trading_account`, `broker`, `source` (EMAIL first), `event_type`
  (string + registry, open vocab: REQUESTED/PROCESSING/APPROVED/COMPLETED/REJECTED/CANCELLED), `occurred_at`
  (broker time) vs `received_at` (ingest), `broker_reference_id?`, `amount?`/`currency?` (never fabricated),
  `evidence_ref` (pointer to stored raw message), `parser_name`/`parser_version`, `confidence`, `evidence_hash`,
  `correlation_status` (UNRESOLVED|CORRELATED|AMBIGUOUS). Raw evidence immutable; corrections = new rows.
- **`Withdrawal`** (durable, correlated) — `trading_account`, `broker_reference_id?`, `amount?`/`currency?`,
  `requested_at`, `completed_at?`, `status` (REQUESTED|PROCESSING|COMPLETED|REJECTED|CANCELLED|PENDING|UNRESOLVED),
  `requested_event`/`completed_event` FKs, `correlation_method` (reference_id|heuristic), `sample_provenance`.
- **No forward FK** from alias to event/withdrawal; events/withdrawal FK *to* the account (+ alias for attribution).

## 3. Ingestion-service boundary (isolation is load-bearing)
A **separately-deployed** worker (own container/service, own least-privilege DB role, own mailbox credential via
`core/credentials.resolve_secret`). It NEVER runs in Django request code, the trading workers, or the MT5 bridge,
and holds no execution/strategy/broker-login authority. **Trading availability never depends on email availability
and vice-versa** (the ingestion worker down ⇒ no new `BrokerEvent`s, but trading + the dashboard keep serving the
last projection). It writes `BrokerEvent`/evidence through a narrow internal API or a scoped shared-DB writer.

## 4. Email-provider / mailbox dependency
Alias namespace `accounts.guvfx.com`. **If its MX + catch-all mailbox are not yet live, do NOT block dev:** use a
GuvFX-controlled **pilot mailbox** (e.g. a dedicated Google Workspace address / catch-all) behind the SAME
`MailSource` ingestion abstraction, so swapping to the real domain later is a config change. The infra dependency
(MX/catch-all for `*@accounts.guvfx.com`) is tracked separately and is the gate for Journey-B (broker sends to the
alias) at pilot scale.

## 5. Pilot broker(s)
**1–2 only**, chosen because we can obtain genuine withdrawal-request + completion emails (e.g. TradersWay, IS6/
Pepperstone — whichever the Sponsor can produce real samples for). No universal parsing in V1.

## 6. Sample-email requirement (critical path)
Real (sanitised OK, structure preserved) samples per pilot broker: **WITHDRAWAL_REQUESTED + WITHDRAWAL_COMPLETED**
at minimum (ideally PROCESSING/REJECTED/CANCELLED too), WITH the broker reference id / amount / currency / timestamp
fields as the broker actually formats them. The deterministic parser is built and validated against these; **no
samples ⇒ no parser ⇒ no V1.**

## 7. Parser strategy
Deterministic per-broker template parsing (anchored regex / structured-field extraction), each **parser_name +
version** explicit + auditable, pinned to the broker + email type. Unknown/ambiguous email → `UNRESOLVED`/review,
never a fabricated event. **No LLM in the critical evidence path** (an LLM may later *suggest* a template offline,
but never decide an event). Positive + negative test fixtures per template.

## 8. Correlation strategy
**Preferred:** broker withdrawal/reference id → deterministic one `Withdrawal`. **Fallback (bounded heuristic):**
`(trading_account, amount, currency, temporal-proximity window)`. **Ambiguous** (multiple candidates / missing ref
or amount) → `UNRESOLVED`, never guess. **No completion email ≠ failure** → `PENDING` (completion-not-observed).
Status only advances on positive evidence.

## 9. UI deliverable (member-facing, private)
A Withdrawals section: summary (**Last / Average / Median / Fastest / Slowest / Completed N/M / Pending**, "Based on
N verified withdrawals") + a history table (date, amount, currency, requested, completed, duration, status) +
evidence drill-down that references stored evidence (no raw email bodies). Metrics are a **read-only projection**
(not computed in a request hot path). Private exact data only; **no public broker score**.

## 10. Day-by-day — investor-demonstrable V1 (5–7 working days)
- **D1:** WP1 — `broker_intelligence` app + `BrokerEmailAlias` + migration + mint/retire + DARK flag. *(Sponsor in
  parallel: pilot broker + first real samples.)*
- **D2:** WP2 — `BrokerEvent` + `Withdrawal` + evidence store (write-once, hashed).
- **D3:** WP3 — ingestion worker skeleton + `MailSource` abstraction (pilot mailbox) + alias→account resolver +
  raw-store; isolation proven (no trading coupling).
- **D4:** WP4 — deterministic parser for pilot broker (REQUESTED+COMPLETED) against the real samples + versioning +
  unresolved handling.
- **D5:** WP5 — correlation → `Withdrawal` + the full metrics projection.
- **D6:** WP6 — member Withdrawals UX wired to the projection.
- **D7:** WP7 — end-to-end demo (replay real samples → events → withdrawal → metrics → UX), hardening, governed
  review/tests. **→ investor-demonstrable.**

## 11. Day-by-day — production pilot (7–10 working days)
- **D8:** real inbound path — `accounts.guvfx.com` MX+catch-all if ready, else harden the pilot mailbox; a real
  pilot BrokerAccount bound to a minted alias (Journey A or B).
- **D9:** live receipt of a genuine broker withdrawal email → event → withdrawal → metrics on the real account;
  operator runbook + monitoring/alerting for ingestion health; idempotent reprocessing.
- **D10:** soak + sample-size accumulation + provenance/audit review → **production pilot** (still private metrics
  only; no public scoring).

## 12. Critical-path risks
1. **Real sample emails (biggest):** without genuine REQUESTED+COMPLETED samples the deterministic parser can't be
   built/validated — this is the long pole. *Mitigation:* Sponsor provides samples on D1–D2.
2. **Mailbox/MX infra** for `accounts.guvfx.com`. *Mitigation:* pilot mailbox behind the same abstraction; real MX
   is only needed for the production pilot's live-receipt step (D8).
3. **Pilot broker produces parseable, consistent emails** + the Sponsor can generate/obtain a real withdrawal.
4. **Isolated-service deploy** (new standalone worker + credential separation) on the prod estate.
5. **Thin real data** at pilot (few withdrawals) → metrics honest-but-small (surfaced as sample size).

## 13. Sponsor actions needed immediately
1. **Pick 1–2 pilot brokers** for which real withdrawal emails are obtainable.
2. **Provide real sample emails** (REQUESTED + COMPLETED, sanitised but structure/fields intact) for each — the
   single hard dependency for the parser.
3. **Confirm the mailbox approach now:** a GuvFX-controlled pilot mailbox to use immediately, and the owner/timeline
   for `accounts.guvfx.com` MX+catch-all (production-pilot gate).
4. **Confirm deploy target** for the isolated ingestion service (where it runs; its own credential store).
5. **Separately: create the Phase-A LIVE monitoring cert account** (support@, Add Account → Live) so that blocker
   closes in parallel.
