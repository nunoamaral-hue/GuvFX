# ADR-0095 — Vantage currency extraction ("Currency Type:") + currency from the correlated submission

- Status: Accepted (repository-only; DARK — command gated, not applied in production)
- Date: 2026-11-12
- Packet: GFX-WITHDRAWAL-VANTAGE-CURRENCY-CORRECTION-068
- Extends: ADR-0079 (Stream B lifecycle), ADR-0090 (Vantage reference-first), ADR-0092 (staff-gated correlation),
  ADR-0093 (staff-gated completion + Vantage completion parsing)

## Context

After ADR-0093 corrected production withdrawal #4 to `COMPLETED` (account `…5017`, amount `30`, duration 19s), the
authenticated owner dashboard still showed the **amount as `N/A`** and the **£30 was absent from the GBP total** — the
stored record has `amount=30` but `currency=''`. This is NOT cosmetic: the source data exists. The genuine Vantage
**submission** email ("Withdrawal Submitted") explicitly states, on their own labelled lines:

```
Currency Type:
 GBP
Amount:
 30.00
```

The parser missed the currency for two reasons (confirmed privacy-safe against the retained evidence — booleans/codes
only, no body into output):

1. **Text path** — `_extract_amount` reads a currency only when it is *inline* on the "Amount:" line (e.g. "GBP 30.00").
   Vantage puts the currency on a **separate `Currency Type:` field** (value on the next line), so the inline currency
   is blank.
2. **HTML path** — `_field_value(cells, _LBL_CURRENCY)` does a `fullmatch("currency")`, which does **not** match the
   label **"Currency Type"**.

The **completion** email states no currency at all, so the ADR-0093 repair (which re-parsed only the completion
candidate) stored a blank currency. The correct design is: the **submission** supplies amount + currency; the
**completion** supplies the completion state + timestamp; correlation combines them; completion must not discard
fields the correlated submission established.

## Decision

### 1. Parser — read the standalone currency field (per-broker opt-in)
- New `_CURRENCY_FIELD` matches a labelled `Currency[ Type]: <CCC>` field, allowing the value on the **next line**
  (a bounded whitespace run that may cross a single newline, like Vantage's "Amount:\n30.00"). The 3-letter code is
  validated by `_norm_currency`, so an unsupported token or mid-sentence prose ("…paid in GBP within 3 days") yields a
  **blank** currency — never invented.
- Applied in the text path **only when a broker opts in** (`currency_from_field`, Vantage only) **and** the inline
  amount-currency is absent. Pepperstone/Dominion/TradersWay are byte-for-byte unchanged.
- `_LBL_CURRENCY` is generalized to `currency(?:\s+type)?` so the HTML path also matches "Currency Type"; "Currency"
  still matches, so no existing broker regresses.

### 2. Repair — resolve the currency from the correlated submission; `--expected-currency`; currency-backfill
- `staff_completion` re-parses the correlation's candidates and resolves the currency from the **request/submission**
  evidence: a **single non-blank** currency across the candidates (conflicting non-blank ⇒ `CONFLICTING_REQUEST_CURRENCY`;
  absent ⇒ blank, never invented).
- New optional `--expected-currency`: when given, the resolved currency must be non-blank (`CURRENCY_NOT_EVIDENCED`
  otherwise) and equal it (`CURRENCY_MISMATCH` otherwise).
- A **currency-backfill** path: for an already-`COMPLETED`, account-resolved row whose `currency` is blank, the command
  (under an explicit `--expected-currency`) sets **only** the currency from the correlated submission evidence. It
  **never overwrites a non-blank currency** (a conflicting existing currency is left untouched — a no-op), and the full
  completion path's currency is likewise `(existing) or (submission currency)`.

### 3. Projection
Unchanged and already currency-safe: once `currency=GBP`, the row renders `30.00 GBP`, there is no "Account unresolved"
and no `N/A`, and `withdrawal_metrics.completed_amount_by_currency` sums it into the **GBP** bucket (so the owner's GBP
total becomes `15.00 + 30.00 = 45.00`); a blank-currency amount is still excluded (never summed or converted).

## Scope / non-goals
- Repository-only; **DARK**. No migration, no model/enum change. No production mutation, no deploy, no mailbox/cursor
  action, no money movement, no broker contact, no public-statistics change, no Copy Trading / TIM / MT5. No raw email
  or PII enters the repo, logs, fixtures, Notion or chat; fixtures are synthetic.
- Withdrawal #4 remains `COMPLETED` with a **blank** currency until a separately-authorized repair applies the backfill.

## Consequences — gated sequence (NOT executed here)
1. Sponsor **merge GO** (exact-head CI 5/5; auto-merge OFF).
2. **Rollback-first** backend deploy of the corrected parser + repair command.
3. **Dry-run** `complete_staff_withdrawal` for #4 with `--expected-currency GBP` — previews the currency backfill with
   no mutation.
4. Sponsor **GO for `--apply`** — backfills `currency=GBP` on #4 (reversible: set currency back to blank).
5. **Authenticated owner visual recheck** — expect `30.00 GBP` and a GBP total of `45.00`.
