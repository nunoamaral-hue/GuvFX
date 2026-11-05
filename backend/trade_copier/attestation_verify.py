"""trade_copier.attestation_verify — backend NON-LIFECYCLE verification of a host-emitter attestation (ADR-0077).

Verifies a signed emitter attestation envelope (``trade_copier.emitter.attestation``, signed with the emitter's
own ``COPIER_INGEST_HMAC_KEY``) AND cross-checks the claimed identity against the AUTHORITATIVE database
(``expected_identity`` + ``HostedExecutionEndpoint``). It is a pure identity proof: it creates NO
``MasterEvent``, ``CopierPositionLifecycle``, ``CopierLifecycleDispatch``, ``ExecutionJob`` or alert, and
contacts no broker. Gated by ``copier_attestation_verify_enabled`` (default OFF) and fail-closed. The single-use
nonce is burned via the shared ``CopierIngestNonce`` store (same replay-protection as ingestion).
"""
from __future__ import annotations

import logging
import time

from trade_copier.emitter import attestation as att

logger = logging.getLogger(__name__)


class AttestationVerifyError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def build_keyring() -> dict:
    """Resolve the attestation HMAC key (fail closed). Same dedicated secret as ingestion — never GUVFX_AGENT_TOKEN."""
    from core.credentials import resolve_secret
    secret = resolve_secret(
        "COPIER_INGEST_HMAC_KEY", aliases=("COPIER_INGEST_HMAC_SECRET",),
        purpose="copier host-emitter attestation verify", min_length=32)
    return {"copier-ingest": secret}


def make_nonce_burn():
    """Durable single-use nonce burn (shared ``CopierIngestNonce`` store)."""
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


def _expected_identity_for(account_id: int):
    """Build the authoritative expected-identity dict from the DB (no broker/positions read). None if unknown."""
    from trading.models import TradingAccount
    from execution.snapshot_transport import expected_identity
    acct = (TradingAccount.objects.filter(id=account_id).select_related("broker_server").first())
    if acct is None:
        return None
    login, server = expected_identity(acct)
    login_fp = att.fingerprint(login or getattr(acct, "account_number", ""))
    expected = {
        "account_id": int(acct.id),
        "broker_login_fp": login_fp,
        "broker_server": server or "",
        # The live terminal the emitter reached (observed via /mt5/snapshots/account) must be THIS account's
        # login — so a wrong-terminal attestation fails closed on the backend path, not only in the unit tests.
        "observed_login_fp": login_fp,
    }
    # Best-effort host-route cross-check from the account's own endpoint (when present) — never cross-tenant.
    try:
        from execution.models import HostedExecutionEndpoint
        ep = (HostedExecutionEndpoint.objects
              .filter(trading_account_id=acct.id)
              .exclude(state=HostedExecutionEndpoint.State.RETIRED)
              .first())
        if ep is not None and ep.trading_account_id == acct.id:
            host = str(getattr(ep, "host", "") or "")
            port = getattr(ep, "port", None)
            runtime_path = str(getattr(ep, "runtime_path", "") or "")
            if host and port:
                expected["bridge_id"] = f"{host}:{port}"
            if runtime_path:
                expected["terminal_fp"] = att.fingerprint(runtime_path)
    except Exception:  # noqa: BLE001 — endpoint cross-check is best-effort; the login/server pin already binds
        pass
    return expected


def verify_attestation_request(envelope: dict, *, correlation_id: str = "") -> dict:
    """Gated, fail-closed backend verification. Returns a sanitised verdict dict; raises ``AttestationVerifyError``
    on a disabled gate / unknown account. Creates NO order, job, lifecycle or alert."""
    from trade_copier.flags import copier_attestation_verify_enabled
    if not copier_attestation_verify_enabled():
        raise AttestationVerifyError("disabled")

    if not isinstance(envelope, dict) or not isinstance(envelope.get("signed"), dict):
        raise att.AttestError("malformed_envelope")
    try:
        account_id = int(envelope["signed"].get("account_id"))
    except (TypeError, ValueError, KeyError):
        raise att.AttestError("bad_account_id")

    expected = _expected_identity_for(account_id)
    if expected is None:
        raise AttestationVerifyError("account_not_found")

    keyring = build_keyring()
    out = att.verify_attestation(envelope, keyring=keyring, now=time.time(),
                                 nonce_burn=make_nonce_burn(), expected=expected)
    signed = out["signed"]
    try:
        from core import observability
        observability.log_stage("copier_attestation_verify", correlation_id, account_id=account_id,
                                 host_id=signed.get("host_id"), verified=True)
    except Exception:  # noqa: BLE001 — logging must never fail a verification
        pass
    return {
        "verified": True,
        "account_id": account_id,
        "host_id": signed.get("host_id"),
        "build_sha": signed.get("build_sha"),
        "broker_server": signed.get("broker_server"),
        "bridge_id": signed.get("bridge_id"),
        "terminal_fp": signed.get("terminal_fp"),
        "config_digest": signed.get("config_digest"),
    }
