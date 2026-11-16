"""trade_copier.attest_reconcile - authoritative, READ-ONLY reconciliation of an AMBIGUOUS one-shot
attestation POST (ADR-0077 / GFX-COPIER-ATTEST-TRANSPORT-RECONCILIATION-139).

The host emitter sends its signed attestation envelope to the attest-verify endpoint EXACTLY ONCE. When
that POST returns an AMBIGUOUS transport outcome (connection dropped / HTTP 502 / no response), the operator
orchestration must NOT blindly retry - the verifier may have processed the request even though the HTTP
response was lost. This module decides, from AUTHORITATIVE DURABLE STATE only (never the lost response),
whether the backend actually processed that specific request:

  * ``CopierVerifierWindow.uses`` is incremented inside ``verifier_window.claim_key`` the moment the key is
    CLAIMED - which happens BEFORE the signature/identity verdict. So ``uses >= 1`` proves the request
    reached verification (PROCESSED); ``uses == 0`` on a still-OPEN window proves it did not (NOT_PROCESSED).
  * ``CopierIngestNonce`` holds the envelope nonce ONLY after a FULL verify (the nonce is burned last, after
    every signature/identity/timestamp check passes). So the nonce being present proves VERIFIED; a
    processed-but-not-burned request is a REJECTED one.

These two EXISTING durable records are sufficient - no new model / migration is required.

Classification (fail-closed; never returns NOT_PROCESSED unless it is provably safe to retry):
  * ``PROCESSED_VERIFIED`` - the request verified (nonce burned). No retry.
  * ``PROCESSED_REJECTED`` - the request was processed and rejected (key claimed, nonce not burned). No retry.
  * ``NOT_PROCESSED``      - window still OPEN, unexpired, uses==0, nonce not burned. Safe to retry ONCE.
  * ``AMBIGUOUS``          - evidence missing / conflicting / unreadable. No retry.

It creates NO state, reads NO key, writes nothing, contacts NO host or broker, and returns only sanitised,
non-secret fields.
"""
from __future__ import annotations

PROCESSED_VERIFIED = "PROCESSED_VERIFIED"
PROCESSED_REJECTED = "PROCESSED_REJECTED"
PROCESSED_INDETERMINATE = "PROCESSED_INDETERMINATE"   # processed (uses>=1) but the verdict is unreadable (lost result, no nonce) - NEVER retry
NOT_PROCESSED = "NOT_PROCESSED"
AMBIGUOUS = "AMBIGUOUS"

_NONCE_MAXLEN = 80  # mirrors trade_copier.emitter.attestation.MAX_NONCE_LEN


def _blank_result() -> dict:
    return {"classification": AMBIGUOUS, "window_found": False, "window_open": None,
            "window_uses": None, "window_max_uses": None, "window_expired": None,
            "nonce_burned": None, "correlation_supplied": False, "correlation_match": None,
            "backend_attest_gate": None, "reason": ""}


def reconcile_attestation(*, nonce: str, correlation_id: str = "", purpose: str = "ATTEST", now=None) -> dict:
    """Classify an ambiguous attestation POST from authoritative durable state. Pure read; fail-closed.

    Returns a sanitised dict (no key, no secret, no envelope/signature). ``nonce`` is the attestation
    envelope nonce (a non-secret single-use value the emitter generated and returned in its sanitised
    result). ``correlation_id`` scopes the window lookup to this operation when supplied.
    """
    from django.utils import timezone
    out = _blank_result()

    nonce = str(nonce or "").strip()
    out["correlation_supplied"] = bool(correlation_id)
    if len(nonce) > _NONCE_MAXLEN:
        out["reason"] = "bad_nonce"
        return out  # AMBIGUOUS
    # An EMPTY nonce is permitted ONLY when a correlation scopes the window (the trigger RESULT - and thus the
    # nonce - was lost, but the host DID run): we then reconcile from the window's ``uses`` alone and NEVER
    # claim VERIFIED. Without a nonce AND without a correlation we cannot safely locate the operation's window.
    if not nonce and not correlation_id:
        out["reason"] = "no_nonce_no_correlation"
        return out  # AMBIGUOUS

    # Read the backend attest gate for context (does not affect classification; a disabled gate simply means
    # a future retry would 503 - the operator handles that, we never claim NOT_PROCESSED off the gate alone).
    try:
        from trade_copier.flags import copier_attestation_verify_enabled
        out["backend_attest_gate"] = bool(copier_attestation_verify_enabled())
    except Exception:  # noqa: BLE001
        out["backend_attest_gate"] = None

    # (1) nonce burned => VERIFIED (authoritative: the nonce is burned only after a full verify). Only when a
    # nonce is present; an empty nonce leaves nonce_burned=None (we cannot confirm a verdict without it).
    burned = False
    if nonce:
        try:
            from trade_copier.models import CopierIngestNonce
            burned = CopierIngestNonce.objects.filter(nonce=nonce).exists()
        except Exception:  # noqa: BLE001 - unreadable nonce store => fail closed (never retry blind)
            out["reason"] = "nonce_read_error"
            return out  # AMBIGUOUS
        out["nonce_burned"] = bool(burned)

    # (2) locate the window for THIS operation. With a correlation id we require an exact match; without one
    # we fall back to the single most-recent ATTEST-capable window (single-flight means there is at most one
    # live attestation window at a time).
    now = now or timezone.now()
    try:
        from trade_copier.models import CopierVerifierWindow, CopierVerifierWindowState as S
        w = None
        if correlation_id:
            w = (CopierVerifierWindow.objects.filter(correlation_id=correlation_id[:64])
                 .order_by("-id").first())
            out["correlation_match"] = bool(w)
        else:
            w = (CopierVerifierWindow.objects.filter(purpose__in=["ATTEST", "BOTH"])
                 .order_by("-id").first())
    except Exception:  # noqa: BLE001 - unreadable window store => fail closed
        out["reason"] = "window_read_error"
        return out  # AMBIGUOUS

    if w is not None:
        out["window_found"] = True
        out["window_open"] = (w.state == S.OPEN)
        out["window_uses"] = int(w.uses)
        out["window_max_uses"] = (None if w.max_uses is None else int(w.max_uses))
        out["window_expired"] = bool(w.expires_at <= now)

    # (3) classify, fail-closed. The burned nonce is the strongest proof; cross-check it against uses.
    if burned:
        if w is not None and int(w.uses) == 0:
            out["reason"] = "conflict_burned_but_uses0"
            out["classification"] = AMBIGUOUS  # a burned nonce with uses==0 is impossible => distrust it
            return out
        out["classification"] = PROCESSED_VERIFIED
        return out

    if w is None:
        out["reason"] = "window_not_found"
        return out  # AMBIGUOUS - cannot prove not-processed without the window

    if int(w.uses) >= 1:
        # The key was CLAIMED (the request reached verification) => PROCESSED, so NEVER retry. With a nonce we
        # know the nonce was not burned => REJECTED; WITHOUT a nonce (lost result) we cannot read the verdict,
        # so it is PROCESSED_INDETERMINATE (still no retry - the operator must not re-run a processed request).
        if nonce:
            out["reason"] = "claimed_not_burned"
            out["classification"] = PROCESSED_REJECTED
        else:
            out["reason"] = "claimed_verdict_unknown_no_nonce"
            out["classification"] = PROCESSED_INDETERMINATE
        return out

    # uses == 0: NOT_PROCESSED is safe to assert ONLY when the window is still OPEN and unexpired (a retry
    # reuses that still-valid window; a closed/expired uses0 window cannot be retried and its request's fate
    # cannot be proven here => AMBIGUOUS).
    if (w.state == S.OPEN) and (w.expires_at > now):
        out["reason"] = "open_unexpired_uses0"
        out["classification"] = NOT_PROCESSED
        return out

    out["reason"] = "window_closed_or_expired_uses0"
    out["classification"] = AMBIGUOUS
    return out
