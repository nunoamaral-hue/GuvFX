"""trade_copier.ingest_probe_verify — backend verification of a NON-TRADING one-shot ingest-signing probe
(GFX-160, ADR-0119).

Verifies a signed, structurally non-trading probe envelope (``trade_copier.emitter.ingest_probe``, signed with the
standing emitter's ``COPIER_INGEST_HMAC_KEY``) resolved from the BOUNDED INGEST verifier window (ADR-0101). It is a
pure crypto/transport/replay proof: it creates NO ``MasterEvent``, ``CopierPositionLifecycle``,
``CopierLifecycleDispatch``, ``ExecutionJob``, follower ticket, ``CopierLiveAuthorization``, ``CopierLiveArm`` or
``LiveExecutionAuthorization``, invokes no copier execution code, and contacts no broker.

Fail-closed and triple-gated:
  * ``copier_ingest_probe_enabled`` (default OFF — which ANDs the master gate AND the ingestion gate); AND
  * the key is readable ONLY while an operator has exactly one OPEN, unexpired window covering INGEST (outside a
    window ``ingest_keyring`` raises and the endpoint returns 503); AND
  * the signed ``account_id`` must equal the fixed pinned account (``PROBE_ACCOUNT_ID``).

The single-use nonce is burned via the shared ``CopierIngestNonce`` store — the SAME replay protection as real
ingestion and attestation, and the durable, metadata-safe evidence that the probe ran (no migration added)."""
from __future__ import annotations

import logging
import time

from trade_copier.emitter import ingest_probe as ip

logger = logging.getLogger(__name__)

# The sole account this probe exists for (defence in depth; the host signer and the pure verifier also pin it).
PROBE_ACCOUNT_ID = 46


class IngestProbeVerifyError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def build_keyring() -> dict:
    """Resolve the ingest HMAC key from the BOUNDED VERIFIER WINDOW (ADR-0101), never the environment. Readable
    only while an operator has an OPEN, unexpired window covering INGEST; outside a window this raises
    (``VerifierWindowError``, fail closed) and the endpoint returns 503. The window primitive fails closed on 0
    or >=2 open windows, so 'exactly one OPEN INGEST window' is enforced here, not assumed."""
    from trade_copier.verifier_window import ingest_keyring
    return ingest_keyring("INGEST")


def make_nonce_burn():
    """Durable single-use nonce burn (shared ``CopierIngestNonce`` store). The burned row is the durable,
    metadata-safe evidence that the probe nonce was consumed (replay then fails closed)."""
    from datetime import datetime, timezone as _tz
    from django.db import IntegrityError, transaction
    from trade_copier.models import CopierIngestNonce

    def _burn(nonce: str, expiry_epoch: float) -> bool:
        expires_at = datetime.fromtimestamp(float(expiry_epoch), tz=_tz.utc)
        try:
            with transaction.atomic():
                CopierIngestNonce.objects.create(nonce=nonce, expires_at=expires_at)
            return True
        except IntegrityError:
            return False
    return _burn


def verify_ingest_probe_request(envelope: dict, *, correlation_id: str = "") -> dict:
    """Gated, fail-closed backend verification of ONE non-trading probe. Returns a sanitised, value-blind verdict
    dict; raises ``IngestProbeVerifyError('disabled')`` when the gate is off. Creates NO order, job, lifecycle,
    authorization, arm or alert. Never returns a secret, signature or raw envelope."""
    from trade_copier.flags import copier_ingest_probe_enabled
    if not copier_ingest_probe_enabled():
        raise IngestProbeVerifyError("disabled")

    if not isinstance(envelope, dict) or not isinstance(envelope.get("signed"), dict):
        raise ip.IngestProbeError("malformed_envelope")
    try:
        account_id = int(envelope["signed"].get("account_id"))
    except (TypeError, ValueError, KeyError):
        raise ip.IngestProbeError("bad_account_id")
    # Fixed-account pin BEFORE touching the window/key: a non-46 probe is refused deterministically.
    if account_id != PROBE_ACCOUNT_ID:
        raise ip.IngestProbeError("account_not_probe_pinned")

    keyring = build_keyring()   # raises VerifierWindowError (fail closed) if no single OPEN INGEST window
    out = ip.verify_ingest_probe(envelope, keyring=keyring, now=time.time(),
                                 nonce_burn=make_nonce_burn(), expected_account_id=PROBE_ACCOUNT_ID)
    signed = out["signed"]
    try:
        from core import observability
        observability.log_stage("copier_ingest_probe_verify", correlation_id, account_id=account_id, verified=True)
    except Exception:  # noqa: BLE001 — logging must never fail a verification
        pass
    # Value-blind verdict: only non-secret, non-trading metadata. Never the signature, secret or trade data
    # (there is none — the envelope has no trade fields).
    return {
        "verified": True,
        "account_id": account_id,
        "operation": signed.get("operation"),
        "protocol_version": signed.get("protocol_version"),
    }
