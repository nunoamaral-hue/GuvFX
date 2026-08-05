"""ADR-0027 — the ONE canonical broker-login validation service.

``BrokerLoginValidator.validate(account)`` is the single, reusable, non-destructive validation mechanism for
every future flow (Add / Edit / Test / Retry / Health / Recovery). It resolves the login+server from stored
data (ADR-0025), decrypts the password **only at point of use** (audited, never persisted/logged), envelope-
encrypts it to the agent's public key (ADR-0027 — the backend cannot read it back), sends a signed
``VALIDATE_LOGIN`` request, and maps the agent's structured reason into a **secret-safe** ``ValidationOutcome``.
It never returns or logs a password, ciphertext, HMAC data, canonical request body, or host path.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass

from django.utils import timezone

from . import broker_cred_envelope as envelope
from .mgmt_client import ManagementChannelError, ManagementChannelTimeout, ManagementChannelUnreachable
from .mgmt_protocol import NIL_UUID
from .provisioner import resolve_broker_server

logger = logging.getLogger(__name__)

# customer-visible health buckets
HEALTHY = "HEALTHY"
NEEDS_ATTENTION = "NEEDS_ATTENTION"
UNAVAILABLE = "UNAVAILABLE"

# agent reason → (status, retryable). Transient (retryable) reasons keep the credential; credential/account
# reasons never hot-loop. ``demo_ok``/``live_detected`` carry the connected classification (both a genuine,
# connected session — the classifier decision, not a failure).
_TAXONOMY = {
    "demo_ok":                 (HEALTHY, False),
    "live_detected":           (HEALTHY, False),
    "invalid_password":        (NEEDS_ATTENTION, False),
    "invalid_login":           (NEEDS_ATTENTION, False),
    "server_not_found":        (NEEDS_ATTENTION, False),
    "account_disabled":        (NEEDS_ATTENTION, False),
    "classification_mismatch": (NEEDS_ATTENTION, False),
    "server_unavailable":      (UNAVAILABLE, True),
    "login_timeout":           (UNAVAILABLE, True),
    # Transport-layer timeouts (2026-08-05, WS-A/B) — THREE distinct failure modes, never merged:
    #   * validation_agent_unreachable = backend could not open a TCP connection to the validation agent
    #     (CONNECT timeout); the request was NEVER SENT, so no agent/MT5/broker activity occurred.
    #   * validation_agent_timeout     = the agent accepted the connection (request sent) but did not answer
    #     in time (READ timeout); MT5 status is UNKNOWN.
    #   * login_timeout (above)        = the agent actually reached the MT5 login phase and MT5 reported a
    #     timeout. Only THIS one is evidence a login was attempted.
    # All platform/transient → UNAVAILABLE/retryable; none is the customer's credentials.
    "validation_agent_unreachable": (UNAVAILABLE, True),
    "validation_agent_timeout":     (UNAVAILABLE, True),
    "mt5_unavailable":         (UNAVAILABLE, True),
    "bridge_unavailable":      (UNAVAILABLE, True),
    "runtime_unavailable":     (UNAVAILABLE, True),
    # backend-side (never reaches the agent)
    "broker_server_missing":   (NEEDS_ATTENTION, False),
    "credential_missing":      (NEEDS_ATTENTION, False),
    "validation_unconfigured": (UNAVAILABLE, True),
    "could_not_verify":        (UNAVAILABLE, True),
    # agent/core-origin fail-closed codes. All are PLATFORM conditions (not the customer's credentials), so
    # they map to UNAVAILABLE — never NEEDS_ATTENTION, which would wrongly send the customer to re-enter a
    # correct password. Explicit (not relying on the ``.get`` default) so the contract is deliberate and a
    # reviewer sees each handled; ``isolation_check_failed``/``impl_integrity_mismatch`` additionally warrant
    # an ops alert (a platform-integrity/config fault the customer cannot fix).
    "isolation_check_failed":  (UNAVAILABLE, True),
    "credential_unsealable":   (UNAVAILABLE, True),
    "validation_busy":         (UNAVAILABLE, True),
    # WS-A (2026-08-05): the validation host's MT5 Python↔terminal IPC could not be established (MT5 -10004
    # "No IPC connection"), BEFORE any broker contact. A PLATFORM condition (Session-0 GUI/IPC readiness),
    # never a broker outage and never the customer's credentials → UNAVAILABLE/retryable. This is the reason
    # the agent now returns instead of mis-labelling a local IPC failure as ``server_unavailable``.
    "validation_ipc_unavailable": (UNAVAILABLE, True),
    # ADR-0027 task-launch remediation: the GUI-capable runner task could not be triggered or did not answer
    # in time. Platform conditions (never the customer's credentials) → UNAVAILABLE/retryable.
    "validation_runner_unavailable": (UNAVAILABLE, True),
    "validation_runner_timeout":     (UNAVAILABLE, True),
    # ADR-0027 observability: the runner could not durably preserve its diagnostic evidence — a platform fault
    # (never a credential verdict). Retryable; the credential scrub still ran, so it is safe to retry.
    "diagnostic_capture_failed":     (UNAVAILABLE, True),
    "credential_scrub_unverified":   (UNAVAILABLE, True),   # ADR-0027 Phase 2: defeated scrub → never HEALTHY
    "validation_baseline_dirty":     (UNAVAILABLE, True),   # ADR-0027 Phase 2: prior run left a dirty baseline
    "impl_integrity_mismatch": (UNAVAILABLE, True),
    "payload_missing":         (UNAVAILABLE, True),
    "payload_digest_mismatch": (UNAVAILABLE, True),
    "agent_internal_error":    (UNAVAILABLE, True),
}


@dataclass(frozen=True)
class ValidationOutcome:
    """Allowlisted, non-secret result of a login validation. No password/ciphertext/host-path ever."""
    status: str            # HEALTHY | NEEDS_ATTENTION | UNAVAILABLE
    reason: str            # taxonomy code
    retryable: bool
    server: str
    login_masked: str
    checked_at: str
    correlation_id: str
    is_demo: bool | None = None

    def as_dict(self) -> dict:
        return {"status": self.status, "reason": self.reason, "retryable": self.retryable,
                "server": self.server, "login_masked": self.login_masked, "is_demo": self.is_demo,
                "checked_at": self.checked_at, "correlation_id": self.correlation_id}


def _mask_login(login: str) -> str:
    s = str(login or "")
    return ("***" + s[-3:]) if len(s) > 3 else "***"


def _outcome(reason, *, server, login_masked, correlation_id, is_demo=None):
    status, retryable = _TAXONOMY.get(reason, (UNAVAILABLE, True))
    return ValidationOutcome(status=status, reason=reason, retryable=retryable, server=server,
                             login_masked=login_masked, is_demo=is_demo,
                             checked_at=timezone.now().isoformat(), correlation_id=correlation_id)


class BrokerLoginValidator:
    """Canonical validator. ``transport`` and ``now`` are injectable for tests; production resolves the signed
    channel + agent base URL from settings/env, exactly like ``AgentWindowsProvisioner``."""

    def __init__(self, *, transport=None, base_url: str = "", keyring=None, key_id=None):
        self._transport = transport
        self._base_url = base_url
        self._keyring = keyring
        self._key_id = key_id

    def _channel(self):
        from .beta_worker import make_http_transport
        from .mgmt_client import _load_keyring
        transport = self._transport or make_http_transport()
        if self._keyring is not None and self._key_id is not None:
            keyring, key_id = self._keyring, self._key_id
        else:
            keyring, key_id = _load_keyring()
        base = self._base_url
        if not base:
            from django.conf import settings
            import os
            base = getattr(settings, "BETA_AGENT_BASE_URL", "") or os.getenv("BETA_AGENT_BASE_URL", "")
        return transport, base, keyring, key_id

    def validate(self, account) -> ValidationOutcome:
        from django.utils import timezone as _tz
        from .mgmt_protocol import sign_request, ProtocolError
        corr = f"validate-acct-{getattr(account, 'id', '?')}-{secrets.token_hex(6)}"
        server, reason = resolve_broker_server(account)
        login = str(getattr(account, "account_number", "") or "")
        masked = _mask_login(login)
        if not server:
            return _outcome(reason or "broker_server_missing", server="", login_masked=masked, correlation_id=corr)

        # runtime/validation-context identity: the account's runtime uuid if any, else the nil placeholder.
        runtime_uuid = NIL_UUID
        if getattr(account, "id", None):
            from .models import AccountRuntime
            rt = AccountRuntime.objects.filter(trading_account=account).order_by("id").first()
            if rt is not None:
                runtime_uuid = str(rt.runtime_uuid)

        if not envelope.backend_enc_configured():
            return _outcome("validation_unconfigured", server=server, login_masked=masked, correlation_id=corr)
        # SEAL-ONLY invariant (code-enforced): the backend must NOT hold envelope private keys — those belong
        # ONLY to the agent. If they are present here (e.g. the agent host's env was copied onto the backend),
        # refuse to seal and log loudly, rather than let the "backend cannot decrypt" property fail silently.
        if envelope.backend_has_private_keys():
            logger.error("broker-login validation refused: BROKER_CRED_ENC_PRIVKEYS is set on the BACKEND — "
                         "envelope private keys must live ONLY on the agent (seal-only invariant)")
            return _outcome("validation_unconfigured", server=server, login_masked=masked, correlation_id=corr)

        # decrypt ONLY at point of use, audit the access (no secret), envelope-encrypt immediately.
        password = self._decrypt_at_point_of_use(account)
        if password is None:
            return _outcome("credential_missing", server=server, login_masked=masked, correlation_id=corr)
        try:
            nonce = secrets.token_hex(16)
            aad = envelope.bind_aad(operation="VALIDATE_LOGIN", runtime_uuid=runtime_uuid,
                                    correlation_id=corr, nonce=nonce)
            sealed = envelope.seal(password.encode("utf-8"), aad=aad)     # backend cannot decrypt this back
        except envelope.EnvelopeError:
            # ``backend_enc_configured`` guards the common case, but seal must still fail CLOSED into a
            # ValidationOutcome — ``validate`` never raises to its caller — with the plaintext already dropped.
            return _outcome("validation_unconfigured", server=server, login_masked=masked, correlation_id=corr)
        finally:
            password = None                                              # drop plaintext asap
        payload = {"login": login, "server": server, "enc_key_id": sealed["key_id"], "password_env": sealed}

        transport, base, keyring, key_id = self._channel()
        try:
            req = sign_request(provisioning_job_id=0, runtime_uuid=runtime_uuid, operation="VALIDATE_LOGIN",
                               correlation_id=corr, keyring=keyring, key_id=key_id,
                               now=int(_tz.now().timestamp()), nonce=nonce, payload=payload)
        except (ProtocolError, envelope.EnvelopeError):
            return _outcome("validation_unconfigured", server=server, login_masked=masked, correlation_id=corr)

        try:
            resp = transport(base, req)
        except ManagementChannelUnreachable:
            # CONNECT timeout: the backend never opened a connection to the agent, so the request was NEVER
            # SENT and NOTHING downstream ran (no agent handler, no runner, no MT5, no broker). This is NOT a
            # login timeout — no login was attempted. Must be caught BEFORE ManagementChannelTimeout (subclass).
            return _outcome("validation_agent_unreachable", server=server, login_masked=masked, correlation_id=corr)
        except ManagementChannelTimeout:
            # READ timeout: the agent accepted the connection (request sent) but did not answer in time. The op
            # may or may not have executed — MT5 status is UNKNOWN. Distinct from a login timeout and from
            # unreachable. NEVER ``login_timeout`` — the backend has no evidence a broker login was attempted.
            return _outcome("validation_agent_timeout", server=server, login_masked=masked, correlation_id=corr)
        except (ManagementChannelError, OSError):
            return _outcome("bridge_unavailable", server=server, login_masked=masked, correlation_id=corr)

        if not isinstance(resp, dict):
            return _outcome("could_not_verify", server=server, login_masked=masked, correlation_id=corr)
        # The agent's sanitised response carries the login taxonomy in ``reason_code`` (its universal reason
        # field) and the DEMO/REAL classification in ``is_demo`` — never a password/ciphertext/host path.
        if resp.get("outcome") == "ok":
            agent_reason = str(resp.get("reason_code") or "demo_ok")
        else:
            agent_reason = str(resp.get("reason_code") or "could_not_verify")
        _d = resp.get("is_demo")
        is_demo = _d if isinstance(_d, bool) else None
        # never echo the agent's server/login — use the ones we submitted (already non-secret)
        return _outcome(agent_reason, server=server, login_masked=masked, correlation_id=corr, is_demo=is_demo)

    def _decrypt_at_point_of_use(self, account) -> str | None:
        """Decrypt ``password_enc`` and audit the ACCESS (redacted, no secret). Returns None if none on file."""
        enc = getattr(account, "password_enc", "") or ""
        if not enc:
            return None
        from trading.crypto import decrypt_password
        try:
            from core.audit import log_customer_credential_event
            log_customer_credential_event("ACCESSED", account=account, actor="broker_login_validation",
                                          purpose="login-validation")
        except Exception:
            pass                                    # audit is best-effort; NEVER blocks/leaks the credential
        return decrypt_password(enc)
