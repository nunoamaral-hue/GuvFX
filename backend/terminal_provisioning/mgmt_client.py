"""CVM-Inc-3 B — backend MANAGEMENT-CHANNEL client (a ``WindowsProvisioner`` over the signed protocol).

The backend side of the privileged channel. It builds a fixed-schema, signed, single-use request per
operation (never a command/path/argument) and hands it to a transport that reaches the private-network
agent. It trusts NOTHING in the response beyond the allowlisted sanitised fields, and treats a transport
timeout as AMBIGUOUS (never as proof of failure) — the worker reconciles those.
"""
from django.conf import settings
from django.utils import timezone

from .mgmt_protocol import DEFAULT_TTL_SECONDS, ProtocolError, sign_request


class ManagementChannelError(Exception):
    """A management-channel call was denied or failed. ``reason_code`` is sanitised."""
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


class ManagementChannelTimeout(Exception):
    """The call did not return in time — AMBIGUOUS (the op may or may not have executed). The worker must
    reconcile (e.g. VERIFY) before any retry; it must NOT be interpreted as failure."""


def _load_keyring() -> tuple[dict, str]:
    """Load the signing keyring + active key id from settings/env (never hard-coded, never logged)."""
    import json
    import os
    raw = getattr(settings, "BETA_AGENT_KEYRING", None) or os.getenv("BETA_AGENT_KEYRING", "")
    active = getattr(settings, "BETA_AGENT_KEY_ID", None) or os.getenv("BETA_AGENT_KEY_ID", "")
    keyring = json.loads(raw) if raw else {}
    return keyring, active


class AgentWindowsProvisioner:
    """Implements the provisioner interface (materialise/configure/start/verify/stop/teardown) by calling
    the Windows agent over the signed channel. Bound to ONE ``ProvisioningJob`` so the agent can key its
    idempotency on (job_id, operation) — a retry of the same job re-sends the same op and the agent
    returns the stored result instead of re-running it (no double launch)."""

    def __init__(self, *, job_id: int, transport, keyring=None, key_id=None, correlation_id: str = "",
                 base_url: str = "", ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.job_id = int(job_id)
        self.transport = transport            # callable(base_url, request_dict) -> response_dict
        if keyring is None or key_id is None:
            keyring, key_id = _load_keyring()
        self.keyring = keyring
        self.key_id = key_id
        self.correlation_id = correlation_id or f"job-{job_id}"
        self.base_url = base_url or (getattr(settings, "BETA_AGENT_BASE_URL", "")
                                     or __import__("os").getenv("BETA_AGENT_BASE_URL", ""))
        self.ttl_seconds = ttl_seconds

    def _call(self, operation: str, runtime) -> dict:
        try:
            req = sign_request(
                provisioning_job_id=self.job_id, runtime_uuid=str(runtime.runtime_uuid),
                operation=operation, correlation_id=self.correlation_id,
                keyring=self.keyring, key_id=self.key_id,
                now=int(timezone.now().timestamp()), ttl_seconds=self.ttl_seconds)
        except ProtocolError as e:
            raise ManagementChannelError(e.reason_code)
        resp = self.transport(self.base_url, req)      # may raise ManagementChannelTimeout
        if not isinstance(resp, dict):
            raise ManagementChannelError("bad_agent_response")
        if resp.get("outcome") != "ok":
            raise ManagementChannelError(resp.get("reason_code") or "agent_denied")
        return resp

    # ── WindowsProvisioner interface ──
    def materialise(self, runtime) -> None:
        self._call("MATERIALISE", runtime)

    def configure(self, runtime, *, login, server, password) -> None:
        # Broker-INDEPENDENT walk: the golden terminal has NO saved broker identity, so NO credentials are
        # ever sent over the channel. (When the later broker-login stage arrives, credential provisioning
        # will use a dedicated, separately-reviewed secure path — never this signed control channel.)
        return None

    def start(self, runtime) -> None:
        self._call("START", runtime)

    def verify(self, runtime) -> dict:
        r = self._call("VERIFY", runtime)
        return {
            "running": bool(r.get("running")),
            "logged_in": bool(r.get("logged_in")),   # always False in the broker-independent phase
            "login": None,
            "server": None,
            "pid": r.get("pid"),
            "session": r.get("session_id"),
            "script_version": r.get("script_version", ""),
            "agent_version": r.get("agent_version", ""),
        }

    def stop(self, runtime) -> None:
        self._call("STOP", runtime)

    def teardown(self, runtime) -> None:
        # TOMBSTONE = stop the bound PID + quarantine the runtime dir (NEVER an arbitrary recursive delete).
        self._call("TOMBSTONE", runtime)
