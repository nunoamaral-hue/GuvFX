# SEC-CRYPTO-001 — Separate broker-credential encryption from Django application identity

**Status: OPEN — authorised as a future programme packet. Explicitly OUT OF SCOPE for B3P-2.**
**Do NOT silently fix this.** Changing the key derivation without a migration destroys decryptability of
every stored broker credential.

## The coupling

`backend/trading/crypto.py::_get_fernet` derives the MT5 broker-credential encryption key from the Django
application signing key when `GUVFX_FERNET_KEY` is unset:

```
GUVFX_FERNET_KEY  →  (if unset)  →  sha256(DJANGO_SECRET_KEY or SECRET_KEY)
```

This violates **Permanent Rule 3** (no service may substitute another's credential) in its highest-
consequence form: a *signing* key and an *encryption* key have different purposes, different blast radii and
different rotation cadences.

## Why it is dangerous

- **`DJANGO_SECRET_KEY` looks routinely rotatable.** Rotating it is normally a session/CSRF inconvenience.
  Here it would **silently render every stored MT5 broker credential undecryptable** — no error at rotation
  time, only failures later when a credential is next read.
- The coupling is invisible while `GUVFX_FERNET_KEY` happens to be set: exactly the "works because the
  values coincide" pattern that hid the validate-worker coupling until the 2026-07-22 rotation.
- It blocks `DJANGO_SECRET_KEY` rotation indefinitely, which is itself a security debt.

## Deliverables

1. **Dedicated broker-credential master key** — its own secret, own name, own rotation procedure, recorded
   in `docs/SECRET_INVENTORY.md` (replacing Gap 7).
2. **Migration of existing ciphertext** — decrypt with the current effective key, re-encrypt with the new
   master key, in a resumable, idempotent management command with a dry-run mode.
3. **Rollback plan** — including how to recover if the migration is interrupted mid-way (both keys must be
   available until the migration is verified complete).
4. **Rotation procedure** — how to rotate the master key thereafter (envelope encryption / key-id header on
   each ciphertext is the obvious enabler; decide explicitly).
5. **Recovery testing** — prove on a copy of production data that ciphertexts written before and after the
   migration both decrypt, and that an interrupted migration is recoverable.
6. **Operational documentation** — inventory row, runbook entry, and removal of the
   `sha256(DJANGO_SECRET_KEY)` fallback only *after* the migration is verified.

## Acceptance

- No code path derives an encryption key from `DJANGO_SECRET_KEY`.
- `DJANGO_SECRET_KEY` can be rotated without touching broker credentials (prove it in a test environment).
- Every stored credential decrypts before and after the migration.
- Inventory Gap 7 closed; `docs/POST_INCIDENT_REVIEW_BRIDGE_TOKEN.md` §7a updated.

## Sequencing

Runs **after** B3P-2. It touches stored customer/broker credentials, so it needs its own maintenance window,
its own adversarial review, and a verified backup before execution.
