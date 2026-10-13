# Withdrawal V1 — Genuine Broker Evidence Certification

Packet: `WITHDRAWAL_V1_GENUINE_EVIDENCE_CERTIFICATION`. Baseline commit `a474f79`. Read-only continuous ingestion is
live (systemd `guvfx-broker-ingest.timer`, 5-min). **No production positive-classifier promotion; no financial action.**

## Evidence inventory (genuine, read-only; PII redacted)

Five genuine authenticated broker messages are retained as immutable evidence (all `auth=pass`):

| Blob | Broker (From) | Subject (redacted) | Current parser classification | Persisted event |
|---|---|---|---|---|
| #1 | TradersWay (`tradersway.com`) | "Please confirm your withdrawal request" | `WITHDRAWAL_CONFIRMATION_REQUIRED` / **INTERNAL_TRANSFER** | event#1 (frozen `UNKNOWN`, acct 46, owner None) |
| #2 | IS6 (`is6.com`) | JP deposit-completion notice | `DEPOSIT_COMPLETED` / **DEPOSIT** | none (quarantined) |
| #3 | IS6 (`is6.com`) | EN "completed deposit processing" | `DEPOSIT_COMPLETED` / **DEPOSIT** | none (quarantined) |
| #4 | IS6 (`is6.com`) | "Daily Confirmation" | **declined** (non-transactional) | none (quarantined) |
| #5 | IS6 (`is6.com`) | "Daily Confirmation" | **declined** (non-transactional) | none (quarantined) |

**There is NO genuine external-withdrawal template for either broker.** `Withdrawal = 0`.

### Findings
- **TradersWay "confirm your withdrawal request" → INTERNAL_TRANSFER** (current parser): a confirmation-required email
  is correctly classified NOT-external (the Sponsor's documented safety case). Genuine-evidence-backed + regression
  covered (`tests_tradersway_parser` / `tests_tradersway_real_template`).
- **Historical artifact (not a defect):** persisted event#1's category is frozen `UNKNOWN` — it was created
  2026-10-12, before the TradersWay classifier was refined to emit INTERNAL_TRANSFER for this template. BrokerEvent is
  append-only (immutable), so the historical row is unchanged. **Both `UNKNOWN` and `INTERNAL_TRANSFER` are excluded
  from the withdrawal projection**, so neither the old nor the new classification could ever enter withdrawal stats —
  no safety impact. event#1 `owner=None` is likewise a pre-identity-binding artifact.
- **IS6 deposits (EN + JP) → DEPOSIT**: genuine deposit-exclusion (a deposit can never enter the withdrawal
  projection). Covered by `tests_is6_parser`.
- **IS6 "Daily Confirmation" → declined**: genuine live-observed non-transactional template; retained as quarantined
  evidence, no event. New regression added (`test_daily_confirmation_statement_is_not_a_money_event`).

## Broker-by-broker certification matrix (evidence-based)

| Capability | IS6 | TradersWay |
|---|---|---|
| Identity verification | **PASS** (id 2 / acct 47) | **PASS** (id 1 / acct 46) |
| Live email routing | **PASS** (blobs 4,5 routed via the live worker; 2,3 IS6-routed) | **DEMONSTRATED** (blob 1 → tradersway parser); live-worker observation **PENDING** (no TW mail through the scheduled worker yet) |
| Immutable evidence capture | **PASS** (blobs 2–5, immutable triggers) | **PASS** (blob 1, immutable) |
| Positive external withdrawal | **BLOCKED** — no genuine template | **BLOCKED** — no genuine template |
| Internal-transfer exclusion | **PENDING** — no genuine IS6 internal-transfer email (structurally excluded) | **PASS** — genuine blob 1 → INTERNAL_TRANSFER (not external), regression-covered |
| Deposit exclusion | **PASS** — genuine blobs 2,3 → DEPOSIT (structurally excluded) | **PENDING** — no genuine TW deposit email |
| Duplicate protection | **PASS** — content-hash idempotency + DB uniq; observed no duplicates (supervised + 2 cycles) | **PASS** — same mechanism |
| Account attribution | **PASS (structural)** — owner-scoped; mismatch → UNRESOLVED; non-txn declined (no attribution needed) | **PASS (structural)** — owner-scoped; event#1 owner=None is a pre-binding artifact; identity now bound → a new TW txn email attributes to acct 46 |
| Adversarial certification | **PASS (non-external categories)**; external **PENDING** | **PASS (non-external categories)**; external **PENDING** |
| Production classifier approval | **NOT GIVEN** | **NOT GIVEN** |

## Classification distinctions (mapped to the existing model — no new enum values)
The logical categories (EXTERNAL_WITHDRAWAL / INTERNAL_TRANSFER / DEPOSIT / request / pending / rejected /
non-transactional / unresolved) map onto the APPROVED model: `TransactionCategory` (EXTERNAL_WITHDRAWAL /
INTERNAL_TRANSFER / DEPOSIT / UNKNOWN) × the open `event_type` lifecycle vocabulary (WITHDRAWAL_REQUESTED /
_CONFIRMATION_REQUIRED / _PROCESSING / _COMPLETED / _REJECTED / DEPOSIT_*). Non-transactional = parser declines (no
event; quarantined evidence). Ambiguous = UNKNOWN / UNRESOLVED. **No new persisted enum values were introduced.** The
withdrawal projection counts ONLY `EXTERNAL_WITHDRAWAL`; a request/pending/rejected/deposit/internal-transfer never
becomes a counted withdrawal.

## Evidence dependency (the blocked gate)
The positive `EXTERNAL_WITHDRAWAL` classifier for **both** brokers is **BLOCKED on genuine external-withdrawal
templates**, which do not exist yet. Per the packet, these arise only from **Sponsor-initiated** genuine withdrawals
(IS6 USD 10 after trading; TradersWay an amount of the Sponsor's choosing) — NOT authorized by this packet, and
GuvFX/Claude will never initiate/confirm/cancel/modify them. When such emails arrive, they are captured automatically
(read-only), and the positive classifier will be developed + certified against the actual templates, then promoted to
production only by a separate explicit programme decision.

## Safety state (unchanged)
Ingestion read-only; `BROKER_INTELLIGENCE_INGEST_ENABLED=1` in backend-only `broker_intel.env`; correlation / UX /
identity-verification flags OFF; all LIVE trading gates OFF (`MT5_ALLOW_LIVE` unset); accounts 25/35/36 preserved, 33
quarantine, 46/47 LIVE read-only monitoring. No positive-classification behaviour changed; the §7 classifier remains
disabled (empty `CERTIFIED_BROKERS`).
