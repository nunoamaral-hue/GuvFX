# Wayond Parser Certification (GFX-PKT-WAYOND-PARSER-CERTIFICATION)

The **permanent regression suite** that proves the `wayond_v1` parser correctly
understands **real** Wayond traffic. Repo-only — no Telegram, no session, no
listener, no order. Runs entirely against a stored corpus of real messages.

## The rule: the corpus defines reality

`backend/signal_intake/wayond_corpus.json` holds **REAL, observed** Wayond messages
only — never synthetic. Every message Nuno supplies becomes a permanent entry, and
the test suite asserts the parser keeps classifying it correctly forever. Synthetic
examples are no longer sufficient once a real example exists.

## Taxonomy

Each message is certified against a human-labelled `expected_type`:

| type | meaning | safe route |
|------|---------|-----------|
| `ENTRY_SIGNAL` | new tradeable BUY/SELL (order + stop-loss) | intake for approval |
| `UPDATE` | TP-hit / move-SL follow-up | recorded, **not** traded |
| `WARNING` | news / NFP caution | quarantined |
| `CHATTER` | general discussion | quarantined |
| `STALE` | outside the freshness window | dropped |
| `QUARANTINED` | edited / media / empty / malformed | dropped |
| `UNKNOWN` | format the parser doesn't recognise | dropped (fail-closed) |

**Safety invariant (certified):** only an `ENTRY_SIGNAL` may become tradeable.
Every other type must not (fail-closed). The only **UNSAFE** verdicts are a *missed*
entry signal or a non-signal *read as* tradeable — both fail the suite.

## Fast intake: paste → stage → confirm → promote

Copy real Wayond messages and separate each with a line that is exactly `---`:

```
EURUSD | BUY 1.0850
❌ Stop Loss 1.0820
✅ TP1 1.0880
---
TP1 hit! +30 pips
---
Heads up: NFP at 13:30, trade carefully.
```

Optional **leading** `@directives` per block (a `@` inside the message body is
ignored — only leading lines count):

| directive | effect |
|-----------|--------|
| `@type: ENTRY_SIGNAL` | declare the ground-truth type → marks the entry **confirmed** |
| `@edit` / `@media` / `@reply` / `@stale` | set `meta` (edited / screenshot / reply / outside-window) |
| `@id: my-slug` | give the entry an explicit id |

**Stage** the paste into a reviewable draft (never the permanent corpus):

```bash
cd backend
pbpaste | python manage.py stage_wayond --out wayond_staging.json      # or --in paste.txt
```

It splits, classifies each message, **proposes** a type (the parser's *observed*
result), and flags entries that **need review** — anything unconfirmed, a proposed
trade, or a signal-shaped message the parser did **not** read as tradeable (a likely
parser gap).

**Confirm/correct** each entry in `wayond_staging.json`: set the true `expected_type`
and `"confirmed": true`. The `expected_type` is *your* ground truth — if it disagrees
with the parser's `observed`, certification will FAIL, which is exactly how a parser
gap surfaces.

**Promote** the confirmed entries into the permanent corpus (skips unconfirmed /
duplicate / bad-type, with reasons):

```bash
python manage.py promote_wayond --in wayond_staging.json
python manage.py certify_wayond        # re-certify
```

### Manual alternative
You can also hand-append an entry to `wayond_corpus.json` → `messages`
(`id`, `source`, `text` verbatim with `\n`, `expected_type`, `meta`, `notes`).

## Certification confidence

`certify_wayond` prints a confidence level from **real-message coverage**, honest by
construction:

- **LOW** — any UNSAFE/FAIL, **or** no real certified example of the safety-critical
  types (`ENTRY_SIGNAL`, `UPDATE`). A WARNING-only corpus proves the pipeline is
  *safe*, not that Wayond *signals* are understood → LOW.
- **MEDIUM** — `ENTRY_SIGNAL` + `UPDATE` certified, but some target types still
  missing.
- **HIGH** — every target type (`ENTRY_SIGNAL, UPDATE, WARNING, CHATTER, QUARANTINED,
  UNKNOWN`) certified with 0 unsafe / 0 fail.

## Run the certification report

```bash
cd backend
python manage.py certify_wayond            # or --corpus /path/to/other.json
```

Prints per-message `expected / observed / verdict / safety` and a summary. **Exits
non-zero** if anything is UNSAFE or FAILs, so it doubles as a CI gate.

## Regression suite

`backend/signal_intake/tests_wayond_certification.py` asserts the whole corpus
certifies clean, that no non-entry message is ever tradeable, and — a **drift
guard** — that the pure report classifier agrees with the *real* dispatcher
(`acquire_message`) on the safety group, so the report can never silently diverge
from the pipeline.

## Parser changes

The parser (`intelligence/telegram_source.py`) is changed **only** when a real
Wayond message requires it (e.g. a real entry signal the parser misses, or — worse —
a non-signal it wrongly reads as tradeable). Optimise for the real provider; never
generalise speculatively. Every parser change must keep the entire corpus certified.
