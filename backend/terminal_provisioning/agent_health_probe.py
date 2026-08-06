"""Minimum-hardening WS-B — backend signed-NEGOTIATE health/readiness probe for the beta validation agent.

The agent exposes NO unauthenticated /health (design §4). The ONLY sanctioned liveness signal is a *signed*
NEGOTIATE handshake to ``:8791``. This module performs that probe with a bounded, split connect/read timeout
and classifies the result into EIGHT distinguished states, so an operator can tell:

  - a down listener (connect fails)           → UNREACHABLE
  - a live socket that won't handshake         → LISTENING_NO_NEGOTIATE
  - a contract/version mismatch                → INCOMPATIBLE
  - an UNSUPERVISED ad-hoc ``python agent.py`` → UNSUPERVISED  (the Aug-5 vector — never HEALTHY)
  - a supervised agent that cannot yet validate→ READY_UNARMED (up but keyring/op unarmed)
  - a fully sanctioned, armed agent            → HEALTHY
  - an older agent that cannot prove supervision→ SUPERVISION_UNKNOWN (conservatively DEGRADED)
  - probe misconfiguration (no url/keyring)    → UNCONFIGURED (fail-closed)

It is READ-ONLY and SIDE-EFFECT-FREE: no broker credential, no MT5 login, no mutation, no attempt row, no
order, no DB write. Persistence/scheduling live above it. The connect/read split is implemented LOCALLY here
(main does not carry the PR-#290 transport-taxonomy split), so this module has no dependency on the
``validation_agent_unreachable``/``validation_agent_timeout`` reason codes.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from .mgmt_protocol import PROTOCOL_VERSION, PROVISIONING_OPERATIONS

# Probe timeout budget — SEPARATE from the long provisioning read budget. Connecting to a live socket is
# fast; a 10s connect ceiling turns a down agent into a bounded UNREACHABLE rather than a hung probe. The
# read budget is short too: NEGOTIATE is a shallow handshake, not a validation.
PROBE_CONNECT_TIMEOUT = 5
PROBE_READ_TIMEOUT = 10

# The eight distinguished readiness states (design §4 health model, projected to a probe outcome).
UNCONFIGURED = "UNCONFIGURED"
UNREACHABLE = "UNREACHABLE"
LISTENING_NO_NEGOTIATE = "LISTENING_NO_NEGOTIATE"
INCOMPATIBLE = "INCOMPATIBLE"
UNSUPERVISED = "UNSUPERVISED"
SUPERVISION_UNKNOWN = "SUPERVISION_UNKNOWN"
READY_UNARMED = "READY_UNARMED"
HEALTHY = "HEALTHY"

STATES = (UNCONFIGURED, UNREACHABLE, LISTENING_NO_NEGOTIATE, INCOMPATIBLE, UNSUPERVISED,
          SUPERVISION_UNKNOWN, READY_UNARMED, HEALTHY)

# Coarse operator band (health-model.json): what pages, what warns, what is fine.
BAND_HEALTHY = "HEALTHY"
BAND_DEGRADED = "DEGRADED"
BAND_UNAVAILABLE = "UNAVAILABLE"

# UNSUPERVISED is deliberately UNAVAILABLE, not DEGRADED: an unsanctioned listener answering :8791 is the
# exact failure we are hardening against, and must page — never be mistaken for a healthy service.
_BAND = {
    UNCONFIGURED: BAND_UNAVAILABLE, UNREACHABLE: BAND_UNAVAILABLE,
    LISTENING_NO_NEGOTIATE: BAND_UNAVAILABLE, INCOMPATIBLE: BAND_UNAVAILABLE,
    UNSUPERVISED: BAND_UNAVAILABLE, SUPERVISION_UNKNOWN: BAND_DEGRADED,
    READY_UNARMED: BAND_DEGRADED, HEALTHY: BAND_HEALTHY,
}


class _ProbeUnreachable(Exception):
    """Connect phase failed — the socket is not accepting (process/socket down or blocked)."""


class _ProbeReadTimeout(Exception):
    """Connected, but no response within the read budget — a live socket that is not answering."""


class _ProbeTransportError(Exception):
    """A non-timeout transport failure (reset, TLS, malformed) — treated as not-reachable but distinct."""


@dataclass(frozen=True)
class AgentReadiness:
    """One sanitised readiness observation. Carries ONLY non-secret facts."""
    state: str
    band: str
    supervised: object                 # True / False / None (unknown — older bundle)
    validate_login_available: bool
    reason: str                        # sanitised code, never a raw agent/exception string
    correlation_id: str
    elapsed_ms: int
    probed_at: float
    layers: dict = field(default_factory=dict)

    @property
    def is_healthy(self) -> bool:
        return self.state == HEALTHY

    @property
    def is_unavailable(self) -> bool:
        return self.band == BAND_UNAVAILABLE

    def as_dict(self) -> dict:
        return {"state": self.state, "band": self.band, "supervised": self.supervised,
                "validate_login_available": self.validate_login_available, "reason": self.reason,
                "correlation_id": self.correlation_id, "elapsed_ms": self.elapsed_ms,
                "probed_at": self.probed_at, "layers": dict(self.layers)}


def _default_transport(base_url: str, req: dict):
    """Real signed-NEGOTIATE transport with a SPLIT connect/read timeout, classifying the failure mode.
    Imported lazily so the module loads without ``requests`` in pure-logic tests."""
    import requests

    from .mgmt_client import provision_url
    try:
        # Target the agent's SINGLE route via the shared helper — the agent serves ONLY ``POST /provision``
        # and 404s (``unknown_route``) any other path. Posting to the bare ``base_url`` was the defect that
        # route-rejected every probe; this now derives the URL identically to the provisioning transport.
        resp = requests.post(provision_url(base_url), json=req,
                             timeout=(PROBE_CONNECT_TIMEOUT, PROBE_READ_TIMEOUT))
    except requests.exceptions.ConnectionError as exc:        # incl. ConnectTimeout — connect phase failed
        raise _ProbeUnreachable() from exc
    except requests.exceptions.ReadTimeout as exc:            # connected, no answer in the read budget
        raise _ProbeReadTimeout() from exc
    except requests.exceptions.Timeout as exc:                # any other timeout → treat as read-side
        raise _ProbeReadTimeout() from exc
    except requests.exceptions.RequestException as exc:       # reset / TLS / malformed
        raise _ProbeTransportError() from exc
    try:
        return resp.json()
    except ValueError as exc:
        raise _ProbeTransportError() from exc


def _resolve_config(base_url, keyring, key_id):
    """Resolve base_url + signing keyring, fail-closed (UNCONFIGURED) if anything required is missing."""
    import json
    import os

    from django.conf import settings
    if base_url is None:
        base_url = getattr(settings, "BETA_AGENT_BASE_URL", "") or os.getenv("BETA_AGENT_BASE_URL", "")
    if keyring is None or key_id is None:
        raw = getattr(settings, "BETA_AGENT_KEYRING", None) or os.getenv("BETA_AGENT_KEYRING", "")
        key_id = key_id or getattr(settings, "BETA_AGENT_KEY_ID", None) or os.getenv("BETA_AGENT_KEY_ID", "")
        try:
            keyring = json.loads(raw) if raw else {}
        except ValueError:
            keyring = {}
    return base_url, (keyring or {}), (key_id or "")


def _classify(info: dict, *, correlation_id, elapsed_ms, now) -> AgentReadiness:
    """Turn a successful NEGOTIATE handshake dict into a readiness state. Pure — no I/O."""
    layers = {"process_running": True, "socket_listening": True, "negotiate_ok": True}
    # Contract compatibility — replicate assert_compatible's checks so an INCOMPATIBLE agent is a DISTINCT
    # state, not a generic failure.
    compatible = (
        info.get("protocol_version") == PROTOCOL_VERSION
        and set(PROVISIONING_OPERATIONS).issubset(set(info.get("supported_operations") or []))
        and bool(info.get("agent_version")) and info.get("manifest_version") is not None)
    layers["contract_compatible"] = compatible
    if not compatible:
        return _make(INCOMPATIBLE, supervised=info.get("agent_supervised"), vla=False,
                     reason="contract_incompatible", cid=correlation_id, ms=elapsed_ms, now=now, layers=layers)

    supervised = info.get("agent_supervised", None)     # True / False / absent(None)
    vla = "VALIDATE_LOGIN" in set(info.get("supported_operations") or [])
    layers["validate_login_available"] = vla
    layers["supervised"] = supervised

    if supervised is False:
        # A listener that is provably NOT the sanctioned service — the Aug-5 vector. Never HEALTHY.
        return _make(UNSUPERVISED, supervised=False, vla=vla, reason="unsupervised_listener",
                     cid=correlation_id, ms=elapsed_ms, now=now, layers=layers)
    if supervised is None:
        # Older agent bundle that cannot attest supervision — conservatively DEGRADED, distinct from a
        # confirmed unsupervised listener (do NOT assume healthy).
        return _make(SUPERVISION_UNKNOWN, supervised=None, vla=vla, reason="supervision_unknown",
                     cid=correlation_id, ms=elapsed_ms, now=now, layers=layers)
    if not vla:
        return _make(READY_UNARMED, supervised=True, vla=False, reason="validate_login_unavailable",
                     cid=correlation_id, ms=elapsed_ms, now=now, layers=layers)
    return _make(HEALTHY, supervised=True, vla=True, reason="ok",
                 cid=correlation_id, ms=elapsed_ms, now=now, layers=layers)


def _make(state, *, supervised, vla, reason, cid, ms, now, layers) -> AgentReadiness:
    return AgentReadiness(state=state, band=_BAND[state], supervised=supervised,
                          validate_login_available=bool(vla), reason=reason, correlation_id=cid,
                          elapsed_ms=int(ms), probed_at=float(now), layers=layers)


def probe_agent_readiness(*, base_url=None, keyring=None, key_id=None, transport=None,
                          now_fn=None, clock=None) -> AgentReadiness:
    """Perform ONE signed-NEGOTIATE readiness probe and return a classified :class:`AgentReadiness`.

    Injectables (all optional; defaults hit settings/env + the network):
      - ``transport(base_url, req) -> dict`` may raise ``_ProbeUnreachable``/``_ProbeReadTimeout``/
        ``_ProbeTransportError`` (or ``requests`` exceptions via the default). Tests pass a fake.
      - ``now_fn() -> epoch`` timestamp; ``clock() -> monotonic`` for elapsed_ms.
    Never raises for an agent-side condition — every failure maps to a state (fail-closed to not-HEALTHY)."""
    now_fn = now_fn or time.time
    clock = clock or time.monotonic
    correlation_id = f"agent-probe-{uuid.uuid4().hex[:12]}"
    base_url, keyring, key_id = _resolve_config(base_url, keyring, key_id)
    now = now_fn()
    if not base_url or not keyring or not key_id:
        return _make(UNCONFIGURED, supervised=None, vla=False, reason="probe_unconfigured",
                     cid=correlation_id, ms=0, now=now, layers={"process_running": None})

    from .mgmt_client import AgentWindowsProvisioner, ManagementChannelError
    provisioner = AgentWindowsProvisioner(
        job_id=0, transport=(transport or _default_transport), keyring=keyring, key_id=key_id,
        correlation_id=correlation_id, base_url=base_url)
    started = clock()
    try:
        info = provisioner.negotiate()
    except _ProbeUnreachable:
        return _fail(UNREACHABLE, "agent_unreachable", correlation_id, started, clock, now,
                     layers={"process_running": False, "socket_listening": False})
    except _ProbeReadTimeout:
        return _fail(LISTENING_NO_NEGOTIATE, "negotiate_read_timeout", correlation_id, started, clock, now,
                     layers={"process_running": True, "socket_listening": True, "negotiate_ok": False})
    except _ProbeTransportError:
        return _fail(UNREACHABLE, "transport_error", correlation_id, started, clock, now,
                     layers={"process_running": False, "socket_listening": None})
    except ManagementChannelError as exc:
        # Reached the agent but it DENIED the signed handshake (auth/integrity) — up but not negotiating.
        return _fail(LISTENING_NO_NEGOTIATE, _safe_reason(getattr(exc, "reason_code", "negotiate_denied")),
                     correlation_id, started, clock, now,
                     layers={"process_running": True, "socket_listening": True, "negotiate_ok": False})
    elapsed_ms = int((clock() - started) * 1000)
    if not isinstance(info, dict):
        return _fail(LISTENING_NO_NEGOTIATE, "bad_agent_response", correlation_id, started, clock, now,
                     layers={"process_running": True, "socket_listening": True, "negotiate_ok": False})
    return _classify(info, correlation_id=correlation_id, elapsed_ms=elapsed_ms, now=now)


def _fail(state, reason, cid, started, clock, now, *, layers) -> AgentReadiness:
    return _make(state, supervised=None, vla=False, reason=reason, cid=cid,
                 ms=int((clock() - started) * 1000), now=now, layers=layers)


_ALLOWED_REASON = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def _safe_reason(code) -> str:
    """Reduce any reason to a short sanitised token (no raw agent/exception text ever surfaces)."""
    s = "".join(c for c in str(code) if c in _ALLOWED_REASON)[:64]
    return s or "negotiate_denied"


# ── scheduler cadence (pure) — WS-B cadence + recovery hysteresis, no threads/DB here ──
CADENCE_HEALTHY_S = 60
CADENCE_DEGRADED_S = 30
CADENCE_UNAVAILABLE_BASE_S = 30
CADENCE_UNAVAILABLE_CAP_S = 300     # exponential backoff cap (5 min)
RECOVERY_CONSECUTIVE_SUCCESSES = 2  # consecutive HEALTHY probes required to declare recovery


def next_probe_delay_seconds(band: str, consecutive_unavailable: int = 0) -> int:
    """Cadence for the next probe: HEALTHY 60s, DEGRADED 30s, UNAVAILABLE exponential backoff from 30s
    capped at 300s. ``consecutive_unavailable`` drives the backoff (0 => base)."""
    if band == BAND_HEALTHY:
        return CADENCE_HEALTHY_S
    if band == BAND_DEGRADED:
        return CADENCE_DEGRADED_S
    n = max(0, int(consecutive_unavailable))
    return min(CADENCE_UNAVAILABLE_CAP_S, CADENCE_UNAVAILABLE_BASE_S * (2 ** min(n, 10)))


@dataclass
class ReadinessTracker:
    """Pure hysteresis over a stream of probe bands: an alert FIRES on the first UNAVAILABLE and CLEARS only
    after ``RECOVERY_CONSECUTIVE_SUCCESSES`` consecutive HEALTHY probes (consecutive-success recovery — a
    single lucky probe does not clear an outage). Also counts consecutive-unavailable for backoff, and
    detects up→down→up flap (crash-loop signal)."""
    consecutive_unavailable: int = 0
    consecutive_healthy: int = 0
    alerting: bool = False
    last_band: str = ""
    up_down_up: int = 0

    def observe(self, band: str) -> dict:
        prev = self.last_band
        if band == BAND_UNAVAILABLE:
            self.consecutive_unavailable += 1
            self.consecutive_healthy = 0
            if not self.alerting:
                self.alerting = True
        elif band == BAND_HEALTHY:
            self.consecutive_healthy += 1
            if prev == BAND_UNAVAILABLE:
                self.up_down_up += 1                 # recovered from an outage — a restart-loop tick
            self.consecutive_unavailable = 0
            if self.alerting and self.consecutive_healthy >= RECOVERY_CONSECUTIVE_SUCCESSES:
                self.alerting = False
        else:  # DEGRADED — neither confirms recovery nor counts as an outage for backoff
            self.consecutive_healthy = 0
            self.consecutive_unavailable = 0
        self.last_band = band
        return {"alerting": self.alerting, "consecutive_unavailable": self.consecutive_unavailable,
                "consecutive_healthy": self.consecutive_healthy, "up_down_up": self.up_down_up,
                "next_delay_s": next_probe_delay_seconds(band, self.consecutive_unavailable)}
