"""trade_copier.ingest_event_signing — the VERSIONED, DOMAIN-SEPARATED, host-private / backend-public Ed25519
construction for authenticating a SIGNED INGEST EVENT, replacing the symmetric HMAC envelope on an explicitly
configured future path (GFX-COPIER-INGEST-EVENT-ED25519-173, ADR-0130).

WHY THIS EXISTS
---------------
The signed fast-path event-ingestion endpoint (ADR-0074) authenticates each emitted revision batch with a
SYMMETRIC HMAC key (``COPIER_INGEST_HMAC_KEY``) that the emitter and the backend both hold, resolved for a
bounded operator session from the verifier window (ADR-0101). ADR-0129 recorded the remaining gap: the full
no-symmetric-secret end state needs the INGEST *event* feed itself migrated from HMAC to an asymmetric
signature, so a backend compromise cannot forge an emission. This module is that construction, built and tested
as a standalone CONTRACT: the HOST holds a PRIVATE Ed25519 key and signs each event; the BACKEND holds only the
corresponding PINNED PUBLIC key and verifies it. No secret bytes cross the boundary.

DOMAIN / IDENTITY SEPARATION
----------------------------
Event signing is kept strictly DISTINCT from the two neighbouring signature domains:

* the Copy-Trading WINDOW-OPEN authorization (ADR-0129 ``verifier_window_authorization``), which authorizes
  OPENING a window — a different field set, a different trust registry (``COPIER_WINDOW_OPEN_TRUST``), and here
  additionally a DOMAIN-prefixed signing input so an event signature can never be replayed as a window-open
  attestation (or vice-versa); and
* the TIM operational RELEASE-signing trust (ADR-0070 / ADR-0124) — never consulted; this module imports
  nothing from the TIM lane.

The pinned public identity here is resolved from the event-only ``COPIER_INGEST_EVENT_TRUST`` registry, which is
EMPTY by default (DARK): every verification fails ``unknown_key_id`` until a real key is separately pinned.

WHAT EACH EVENT BINDS
---------------------
A valid event is accepted ONLY when it is signed by a pinned public key AND binds, exactly, to: the configured
SIGNER (``key_id``), the tenant OWNER (``owner_id``), the governed ROUTE (``master_account_id`` ->
``follower_account_id``), the ``INGEST`` PURPOSE, the AUTHORIZED WINDOW (by its ``window_not_after_epoch``
boundary — the event is valid only while that exact operator window is OPEN and unexpired, and may not outlive
it), its own EXPIRY (``issued_at_epoch`` freshness + ``expiry_epoch``), a single-use NONCE (durable replay burn),
and the PAYLOAD (``payload_digest``). Everything is fail-closed and value-blind: a rejection is a stable reason
code, never a key, a signature or a secret.

This module creates nothing, contacts no host or broker, and resolves no symmetric key. It is exercised only
through injected providers + synthetic keys in tests; wiring a real host emitter + the production trust pin is a
separate, Sponsor-gated rollout (see ADR-0130 "Integration gap").
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable, Optional

from django.conf import settings

# ─────────────────────────────── protocol constants ───────────────────────────────
INGEST_EVENT_PROTOCOL_VERSION = "copier-ingest-event-ed25519-1"
INGEST_EVENT_ALG = "ed25519"
INGEST_EVENT_PURPOSE = "INGEST"
# Domain-separation prefix: distinct from the window-open attestation (which has NO domain prefix) and from TIM.
# Bound into every signature so a signature from one domain can never verify in another.
INGEST_EVENT_DOMAIN = b"guvfx.copier.ingest-event.v1"

DEFAULT_MAX_ISSUE_AGE_S = 120            # the emitter signs immediately before sending
_FUTURE_SKEW_S = 30
MAX_EVENT_TTL_SECONDS = 600              # an event may not claim validity for more than 10 minutes
MAX_NONCE_LEN = 72                       # leaves room for the durable-store namespace prefix within varchar(80)

_SIGNED_FIELDS = (
    "protocol_version", "alg", "key_id",
    "owner_id", "master_account_id", "follower_account_id",
    "purpose", "window_not_after_epoch",
    "issued_at_epoch", "expiry_epoch", "nonce", "payload_digest",
)

# ─────────────────────────────── stable, value-blind reason codes ───────────────────────────────
R_VERIFIED = "verified"
R_MALFORMED = "malformed_event"
R_BAD_PROTOCOL = "bad_protocol_version"
R_BAD_ALG = "bad_alg"
R_UNKNOWN_KEY = "unknown_key_id"             # key_id not pinned / revoked / unknown
R_BAD_SIGNATURE = "bad_signature"
R_PURPOSE_MISMATCH = "purpose_mismatch"
R_ROUTE_MISMATCH = "route_mismatch"
R_TENANT_MISMATCH = "tenant_mismatch"
R_NO_WINDOW = "no_authorized_window"          # no OPEN INGEST window to bind to
R_WINDOW_MISMATCH = "window_mismatch"         # bound not-after != the OPEN window's not-after
R_WINDOW_EXPIRED = "authorized_window_expired"
R_EVENT_EXPIRED = "event_expired"             # stale/future issue, past expiry, ttl too long
R_EVENT_OUTLIVES_WINDOW = "event_outlives_window"
R_PAYLOAD_DIGEST_MISMATCH = "payload_digest_mismatch"
R_NONCE_TOO_LONG = "nonce_too_long"
R_NONCE_REPLAYED = "nonce_replayed"


class IngestEventError(Exception):
    """A structurally-sanitised Ed25519 ingest-event rejection. ``reason`` is a stable code, never a secret."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ─────────────────────────────── pure signing primitives ───────────────────────────────

def canonical_bytes(obj) -> bytes:
    """Deterministic canonical JSON (sorted keys, compact separators) — host and backend agree byte-for-byte."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def payload_digest(payload) -> str:
    """SHA-256 hex of the canonical payload (binds the carried body to the signature)."""
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


@dataclass(frozen=True)
class IngestEventAttestation:
    """The canonical, host-signed field set for ONE INGEST event. Every field is bound by the signature; none is
    a secret. ``key_id`` names which pinned public key signed it (for rotation/revocation)."""
    key_id: str
    owner_id: int
    master_account_id: int
    follower_account_id: int
    purpose: str
    window_not_after_epoch: int
    issued_at_epoch: int
    expiry_epoch: int
    nonce: str
    payload_digest: str

    def signed_dict(self) -> dict:
        return {
            "protocol_version": INGEST_EVENT_PROTOCOL_VERSION,
            "alg": INGEST_EVENT_ALG,
            "key_id": str(self.key_id),
            "owner_id": int(self.owner_id),
            "master_account_id": int(self.master_account_id),
            "follower_account_id": int(self.follower_account_id),
            "purpose": str(self.purpose),
            "window_not_after_epoch": int(self.window_not_after_epoch),
            "issued_at_epoch": int(self.issued_at_epoch),
            "expiry_epoch": int(self.expiry_epoch),
            "nonce": str(self.nonce),
            "payload_digest": str(self.payload_digest),
        }


def signing_input(signed: dict) -> bytes:
    """The exact bytes the host signs and the backend verifies: DOMAIN || '\\n' || canonical(signed-field-set).
    The domain prefix is what separates this signature from the window-open attestation and TIM signing."""
    body = canonical_bytes({k: signed[k] for k in _SIGNED_FIELDS})
    return INGEST_EVENT_DOMAIN + b"\n" + body


def sign_ingest_event(*, signing_key, owner_id: int, master_account_id: int, follower_account_id: int,
                      nonce: str, issued_at_epoch: int, expiry_epoch: int, window_not_after_epoch: int,
                      payload: dict, key_id: str, purpose: str = INGEST_EVENT_PURPOSE) -> dict:
    """Build a signed Ed25519 INGEST-event envelope (tests + a FUTURE host emitter; NEVER production backend code).

    ``signing_key`` is an ``Ed25519PrivateKey`` (tests pass a SYNTHETIC key). Returns
    ``{"alg", "signed": {...}, "signature": hex, "payload": payload}``."""
    att = IngestEventAttestation(
        key_id=str(key_id), owner_id=int(owner_id), master_account_id=int(master_account_id),
        follower_account_id=int(follower_account_id), purpose=str(purpose),
        window_not_after_epoch=int(window_not_after_epoch), issued_at_epoch=int(issued_at_epoch),
        expiry_epoch=int(expiry_epoch), nonce=str(nonce), payload_digest=payload_digest(payload))
    signed = att.signed_dict()
    signature = signing_key.sign(signing_input(signed)).hex()
    return {"alg": INGEST_EVENT_ALG, "signed": signed, "signature": signature, "payload": payload}


# ─────────────────────────────── pinned public identity (Copy-Trading event trust) ───────────────────────────────

def _ingest_event_trust_public_key(key_id: str) -> Optional[bytes]:
    """Resolve a PINNED INGEST-event public key (raw 32-byte Ed25519) by ``key_id`` from the
    ``COPIER_INGEST_EVENT_TRUST`` setting (``{key_id: base64_raw_public_key}``). Returns None when the key_id is
    not pinned — unknown OR revoked (revocation = remove the entry). DARK by default: the setting is EMPTY, so
    every key_id resolves to None and verification fails ``unknown_key_id``. This registry is INGEST-event-ONLY:
    it is NEVER ``COPIER_WINDOW_OPEN_TRUST`` and NEVER the TIM release-signing trust store."""
    import base64
    trust = getattr(settings, "COPIER_INGEST_EVENT_TRUST", {}) or {}
    b64 = trust.get(str(key_id))
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64)
    except Exception:  # noqa: BLE001 — a malformed pin is treated as absent (fail closed)
        return None
    return raw if len(raw) == 32 else None


def _ed25519_verify(public_key_raw: bytes, signature: bytes, message: bytes) -> bool:
    """Verify an Ed25519 signature with the backend ``cryptography`` primitive. Fail-closed: any error -> False;
    never raises, never logs, never carries key/sig bytes into an exception. Local to this module so the event
    domain stays decoupled from the window-open-authorization domain."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(bytes(public_key_raw)).verify(bytes(signature), bytes(message))
        return True
    except Exception:  # noqa: BLE001
        return False


def _active_ingest_window_not_after(*, now_epoch: int) -> Optional[int]:
    """Default authorized-window lookup: the ``not_after`` epoch of the single OPEN, unexpired window that covers
    INGEST, or None. Reads window METADATA only (never the key value / tmpfs path). Injected in tests."""
    from trade_copier.verifier_window import active_window
    w = active_window("INGEST")
    if w is None:
        return None
    return int(w.expires_at.timestamp())


# ─────────────────────────────── the verifier ───────────────────────────────

def verify_ingest_event(
    envelope: dict,
    *,
    expected_owner_id: int,
    expected_master_id: int,
    expected_follower_id: int,
    now_epoch: int,
    nonce_burn: Callable[[str, float], bool],
    public_key_provider: Callable[[str], Optional[bytes]] = _ingest_event_trust_public_key,
    window_not_after_provider: Callable[..., Optional[int]] = _active_ingest_window_not_after,
    expected_purpose: str = INGEST_EVENT_PURPOSE,
    max_issue_age_s: int = DEFAULT_MAX_ISSUE_AGE_S,
) -> dict:
    """Verify a signed Ed25519 INGEST event against a PINNED public identity, fail-closed and value-blind.

    Returns ``{"signed", "payload", "bound"}`` on success; raises ``IngestEventError(reason)`` on any failure.
    The ordering is structure -> declared alg/protocol -> pinned identity -> signature -> binding
    (purpose/route/tenant) -> authorized window -> event expiry -> payload digest -> single-use replay, so a
    verdict never depends on a field the signature did not cover, and the nonce is spent only by a fully-valid
    event. Providers are INJECTED (the trust registry, the window lookup and the durable nonce burn), so the
    pure contract is exercised with synthetic keys and no I/O; by default it reads the real Copy-Trading event
    trust + the real verifier window. An Ed25519 event NEVER falls back to HMAC — a failure here is terminal.
    """
    # 1) Structure — envelope + signed field set must be present and well-typed.
    if not isinstance(envelope, dict):
        raise IngestEventError(R_MALFORMED)
    signed = envelope.get("signed")
    signature_hex = envelope.get("signature")
    payload = envelope.get("payload")
    if not isinstance(signed, dict) or not isinstance(signature_hex, str) or payload is None:
        raise IngestEventError(R_MALFORMED)
    if envelope.get("alg") != INGEST_EVENT_ALG or signed.get("alg") != INGEST_EVENT_ALG:
        raise IngestEventError(R_BAD_ALG)
    if signed.get("protocol_version") != INGEST_EVENT_PROTOCOL_VERSION:
        raise IngestEventError(R_BAD_PROTOCOL)
    for field in _SIGNED_FIELDS:
        if field not in signed:
            raise IngestEventError(R_MALFORMED)
    try:
        key_id = str(signed["key_id"])
        owner_id = int(signed["owner_id"])
        master_id = int(signed["master_account_id"])
        follower_id = int(signed["follower_account_id"])
        purpose = str(signed["purpose"])
        window_not_after = int(signed["window_not_after_epoch"])
        issued_at = int(signed["issued_at_epoch"])
        expiry = int(signed["expiry_epoch"])
        nonce = str(signed["nonce"])
        digest_claim = str(signed["payload_digest"])
        signature = bytes.fromhex(signature_hex)
    except (TypeError, ValueError):
        raise IngestEventError(R_MALFORMED)
    if not key_id or not nonce or not signature:
        raise IngestEventError(R_MALFORMED)
    if len(nonce) > MAX_NONCE_LEN:
        raise IngestEventError(R_NONCE_TOO_LONG)   # bounded before any durable insert (sanitised, never a 500)

    # 2) Pinned identity — unknown OR revoked key_id ⇒ fail closed BEFORE any signature math.
    pub = public_key_provider(key_id)
    if not pub:
        raise IngestEventError(R_UNKNOWN_KEY)

    # 3) Signature over the DOMAIN-separated canonical signing input (binds every field + the event domain).
    if not _ed25519_verify(pub, signature, signing_input(signed)):
        raise IngestEventError(R_BAD_SIGNATURE)

    # 4) Binding — purpose, route and tenant must match EXACTLY (only now that the signature is proven).
    if purpose != expected_purpose:
        raise IngestEventError(R_PURPOSE_MISMATCH)
    if master_id != int(expected_master_id) or follower_id != int(expected_follower_id):
        raise IngestEventError(R_ROUTE_MISMATCH)
    if owner_id != int(expected_owner_id):
        raise IngestEventError(R_TENANT_MISMATCH)

    # 5) Authorized window — the event is valid ONLY while the exact operator window it names is OPEN + unexpired.
    #    Read-only metadata (never the key value / tmpfs path); the uncapped live window burns no "use" here.
    current_not_after = window_not_after_provider(now_epoch=now_epoch)
    if current_not_after is None:
        raise IngestEventError(R_NO_WINDOW)
    if int(current_not_after) != window_not_after:
        raise IngestEventError(R_WINDOW_MISMATCH)
    if int(window_not_after) <= int(now_epoch):
        raise IngestEventError(R_WINDOW_EXPIRED)

    # 6) Event expiry — freshly issued, not implausibly future, not past its own expiry, bounded TTL, and it may
    #    not outlive the authorized window (so a signed event can never be verified after its window closes).
    if issued_at < int(now_epoch) - int(max_issue_age_s) or issued_at > int(now_epoch) + _FUTURE_SKEW_S:
        raise IngestEventError(R_EVENT_EXPIRED)
    if int(now_epoch) > expiry or expiry <= issued_at or (expiry - issued_at) > MAX_EVENT_TTL_SECONDS:
        raise IngestEventError(R_EVENT_EXPIRED)
    if expiry > window_not_after:
        raise IngestEventError(R_EVENT_OUTLIVES_WINDOW)

    # 7) Payload integrity — the carried body must match the signed digest.
    if payload_digest(payload) != digest_claim:
        raise IngestEventError(R_PAYLOAD_DIGEST_MISMATCH)

    # 8) Single-use replay burn (durable, atomic) — LAST, so a replay of a valid event is still rejected and an
    #    unauthenticated event can never consume a nonce. The durable store namespaces this nonce into its own
    #    domain so an event nonce can never collide with a window-open or HMAC nonce of the same value.
    if not nonce_burn(nonce, float(expiry)):
        raise IngestEventError(R_NONCE_REPLAYED)

    return {"signed": signed, "payload": payload,
            "bound": {"owner_id": owner_id, "master_account_id": master_id,
                      "follower_account_id": follower_id, "purpose": purpose,
                      "window_not_after_epoch": window_not_after, "key_id": key_id}}
