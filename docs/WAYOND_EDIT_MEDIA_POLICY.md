# Wayond Edit / Media / Reply Policy — DESIGN (PROPOSED)

**Packet:** GFX-PKT-WAYOND-EDIT-AND-MEDIA-POLICY · **Status:** PROPOSED (design only — no
code changed; Nuno to ratify before any dispatcher change) · **Scope:** repo-only, no
listener, no deploy, no execution change, no E3.

## Why this exists

Real Wayond corpus V1 (21 messages) exposed that live traffic contains **edited**
messages and **media-bearing** (position-screenshot) updates. The current dispatcher
quarantines both *before parsing*, so valid text updates/signals inside them are
dropped. This doc decides the policy before the listener goes live.

## Grounding — what the code actually does today

From `backend/signal_intake/acquisition.py` (`_classify`, verified):

```
armed? → stale (None-date / age>window) → edit_date? → media? → empty? → parse → route
                                            │ QUAR      │ QUAR
route: SIGNAL & tradeable → INTAKEN → PendingSignalApproval (PENDING — human-gated)
       UPDATE             → SignalUpdate (recorded, NEVER acted)
       else               → QUARANTINED
```

Three facts that shape the policy:

1. **No entry is ever auto-traded.** A tradeable SIGNAL becomes a `PendingSignalApproval`
   in `PENDING_APPROVAL`, which requires the `review_signals` permission to approve
   (RBAC, fail-closed). The edit/media guards are a *second* safety layer on top of an
   already human-gated path — not the only thing preventing a trade.
2. **Updates are never acted on.** `_record_update` writes a `SignalUpdate` row; nothing
   consumes it to place or modify an order.
3. **Dedup is by `(provider, message_id)`** and runs *before* classification
   (`AcquiredMessage.objects.filter(...).first()`). So a message we have already ingested,
   if re-delivered *edited* under the same `message_id`, returns the existing row and is
   **NOT reprocessed** — the edit is silently ignored. `reply_to_message_id`, `edit_date`
   (bool) and `media` (bool) are already captured in `raw_payload`; `SignalUpdate` already
   stores `reply_to_message_id`.

## Challenges to the PM default view (where code evidence refines it)

- **The approval gate makes "quarantine media entries" over-conservative.** Because an
  entry only ever reaches a *human-reviewed* PENDING state, parsing a text-bearing media
  entry into that same queue introduces **no auto-trade risk**. So media can be downgraded
  from a hard parse-blocker to *evidence*, and text-bearing messages parsed — for both
  updates *and* entries — without weakening the trade gate. (Caveat: no media-bearing
  *entry* appears in the corpus; media in real traffic is on *updates* + the NFP warning.
  So the media-entry rule is precautionary.)
- **Scope item 10 ("edit changes entry/SL/TP → review") is only partly achievable in MVP.**
  Dedup-by-`message_id` means an edit to an *already-ingested* message is swallowed, so we
  cannot reliably detect a mid-life edit that changes entry/SL/TP. We can only quarantine a
  message that is *already edited the first time we see it*. Reliable edit-diff needs an
  edit-aware path (compare `edit_date`, write an amendment record) — **defer to a follow-up**.
- **Immutability constrains "supersede."** `.claude/rules/data.md`: raw evidence is
  written once, corrections are *new records, not overwrites*. So an edit must **never**
  mutate the original `AcquiredMessage`; it is always a new, linked record.
- **Media bytes must not enter the DB/Git** (`data.md`: no bulk/binary in Git). "Retain
  media as evidence" therefore means a **reference** (Telegram `file_id` + type/dims), not
  the image bytes. Byte retention (if ever wanted) is a configured external store, later.

## Recommended MVP policy (table)

| # | Case | Today | Recommended MVP | Auto-trade? |
|---|------|-------|-----------------|-------------|
| 1 | **Edited entry signal** | QUAR (if edited on first sight); IGNORED (edit to already-seen, via dedup) | Route to human review, flagged `edited`; **never** auto-intake | No |
| 2 | **Edited update** | QUAR / dropped | Record as `SignalUpdate` flagged `edited`; never acted | No |
| 3 | **Media entry (with text)** | QUAR (media guard) | Parse from text → INTAKEN → PENDING approval; media ref as evidence | No (approval-gated) |
| 4 | **Media update (with text)** | QUAR / dropped | Parse from text → `SignalUpdate`; media ref as evidence | No |
| 5 | **Reply-quoted update** | Recorded; `reply_to` stored but unused for linking | Record **and link** to the originating signal via `reply_to_message_id` | No |
| 6 | **Screenshot-only (no parseable text)** | QUAR | **Keep QUARANTINED** (no OCR in MVP); retain media ref | No |
| 7 | **Text + screenshot** | QUAR (media guard) | Parse text; media = evidence, not a blocker | No |
| 8 | **Media: block vs evidence** | Hard-blocks parsing | **Evidence metadata**; blocks only when text is empty/unparseable | — |
| 9 | **Edit: supersede / amend / quarantine original** | Edit to seen msg IGNORED; edit to new msg QUAR | **Never overwrite** original (immutable); edit = new linked record; entries→review, updates→record | No |
| 10 | **Edit changes entry/SL/TP** | Not detected (dedup swallows edits) | Human review REQUIRED; **reliable detection deferred** (needs edit-aware reprocessing) — MVP quarantines edited-on-sight only | No |

## Deliverable summary

- **What should CHANGE in the dispatcher** (only after ratification, as a separate packet):
  1. **Media → evidence, not a hard block.** Replace `if media: QUARANTINED` with: retain
     the media reference in `raw_payload`; parse when there is meaningful text; quarantine
     only screenshot-only / unparseable messages.
  2. **Edited handling.** Keep edited messages out of auto-intake for entries (→ review),
     record edited *updates* as `SignalUpdate` (flagged), and always as a **new** record
     (never overwrite the original).
  3. **Reply linking.** Use `reply_to_message_id` to associate an update with its
     originating signal's `AcquiredMessage` / provider message.
- **What should REMAIN quarantined:** screenshot-only / unparseable messages; empty text;
  non-armed provider; stale / indeterminate-date; edited **entry** signals (→ human
  review); unknown / malformed messages.
- **What should be EVIDENCE-ONLY (stored, never acted on):** media reference (`file_id`,
  type), `reply_to_message_id`, edit metadata (`edit_date`, an `edited` flag) — all in the
  immutable `raw_payload`; the `SignalUpdate` ledger; the re-entry price inside an "SL hit,
  N for re-entries" message.

## Risks

- **Relaxing the media guard widens the parse surface.** Mitigation: entries stay
  human-gated (PENDING approval); updates are never acted on; screenshot-only still
  quarantines. Net auto-trade risk: unchanged (still zero).
- **Edit-diff blind spot.** An edit that changes entry/SL/TP on an already-ingested message
  is invisible under current dedup — a reviewer could approve a now-stale price. Mitigation
  for MVP: surface the `edited` flag; **defer** true edit-diff to a follow-up packet and
  document the limitation loudly.
- **Caption ≠ signal.** A media message's caption might not be the full signal. Mitigation:
  entries still require human approval (reviewer sees text + media ref).
- **Reply target may be absent** (Telegram doesn't always populate `reply_to`). Mitigation:
  link when present; otherwise record unlinked (as today).

## Is a Nuno decision required?

**Yes.** This changes the dispatcher's fail-closed posture (Amber). Specifically Nuno must
ratify: (a) downgrade media from parse-blocker to evidence (parse text-bearing media);
(b) edited **entries** → surface-to-review vs strict-quarantine (recommend surface-with-flag
so real corrected signals aren't silently missed, since the human gate still protects the
trade); (c) accept the MVP limitation that mid-life edits to already-ingested messages are
not diffed (deferred). No implementation proceeds until this doc is ratified.

## Recommended implementation packet

**`GFX-PKT-WAYOND-EDIT-MEDIA-DISPATCHER`** (after ratification) — repo-only, additive
dispatcher changes implementing items 1–3 above, each fail-closed, with corpus + unit
tests and adversarial review. **No listener, no deploy, no execution change, no order.**
Edit-diff detection is explicitly out of that packet's scope (its own later packet).
