"""hosted_workspace.host_executor — Beta Readiness Stream 5: the signed host-executor (Django side).

``SignedHostExecutor`` is the concrete ``HostExecutor`` that ``prepare_hosted_slot`` drives when the engine is
armed. It is NOT a shell, a PowerShell runner, or a file API: every method maps to ONE allow-listed operation
on ``host_protocol`` and sends ONLY ``account_id`` + a typed op — the host derives identity and all paths. The
Windows account password (PROVISION_IDENTITY) is sealed with the ADR-0027 envelope (the backend cannot decrypt
what it sealed) and never logged. The signed response is verified so a MITM cannot forge the G5 ACL read-back.

DARK: ``resolve_signed_host_executor`` returns ``None`` unless ``HOSTED_HOST_EXECUTOR_ENABLED`` is on AND the
keyring / base_url / envelope key are configured — so in the repository-only phase the executor is absent and
``prepare_hosted_slot`` fails closed (host_executor_unavailable), contacting no host. Confinement (identity↔path,
Customer-Zero refusal) is enforced HERE as the first layer, and again host-side (host_agent_dispatch).
"""
from __future__ import annotations

import logging
import secrets

from hosted_workspace.host_agent_dispatch import derive_slot, reserved_ids_from
from hosted_workspace.host_protocol import (
    HostProtocolError, sign_hosted_request, verify_hosted_response,
)

logger = logging.getLogger("guvfx.hosted_workspace")


def _epoch_now() -> int:
    from django.utils import timezone
    return int(timezone.now().timestamp())


class SignedHostExecutor:
    """One instance per (account, host). Stateless across calls except its identity + transport config."""

    def __init__(self, *, account_id, rdp_host, transport, keyring, key_id, base_url,
                 seal_password, reserved_ids=None, correlation_id=None, clock=_epoch_now):
        self.account_id = int(account_id)
        self.rdp_host = str(rdp_host or "")
        self.transport = transport                 # callable(base_url, request) -> response dict; may raise
        self.keyring = keyring
        self.key_id = key_id
        self.base_url = base_url
        self._seal_password = seal_password        # callable(password_bytes, *, account_id, correlation_id, nonce) -> envelope
        self.reserved_ids = reserved_ids_from(reserved_ids)
        self.correlation_id = str(correlation_id or secrets.token_hex(8))
        self._clock = clock
        self._slot = derive_slot(self.account_id)

    # ── confinement (Django layer) ────────────────────────────────────────────────────────────────────────
    def _confined(self, *, username=None, runtime_root=None) -> bool:
        if self.account_id in self.reserved_ids:
            return False
        if username is not None and str(username) != self._slot["username"]:
            return False
        if runtime_root is not None and str(runtime_root).replace("/", "\\").rstrip("\\").lower() \
                != self._slot["runtime_root"].lower():
            return False
        return True

    def _send(self, operation, *, params=None, payload=None, nonce=None) -> dict:
        if self.account_id in self.reserved_ids:
            return {"ok": False, "reason": "reserved_identity"}
        nonce = nonce or secrets.token_hex(16)
        try:
            req = sign_hosted_request(
                account_id=self.account_id, operation=operation, correlation_id=self.correlation_id,
                keyring=self.keyring, key_id=self.key_id, now=int(self._clock()),
                params=params or {}, nonce=nonce, payload=payload)
        except HostProtocolError as e:
            return {"ok": False, "reason": e.reason_code}
        try:
            resp = self.transport(self.base_url, req)
        except Exception:  # noqa: BLE001 — network/timeout/transport errors are AMBIGUOUS → fail closed, sanitised
            logger.warning("hosted host-executor: transport failed op=%s account=%s corr=%s",
                           operation, self.account_id, self.correlation_id)
            return {"ok": False, "reason": "host_unavailable"}
        try:
            result = verify_hosted_response(resp, correlation_id=self.correlation_id, nonce=nonce,
                                            keyring=self.keyring)
        except HostProtocolError as e:
            return {"ok": False, "reason": e.reason_code}
        if "ok" not in result:
            result["ok"] = False
        return result

    # ── HostExecutor protocol (what prepare_hosted_slot calls) ────────────────────────────────────────────
    def materialise_identity(self, spec, rdp_host=None) -> dict:
        if int(spec.get("account_id", -1)) != self.account_id or not self._confined(
                username=spec.get("windows_username"), runtime_root=spec.get("runtime_root")):
            return {"ok": False, "reason": "confinement_mismatch"}
        password = spec.get("password") or ""
        if not password:
            return {"ok": False, "reason": "password_absent"}
        nonce = secrets.token_hex(16)
        try:
            envelope = self._seal_password(
                str(password).encode("utf-8"), account_id=self.account_id,
                correlation_id=self.correlation_id, nonce=nonce)
        except Exception:  # noqa: BLE001 — sealing failure is fail-closed + sanitised (never logs the password)
            return {"ok": False, "reason": "seal_failed"}
        return self._send("PROVISION_IDENTITY", payload=envelope, nonce=nonce)

    def apply_workspace_acl(self, plan, rdp_host=None) -> dict:
        if not self._confined(username=getattr(plan, "windows_username", None),
                              runtime_root=getattr(plan, "runtime_root", None)):
            return {"ok": False, "reason": "confinement_mismatch"}
        return self._send("APPLY_WORKSPACE_ACL")

    def rollback_workspace_acl(self, plan, rdp_host=None) -> dict:
        if not self._confined(username=getattr(plan, "windows_username", None),
                              runtime_root=getattr(plan, "runtime_root", None)):
            return {"ok": False, "reason": "confinement_mismatch"}
        return self._send("ROLLBACK_WORKSPACE_ACL")

    def populate_runtime(self, runtime_root, rdp_host=None) -> dict:
        if not self._confined(runtime_root=runtime_root):
            return {"ok": False, "reason": "confinement_mismatch"}
        return self._send("MATERIALISE_RUNTIME")

    def apply_autotrading_config(self, runtime_root, rdp_host=None) -> dict:
        if not self._confined(runtime_root=runtime_root):
            return {"ok": False, "reason": "confinement_mismatch"}
        return self._send("APPLY_AUTOTRADING_CONFIG")

    def grant_rdp(self, username, rdp_host=None) -> dict:
        if not self._confined(username=username):
            return {"ok": False, "reason": "confinement_mismatch"}
        return self._send("ENSURE_RDP_MEMBERSHIP")

    def enforce_single_session(self, rdp_host=None) -> dict:
        return self._send("ENSURE_SINGLE_SESSION")

    def verify_remoteapp(self, username, runtime_root, rdp_host=None) -> dict:
        if not self._confined(username=username, runtime_root=runtime_root):
            return {"ok": False, "reason": "confinement_mismatch"}
        return self._send("ENSURE_REMOTEAPP")

    def applocker_prepare(self, username, rdp_host=None) -> dict:
        if not self._confined(username=username):
            return {"ok": False, "reason": "confinement_mismatch"}
        return self._send("APPLY_APPLOCKER_AUDIT")

    def register_observer(self, username, runtime_root, rdp_host=None) -> dict:
        if not self._confined(username=username, runtime_root=runtime_root):
            return {"ok": False, "reason": "confinement_mismatch"}
        return self._send("PREPARE_OBSERVER")

    def verify_slot(self, rdp_host=None) -> dict:
        return self._send("VERIFY_SLOT")

    def activate_order_bridge(self, runtime_root, rdp_host=None) -> dict:
        """FINAL Closed-Beta stream: ask the host to activate THIS node's dedicated pin-enforcing order bridge
        and health-check it. Confined on ``runtime_root`` (Django layer) + Customer-Zero refused in ``_send``;
        the host re-derives the slot (terminal_root + account_id) from ``account_id`` and refuses CZ again.
        Sends NO params — every identity/path is server-derived, like ``populate_runtime``. Returns the
        sanitised signed result (``{"ok": bool, ...}``); the caller derives + persists the endpoint itself."""
        if not self._confined(runtime_root=runtime_root):
            return {"ok": False, "reason": "confinement_mismatch"}
        return self._send("ACTIVATE_ORDER_BRIDGE")

    def observe(self, rdp_host=None) -> dict:
        """9E READ-ONLY live observation: ask the host to trigger THIS account's session-bound observer task
        (running as guvfx_u_<id>, guarded-attach to its already-running MT5) and return the resulting snapshot.
        The account identity is bound server-side in the signed request (never a caller path/user/task); the
        host derives the slot and refuses Customer Zero. Returns the sanitised, signed result dict (the
        RawWorkspaceSnapshot fields) or a fail-closed ``{"ok": False, ...}`` on any transport/host failure.
        This is the ONLY executor method that carries no confinement args because it supplies none — every
        identity/path is host-derived from ``account_id``. It NEVER logs in, NEVER trades, NEVER mutates."""
        return self._send("OBSERVE_WORKSPACE")


# ── DARK factory ─────────────────────────────────────────────────────────────────────────────────────────
def _default_seal_password(password_bytes, *, account_id, correlation_id, nonce):
    """Seal the Windows account password to the host's public key (ADR-0027 envelope, its OWN key registry —
    RULE 3/6: not conflated with broker-cred keys). The backend cannot decrypt what it sealed."""
    from terminal_provisioning.broker_cred_envelope import bind_aad, seal
    import json
    import os

    from django.conf import settings
    key_id = (getattr(settings, "HOSTED_EXECUTOR_ENC_KEY_ID", None)
              or os.getenv("HOSTED_EXECUTOR_ENC_KEY_ID", "")).strip()
    raw = getattr(settings, "HOSTED_EXECUTOR_ENC_PUBKEYS", None) or os.getenv("HOSTED_EXECUTOR_ENC_PUBKEYS", "")
    pubs = json.loads(raw) if raw else {}
    if not key_id or key_id not in pubs:
        raise HostProtocolError("envelope_key_unconfigured")
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
    import base64
    pub = X25519PublicKey.from_public_bytes(base64.b64decode(str(pubs[key_id]).encode("ascii"), validate=True))
    aad = bind_aad(operation="PROVISION_IDENTITY", runtime_uuid=f"account:{account_id}",
                   correlation_id=correlation_id, nonce=nonce)
    return seal(password_bytes, aad=aad, key_id=key_id, recipient_public_key=pub)


def resolve_signed_host_executor(*, account_id, rdp_host=""):
    """Return a live ``SignedHostExecutor`` iff the engine is armed AND fully configured; else ``None`` (DARK).
    Configuration is loaded from settings/env, never hard-coded, never logged."""
    import json
    import os

    from django.conf import settings

    from hosted_workspace.flags import hosted_host_executor_enabled
    if not hosted_host_executor_enabled():
        return None
    raw = getattr(settings, "HOSTED_EXECUTOR_KEYRING", None) or os.getenv("HOSTED_EXECUTOR_KEYRING", "")
    key_id = (getattr(settings, "HOSTED_EXECUTOR_KEY_ID", None) or os.getenv("HOSTED_EXECUTOR_KEY_ID", "")).strip()
    base_url = (getattr(settings, "HOSTED_EXECUTOR_BASE_URL", None)
                or os.getenv("HOSTED_EXECUTOR_BASE_URL", "")).strip()
    try:
        keyring = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return None
    if not keyring or key_id not in keyring or not base_url:
        return None   # incomplete config → stay dark (fail closed), never a half-armed executor
    return SignedHostExecutor(
        account_id=account_id, rdp_host=rdp_host, transport=_http_transport,
        keyring=keyring, key_id=key_id, base_url=base_url, seal_password=_default_seal_password)


# Per-operation HTTP read timeout (seconds). MATERIALISE_RUNTIME copies the ~378MB golden runtime and can far
# exceed the default; the host daemon's own primitive timeout is 600s and it drains in-flight work on stop, so
# the client must wait long enough for a single synchronous response rather than give up early (a false
# host_unavailable that leaves the slot un-advanced). There is NO repost/retry loop here — exactly one POST per
# step — so a longer wait can never re-run a materialise; it only avoids a premature client-side timeout.
_DEFAULT_HTTP_TIMEOUT_S = 30
# OBSERVE_WORKSPACE is served SYNCHRONOUSLY: Invoke-GuvfxObserver.ps1 triggers the session-bound observer and
# blocks polling for its result up to -TimeoutSeconds (default 60s) plus process-enumeration + a bounded
# network-corroboration query, so the client must wait longer than that whole host budget or a legitimately
# slow-but-successful observation is falsely reported host_unavailable and the slot never advances (the same
# false-negative class as MATERIALISE_RUNTIME). 120s > the host's 60s wait + margin, and < the daemon's 600s
# primitive timeout. There is NO repost/retry here — exactly one POST per cycle — so a longer wait cannot re-run.
_OP_HTTP_TIMEOUTS_S = {"MATERIALISE_RUNTIME": 660, "OBSERVE_WORKSPACE": 120}


def _http_transport(base_url: str, request: dict) -> dict:
    """POST a signed request to the host provisioning agent and return its JSON response. Kept minimal; the
    executor treats ANY exception here as an ambiguous host-unavailable (fail closed). The read timeout is
    per-operation so a long MATERIALISE_RUNTIME is not falsely reported as host-unavailable."""
    import json
    import urllib.request
    data = json.dumps(request).encode("utf-8")
    timeout = _OP_HTTP_TIMEOUTS_S.get(str(request.get("operation") or ""), _DEFAULT_HTTP_TIMEOUT_S)
    req = urllib.request.Request(base_url.rstrip("/") + "/hosted/provision", data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310 — host is a fixed Tailscale peer over TLS
        return json.loads(resp.read().decode("utf-8"))
