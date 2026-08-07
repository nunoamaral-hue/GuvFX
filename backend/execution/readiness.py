"""ADR-0033 — execution-readiness provider abstraction.

TWO readiness models coexist behind the ONE central execution gate (``broker_gate``), selected per account
by ``TradingAccount.readiness_provider``. Both return the SAME immutable ``ReadinessDecision`` consumed by
``evaluate_execution_gate``. Neither weakens the order-time bridge gate (``evaluate_binding`` in
``scripts/mt5_signal_bridge.py``), which remains the AUTHORITATIVE live check immediately before every
``order_send`` (with a mandatory per-job identity pin — ADR-0033 Tension 3).

Provider A — ``temporary_validation`` (the default; ALL existing accounts): the EXISTING behaviour,
UNCHANGED. ``password_enc`` present + ``validation_status == VALIDATED`` (plus the shared
``is_active`` / ``disconnected_at`` checks). Regression-identical to the pre-ADR-0033 gate.

Provider B — ``persistent_workspace``: attach-verified readiness — a durable ``HostedMt5Workspace`` in an
execution-capable lifecycle whose LAST attach observation was a positive active-account match, connected
and trade-allowed, within a freshness bound. It requires **NO** ``password_enc`` and **NO** temporary
``VALIDATED`` (ADR-0033 Q1) — but it is **ANDed with, never substituted for**, the lifecycle checks
``is_active`` / ``disconnected_at`` (and, at dispatch, ``BrokerAccountHealth`` / ``BrokerRuntimePause``,
which ``broker_gate`` continues to apply to BOTH providers) — ADR-0033 Tension 1 / red-team finding #1.

Cache-is-not-authority (red-team finding on stale observation): the workspace's observed_* fields are a
CACHE. They gate ELIGIBILITY here (analogous to how ``VALIDATED`` is a historical eligibility fact); the
LIVE order-time bridge gate is the authority. Provider B therefore requires the last observation to be
FRESH, and ``HostedMt5Workspace.is_execution_ready`` / observer state can never by itself authorise an
order.
"""
from __future__ import annotations

from dataclasses import dataclass

# Provider identifiers (mirror TradingAccount.ReadinessProvider values).
TEMPORARY_VALIDATION = "temporary_validation"
PERSISTENT_WORKSPACE = "persistent_workspace"

# ── Provider-B (persistent workspace) reason codes — stable, non-secret, customer-safe ──
RW_SUBSYSTEM_DISABLED = "workspace_subsystem_disabled"
RW_WORKSPACE_MISSING = "workspace_missing"
RW_WORKSPACE_NOT_READY = "workspace_not_execution_ready"
RW_WORKSPACE_NOT_CONNECTED = "workspace_not_connected"
RW_ACTIVE_ACCOUNT_MISMATCH = "active_account_mismatch"
RW_OBSERVATION_STALE = "workspace_observation_stale"

# The last attach observation must be no older than this to gate eligibility (mirrors the runtime
# heartbeat freshness). It is an ELIGIBILITY bound only — the authority is the live order-time gate.
WORKSPACE_OBSERVATION_FRESH_SECONDS = 300

# WorkspaceState.CONNECTED value (kept as a literal to avoid importing the model app at module load).
_STATE_CONNECTED = "CONNECTED"


@dataclass(frozen=True)
class ReadinessDecision:
    """The common contract both providers return, consumed by ``evaluate_execution_gate``."""
    eligible: bool
    reason_code: str
    provider: str

    def as_dict(self) -> dict:
        return {"eligible": self.eligible, "reason_code": self.reason_code, "provider": self.provider}


def _hosted_persistent_mt5_enabled() -> bool:
    """DARK master gate for the whole persistent-workspace subsystem (import-local; fail-closed)."""
    try:
        from hosted_workspace.flags import hosted_persistent_mt5_enabled
        return hosted_persistent_mt5_enabled()
    except Exception:  # noqa: BLE001 — absence of the subsystem means it is disabled
        return False


class TemporaryValidationProvider:
    """Provider A. Wraps the pre-ADR-0033 gate logic EXACTLY (regression-identical). Returns the existing
    creation-gate reason codes so downstream mapping/audit/tests are unchanged."""

    key = TEMPORARY_VALIDATION

    def evaluate(self, account) -> ReadinessDecision:
        # Import broker_gate lazily to avoid an import cycle (broker_gate imports this module's
        # ``evaluate_readiness`` lazily inside the gate function).
        from execution import broker_gate as g
        if not getattr(account, "is_active", False):
            return ReadinessDecision(False, g.R_ACCOUNT_INACTIVE, self.key)
        if getattr(account, "disconnected_at", None) is not None:
            return ReadinessDecision(False, g.R_ACCOUNT_DISCONNECTED, self.key)
        if not (getattr(account, "password_enc", "") or ""):
            return ReadinessDecision(False, g.R_CREDENTIAL_MISSING, self.key)
        status = getattr(account, "validation_status", None)
        if status == g._VS.VALIDATED:
            return ReadinessDecision(True, g.GATE_OK, self.key)
        return ReadinessDecision(False, g._NOT_VALIDATED.get(status, g.R_VALIDATION_STATE_UNKNOWN), self.key)


class PersistentWorkspaceProvider:
    """Provider B. Attach-verified readiness. Requires NO password_enc / VALIDATED, but is ANDed with the
    shared lifecycle checks (is_active / disconnected_at). Fail-closed on every ambiguity. DARK unless
    ``HOSTED_PERSISTENT_MT5_ENABLED`` is on (so a mis-set account can never become ready while the
    subsystem is dark).

    Check order makes every reason code reachable and reports the MOST SPECIFIC failure — in particular a
    wrong active account reports ``active_account_mismatch`` (the central failure mode of the attach
    model), rather than collapsing into a generic not-ready."""

    key = PERSISTENT_WORKSPACE

    def evaluate(self, account) -> ReadinessDecision:
        from execution import broker_gate as g
        if not _hosted_persistent_mt5_enabled():
            return ReadinessDecision(False, RW_SUBSYSTEM_DISABLED, self.key)
        # Shared lifecycle checks — ANDed, never dropped (red-team finding #1).
        if not getattr(account, "is_active", False):
            return ReadinessDecision(False, g.R_ACCOUNT_INACTIVE, self.key)
        if getattr(account, "disconnected_at", None) is not None:
            return ReadinessDecision(False, g.R_ACCOUNT_DISCONNECTED, self.key)
        ws = getattr(account, "hosted_workspace", None)
        if ws is None:
            return ReadinessDecision(False, RW_WORKSPACE_MISSING, self.key)
        # Attach truth from the LAST observation (a cache; the live gate is the authority). Fail-closed,
        # most-specific-first so each reason code is reachable.
        if ws.observed_connected is not True or ws.observed_trade_allowed is not True:
            return ReadinessDecision(False, RW_WORKSPACE_NOT_CONNECTED, self.key)
        if ws.active_account_match is not True:
            return ReadinessDecision(False, RW_ACTIVE_ACCOUNT_MISMATCH, self.key)
        if getattr(ws, "state", "") != _STATE_CONNECTED:
            return ReadinessDecision(False, RW_WORKSPACE_NOT_READY, self.key)
        if not _observation_fresh(ws):
            return ReadinessDecision(False, RW_OBSERVATION_STALE, self.key)
        return ReadinessDecision(True, g.GATE_OK, self.key)


def _observation_fresh(ws) -> bool:
    """True only when the workspace's last attach observation is recent enough to gate eligibility.

    NOTE for the future writer (deferred increment): ``last_observed_at`` MUST be updated atomically with
    the ``observed_*`` snapshot it dates, or a stale snapshot could ride a fresh timestamp."""
    from django.utils import timezone
    ts = getattr(ws, "last_observed_at", None)
    if ts is None:
        return False
    age = (timezone.now() - ts).total_seconds()
    return 0 <= age <= WORKSPACE_OBSERVATION_FRESH_SECONDS


_PROVIDERS = {
    TEMPORARY_VALIDATION: TemporaryValidationProvider(),
    PERSISTENT_WORKSPACE: PersistentWorkspaceProvider(),
}


def provider_for(account):
    """Select the readiness provider for an account. Unknown / unset ⇒ the temporary provider (safe
    default; every existing account is temporary)."""
    key = getattr(account, "readiness_provider", TEMPORARY_VALIDATION) or TEMPORARY_VALIDATION
    return _PROVIDERS.get(key, _PROVIDERS[TEMPORARY_VALIDATION])


def evaluate_readiness(account) -> ReadinessDecision:
    """The single entry point ``evaluate_execution_gate`` delegates to. Selects the provider and returns
    its decision. Callers must still apply the flag gate + account-present check + (at dispatch) the
    health/pause convergence — this only replaces the password_enc/VALIDATED eligibility layer."""
    return provider_for(account).evaluate(account)
