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

## Add a real message to the corpus

Append an entry to `wayond_corpus.json` → `messages`:

```json
{
  "id": "buy-eurusd-2026-07-10",
  "source": "wayond-channel-copied-2026-07-10",
  "text": "EURUSD | BUY 1.0850\n❌ Stop Loss 1.0820\n✅ TP1 1.0880",
  "expected_type": "ENTRY_SIGNAL",
  "meta": {"is_edit": false, "media": false, "is_reply": false, "stale": false},
  "notes": "real entry signal"
}
```

- `text` — the message **verbatim** (use `\n` for line breaks).
- `expected_type` — one of the taxonomy values above (the certified truth).
- `meta` — set `is_edit` / `media` / `stale` when the real message was edited, a
  screenshot/image, or arrived outside the window.

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
