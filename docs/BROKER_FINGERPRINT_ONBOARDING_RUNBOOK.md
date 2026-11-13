# Runbook — Onboard a genuinely new broker WITHOUT a code change (ADR-0099)

This is the operator procedure for recognising a new broker's withdrawal emails using a **data-driven certified
fingerprint**, with no per-broker code. Every step is governed, audited and reversible. It is DARK by default: a
fingerprint only takes effect on the generic path (after a specific-parser miss) and only for a canary owner.

> **Boundaries.** This procedure never reads a mailbox, moves a cursor, contacts a broker, moves money, publishes a
> public statistic, or touches Copy Trading. It is Sponsor-gated for production: do not seed/certify/activate against
> production without explicit authorisation.

The command is `manage.py broker_fingerprint <action> [options]`. Reports are redacted (enums/counts/12-char hashes).

## 0. Precondition — the broker PROFILE exists

A fingerprint can only be certified against an **active** `BrokerProfile` whose `approved_sender_domains` cover the
fingerprint's authenticated sender domains. Confirm (or create, via the existing broker-profile flow) the profile
first — the fingerprint's sender authority is tied to it.

## 1. A discovery candidate appears

Tier-2 unknown-sender discovery (ADR-0089) surfaces a redacted `UnknownSenderDiscoveryCandidate` for a credible
withdrawal from a non-allowlisted sender. This is the signal that a new broker may be worth onboarding. **No automatic
action is taken** — it is review-only.

## 2. Review sanitized evidence

A staff reviewer inspects the SANITISED/SYNTHETIC evidence (never raw PII) and confirms this is a genuine broker
withdrawal email, not marketing/deposit/verification/internal-transfer.

## 3. Propose the fingerprint

Author a JSON spec (authenticated domains, subject/body indicators, `lifecycle_phrases`, amount/currency/reference
hints, `account_suffix_patterns`, `semantic_overrides`, and SYNTHETIC `positive_examples` / `negative_examples`).

```
manage.py broker_fingerprint propose --actor user:<id> --broker-profile-id <id> \
    --reason "<why>" --ticket "<TICKET>" --spec-file /path/to/spec.json
```

This creates a **PROPOSED** version (next version for that name). Nothing is active or certified.

## 4. Dry-run (validate)

```
manage.py broker_fingerprint validate --name <name> --fingerprint-version <v> --actor user:<id>
```

The redacted report shows, per SYNTHETIC example: would-match, lifecycle classification, extraction-field presence,
and totals for review-only / quarantined / ambiguous, plus any **domain conflicts** with existing certified
fingerprints. Iterate on the spec (re-propose a new version) until the positives are recognised and the negatives are
not. The fingerprint is marked **VALIDATED**.

## 5. Resolve conflicts

If the dry-run reports a `domain_conflicts` entry, two certified brokers would claim the same sender domain — resolve
it (narrow the domains, or retire the other) before certifying. Certification is **fail-closed** on any overlap.

## 6. Certify (version-bound, audited)

```
manage.py broker_fingerprint certify --name <name> --fingerprint-version <v> --actor user:<id> \
    --reason "<attestation>" --ticket "<TICKET>" --broker-profile-id <id>
```

Certify refuses unless: staff actor + reason + ticket; active broker profile; authenticated domains ⊆ the profile's
approved senders; no domain conflict; positive fixtures present; and the dry-run regression passes (every positive
recognised, no negative auto-accepted). On success the version becomes **CERTIFIED** with `certified_capability=True`
(now immutable in its matching fields — a later change needs a new version). It is **not yet active**.

## 7. Activate

```
manage.py broker_fingerprint activate --name <name> --fingerprint-version <v> --actor user:<id>
```

Only ONE active+certified version may exist per broker (DB-enforced) — deactivate the prior one first, or use
`rollback`. Once active, the detector will recognise this broker on the generic path for canary owners.

## 8. Bounded reprocessing (assess, then apply)

Reprocess the broker's ALREADY-RETAINED evidence so previously review-only messages are recognised. Always **dry-run
first**; it is owner-scoped, version-pinned, bounded, idempotent and duplicate-safe. A prior event that disagrees is
stopped for review; evidence that no longer resolves stays N/A. (Reprocessing never reads a mailbox or moves a
cursor.) The apply path is Sponsor-gated.

## 9. Subsequent emails enter the normal workflow

New emails from the now-certified broker are recognised by the detector (on the generic path, canary owners) and flow
through the normal withdrawal pipeline — a certified `EXTERNAL_WITHDRAWAL` only when the message carries positive
external-money-out evidence.

## 10. Rollback / deactivate if results are wrong

```
manage.py broker_fingerprint deactivate --name <name> --fingerprint-version <v> --actor user:<id> --reason "<why>"
manage.py broker_fingerprint rollback  --name <name> --target-version <prior_v> --actor user:<id> \
    --reason "<why>" --ticket "<TICKET>"
```

Deactivate retires the version (certified capability is retained as version-bound history). Rollback restores a prior
certified version as the live one in a single transaction. Both are audited; a certified version is never deleted.
