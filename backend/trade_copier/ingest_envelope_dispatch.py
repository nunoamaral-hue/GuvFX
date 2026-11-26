"""trade_copier.ingest_envelope_dispatch — the fail-closed algorithm selector for a signed INGEST envelope
(GFX-COPIER-INGEST-EVENT-ED25519-173, ADR-0130).

The signed event-ingestion endpoint must accept the LEGACY symmetric HMAC envelope (ADR-0074) on its explicitly
configured path AND the new asymmetric Ed25519 event (ADR-0130), while guaranteeing the single most important
property of the migration: an Ed25519 event that is invalid or whose key is unknown MUST fail closed and MUST
NEVER fall back to HMAC verification.

This module makes the algorithm decision EXACTLY ONCE, from the envelope's DECLARED ``alg`` field, and then calls
only the selected verifier. It never catches a verification failure to retry with the other algorithm, so a
cross-algorithm fallback is structurally impossible. Each concrete verifier (its keyring / trust / window / nonce
providers) is INJECTED, so the selector is pure and exhaustively testable with synthetic inputs.
"""
from __future__ import annotations

from typing import Callable, Optional

ALG_HMAC = "hmac"
ALG_ED25519 = "ed25519"

R_UNKNOWN_ALG = "unknown_alg"
R_HMAC_LEGACY_DISABLED = "hmac_legacy_disabled"


class DispatchError(Exception):
    """A sanitised dispatch-selection failure (never a secret). Distinct from the per-algorithm verify errors
    (``event_ingestion.IngestError`` / ``ingest_event_signing.IngestEventError``), which propagate unchanged."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def select_alg(envelope) -> str:
    """Deterministically select the verification algorithm from the envelope's DECLARED ``alg``.

    A missing/empty ``alg`` is the LEGACY HMAC envelope (the ADR-0074 envelope shape carries no ``alg`` field),
    so existing emitters keep verifying via HMAC unchanged. An explicit ``"ed25519"`` selects the asymmetric
    path. Anything else is an explicitly unknown algorithm. The decision is returned here and made ONCE; the
    caller must verify with exactly this algorithm and never retry with another."""
    if not isinstance(envelope, dict):
        return ALG_HMAC            # a non-dict is handled (and rejected) by the HMAC verifier's structure check
    alg = envelope.get("alg")
    if alg in (None, "", ALG_HMAC):
        return ALG_HMAC
    if alg == ALG_ED25519:
        return ALG_ED25519
    return ""                      # explicitly unknown ⇒ fail closed below


def verify_ingest_envelope(
    envelope,
    *,
    hmac_verify: Callable[[dict], dict],
    ed25519_verify: Callable[[dict], dict],
    hmac_legacy_enabled: Optional[bool] = None,
) -> dict:
    """Route ``envelope`` to exactly ONE verifier by its declared algorithm and return
    ``{"alg", "result"}``.

    * ``ed25519`` → ``ed25519_verify(envelope)``. Any failure propagates (the verifier raises); this function
      NEVER falls back to HMAC.
    * ``hmac`` (or no ``alg`` — the legacy shape) → ``hmac_verify(envelope)``, but ONLY while the legacy HMAC
      path is enabled (``copier_ingest_hmac_legacy_enabled`` by default; preserve existing behaviour). When the
      operator has explicitly disabled the legacy path, this raises ``DispatchError(hmac_legacy_disabled)`` and
      does NOT try Ed25519.
    * anything else → ``DispatchError(unknown_alg)`` (fail closed).

    ``hmac_verify`` / ``ed25519_verify`` are injected closures that bind their own providers (keyring + nonce
    burn for HMAC; expected tenant/route + window lookup + durable nonce burn for Ed25519)."""
    alg = select_alg(envelope)
    if alg == ALG_ED25519:
        # Selected ONCE. A failure here raises out of ed25519_verify and is terminal — no HMAC fallback.
        return {"alg": ALG_ED25519, "result": ed25519_verify(envelope)}
    if alg == ALG_HMAC:
        enabled = _hmac_legacy_enabled() if hmac_legacy_enabled is None else bool(hmac_legacy_enabled)
        if not enabled:
            raise DispatchError(R_HMAC_LEGACY_DISABLED)
        return {"alg": ALG_HMAC, "result": hmac_verify(envelope)}
    raise DispatchError(R_UNKNOWN_ALG)


def _hmac_legacy_enabled() -> bool:
    from trade_copier.flags import copier_ingest_hmac_legacy_enabled
    return copier_ingest_hmac_legacy_enabled()
