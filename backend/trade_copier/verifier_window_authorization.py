"""trade_copier.verifier_window_authorization — the host-private / backend-public authorization contract that
permits OPENING the INGEST verifier window for the governed #46->#48 demonstration WITHOUT transferring any
symmetric secret across the host<->backend boundary (GFX-COPIER-OWNER-DEMO-ASYM-VERIFIER-172, ADR-0129).

WHY THIS EXISTS
---------------
ADR-0128 shipped the owner-demo execution wiring but deliberately left the verifier-window opener UNPROVISIONED,
because the interim path would have had to stream the standing SYMMETRIC ingest key host->backend (a third
exposure surface for the shared secret). This module implements the preferred **asymmetric** design as a
standalone, fully-tested CONTRACT: the HOST holds a PRIVATE Ed25519 signing key and signs a bounded
"open this window" attestation; the BACKEND holds only the corresponding PINNED PUBLIC key and verifies it. No
secret bytes ever cross the boundary, and a backend compromise can forge nothing.

IDENTITY SEPARATION
-------------------
The pinned public identity here is a **Copy-Trading** identity, resolved from the Copy-Trading trust registry
(``COPIER_WINDOW_OPEN_TRUST``) — it is NEVER the TIM operational release-signing trust store. The two trust
domains are kept strictly separate (ADR-0070 TIM signing vs ADR-0129 copier window authorization); this module
imports nothing from the TIM lane.

WHAT IT BINDS
-------------
A valid attestation is accepted ONLY when it is signed by a pinned public key AND binds, exactly, to: the intended
tenant OWNER, the governed master #46 -> follower #48 route, the ``INGEST`` purpose, and a bounded window whose
not-after outlives the live authorization. Expiry, single-use replay protection (nonce), revocation (un-pinning
the public key) and teardown (window close spends the authorization) are all defined below. Everything is
fail-closed and value-blind: the verdict is reason codes + booleans + bound facts only — never a key, a signature,
or any secret.

PRODUCTION STATE: DARK. The Copy-Trading trust registry is EMPTY by default, so every verification fails
``INVALID_IDENTITY`` and no opener can be provisioned. Wiring a real host opener to this contract is a separate,
Sponsor-gated host rollout (see ADR-0129 "Integration gap"). This module is exercised only through injected
providers + synthetic keys in tests.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Optional

from django.conf import settings

PURPOSE_INGEST = "INGEST"

# Stable, value-blind reason codes.
R_VERIFIED = "verified"
R_MALFORMED = "MALFORMED_ATTESTATION"
R_INVALID_IDENTITY = "INVALID_IDENTITY"          # key_id not pinned / revoked / unknown
R_BAD_SIGNATURE = "BAD_SIGNATURE"
R_PURPOSE_MISMATCH = "PURPOSE_MISMATCH"
R_ROUTE_MISMATCH = "ROUTE_MISMATCH"
R_TENANT_MISMATCH = "TENANT_MISMATCH"
R_ATTESTATION_EXPIRED = "ATTESTATION_EXPIRED"    # issued_at too old / in the future
R_WINDOW_EXPIRY_INSUFFICIENT = "WINDOW_EXPIRY_INSUFFICIENT"  # not-after does not outlive the authorization
R_REPLAYED = "REPLAYED"

# Default: an attestation must have been issued within this many seconds (the opener signs immediately before use).
DEFAULT_MAX_ISSUE_AGE_S = 120
# A small tolerance for clock skew on the "issued in the future" check.
_FUTURE_SKEW_S = 30


@dataclass(frozen=True)
class WindowOpenAttestation:
    """The canonical, host-signed payload authorizing ONE INGEST window open. All fields are bound by the
    signature; none is a secret. ``key_id`` names which pinned public key signed it (for rotation/revocation)."""
    key_id: str
    owner_id: int
    master_account_id: int
    follower_account_id: int
    purpose: str
    issued_at_epoch: int
    window_not_after_epoch: int
    nonce: str

    def canonical_bytes(self) -> bytes:
        """Deterministic serialization the host signs and the backend verifies. Sorted keys + compact separators
        so host and backend agree byte-for-byte; the signature therefore binds EVERY field."""
        return json.dumps({
            "key_id": str(self.key_id),
            "owner_id": int(self.owner_id),
            "master_account_id": int(self.master_account_id),
            "follower_account_id": int(self.follower_account_id),
            "purpose": str(self.purpose),
            "issued_at_epoch": int(self.issued_at_epoch),
            "window_not_after_epoch": int(self.window_not_after_epoch),
            "nonce": str(self.nonce),
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")


class NonceStore:
    """Single-use replay guard. ``consume`` returns True exactly once per (scope, nonce) and False on every repeat.
    The default is in-memory (tests / single process); a production opener needs a DURABLE store (DB/cache) — a
    documented integration gap (ADR-0129). Fail-closed: a store error is treated by the caller as a replay."""

    def __init__(self):
        self._seen: set = set()

    def consume(self, *, scope: str, nonce: str, now_epoch: int) -> bool:  # noqa: ARG002 — now kept for TTL impls
        key = (str(scope), str(nonce))
        if key in self._seen:
            return False
        self._seen.add(key)
        return True


def _copier_trust_public_key(key_id: str) -> Optional[bytes]:
    """Resolve a PINNED Copy-Trading window-open public key (raw 32-byte Ed25519) by ``key_id`` from the
    ``COPIER_WINDOW_OPEN_TRUST`` setting (``{key_id: base64_raw_public_key}``). Returns None when the key_id is not
    pinned — i.e. unknown OR revoked (revocation = remove the entry). DARK by default: the setting is EMPTY, so
    every key_id resolves to None and verification fails INVALID_IDENTITY. This registry is Copy-Trading-ONLY and
    never consults the TIM release-signing trust store."""
    import base64
    trust = getattr(settings, "COPIER_WINDOW_OPEN_TRUST", {}) or {}
    b64 = trust.get(str(key_id))
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64)
    except Exception:  # noqa: BLE001 — a malformed pin is treated as absent (fail closed)
        return None
    return raw if len(raw) == 32 else None


def _ed25519_verify(public_key_raw: bytes, signature: bytes, message: bytes) -> bool:
    """Verify an Ed25519 signature with the backend's ``cryptography`` primitive. Fail-closed: any error ⇒ False;
    never raises, never logs, never carries key/sig bytes into an exception."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(bytes(public_key_raw)).verify(bytes(signature), bytes(message))
        return True
    except Exception:  # noqa: BLE001
        return False


def _verdict(reason: str, **extra) -> dict:
    d = {"ok": reason == R_VERIFIED, "reason": reason}
    d.update(extra)
    return d


def verify_window_open_authorization(
    attestation: WindowOpenAttestation,
    signature: bytes,
    *,
    expected_owner_id: int,
    expected_master_id: int,
    expected_follower_id: int,
    authz_not_after_epoch: int,
    now_epoch: int,
    public_key_provider: Callable[[str], Optional[bytes]] = _copier_trust_public_key,
    nonce_store: Optional[NonceStore] = None,
    expected_purpose: str = PURPOSE_INGEST,
    max_issue_age_s: int = DEFAULT_MAX_ISSUE_AGE_S,
) -> dict:
    """Verify a host-signed INGEST window-open authorization against a PINNED public identity, fail-closed and
    value-blind. Returns ``{"ok", "reason", ...}``. The ordering checks structure -> pinned identity -> signature
    -> binding (purpose/route/tenant) -> expiry (attestation + window) -> single-use replay, so a verdict never
    depends on a field the signature did not cover.

    Providers are INJECTED (``public_key_provider`` = the Copy-Trading trust registry by default; ``nonce_store``
    = a durable single-use guard). No secret is read, returned or logged — only the public key is consulted."""
    ns = nonce_store if nonce_store is not None else NonceStore()

    # 1) Structure — every bound field must be present and well-typed (a truncated attestation fails closed).
    try:
        key_id = str(attestation.key_id)
        owner_id = int(attestation.owner_id)
        master_id = int(attestation.master_account_id)
        follower_id = int(attestation.follower_account_id)
        purpose = str(attestation.purpose)
        issued_at = int(attestation.issued_at_epoch)
        not_after = int(attestation.window_not_after_epoch)
        nonce = str(attestation.nonce)
        sig = bytes(signature)
    except (TypeError, ValueError):
        return _verdict(R_MALFORMED)
    if not key_id or not nonce or not sig:
        return _verdict(R_MALFORMED)

    # 2) Pinned identity — unknown OR revoked key_id ⇒ fail closed BEFORE any signature math.
    pub = public_key_provider(key_id)
    if not pub:
        return _verdict(R_INVALID_IDENTITY)

    # 3) Signature over the canonical payload (binds every field).
    if not _ed25519_verify(pub, sig, attestation.canonical_bytes()):
        return _verdict(R_BAD_SIGNATURE)

    # 4) Binding — purpose, route, tenant must match EXACTLY.
    if purpose != expected_purpose:
        return _verdict(R_PURPOSE_MISMATCH)
    if master_id != int(expected_master_id) or follower_id != int(expected_follower_id):
        return _verdict(R_ROUTE_MISMATCH)
    if owner_id != int(expected_owner_id):
        return _verdict(R_TENANT_MISMATCH)

    # 5) Expiry — the attestation must be freshly issued (not stale, not implausibly future), and the window it
    #    authorizes must OUTLIVE the live authorization (mirrors covers_live_authorization) and be in the future.
    if issued_at < now_epoch - int(max_issue_age_s) or issued_at > now_epoch + _FUTURE_SKEW_S:
        return _verdict(R_ATTESTATION_EXPIRED)
    if not_after <= now_epoch or not_after < int(authz_not_after_epoch):
        return _verdict(R_WINDOW_EXPIRY_INSUFFICIENT)

    # 6) Single-use replay — consume the nonce LAST (only a fully-valid attestation spends a nonce). A store error
    #    is treated as a replay (fail closed).
    try:
        fresh = ns.consume(scope=f"copier-window-open:{master_id}->{follower_id}", nonce=nonce, now_epoch=now_epoch)
    except Exception:  # noqa: BLE001
        fresh = False
    if not fresh:
        return _verdict(R_REPLAYED)

    return _verdict(R_VERIFIED, key_id=key_id, window_not_after_epoch=not_after,
                    bound={"owner_id": owner_id, "master_account_id": master_id,
                           "follower_account_id": follower_id, "purpose": purpose})
