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

Cache-is-not-authority (red-team finding on stale observation): the workspace's M3c canonical projection
(``canonical_state`` / ``proj_*`` / ``last_decision_at`` — ADR-0034 M3c) is a CACHE. It gates ELIGIBILITY
here (analogous to how ``VALIDATED`` is a historical eligibility fact); the LIVE order-time bridge gate is
the authority. Provider B therefore requires the last canonical decision to be FRESH, and no persisted
projection / observer state can by itself authorise an order.
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
RW_NOT_CONFIRMED = "workspace_account_not_confirmed"  # ADR-0034 Onboarding — customer human-ACK gate
RW_OBSERVATION_STALE = "workspace_observation_stale"
# ── ADR-0034 Execution Engine arming reason codes (Decision D conditions 2 / 4 / 11) ──
RW_EXECUTION_FEATURE_DISABLED = "workspace_execution_feature_disabled"  # condition 2 (subsystem-level flag)
RW_EXECUTION_DISABLED = "workspace_execution_disabled"                  # condition 4 (per-workspace arm)
RW_REAL_ACCOUNT_NOT_ENABLED = "real_account_not_enabled"               # condition 11 (demo-only, fail-closed)

# The last attach observation must be no older than this to gate eligibility (mirrors the runtime
# heartbeat freshness). It is an ELIGIBILITY bound only — the authority is the live order-time gate.
WORKSPACE_OBSERVATION_FRESH_SECONDS = 300


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


def _hosted_mt5_execution_enabled() -> bool:
    """DARK subsystem-level EXECUTION gate (ADR-0034 Decision D condition 2; import-local; fail-closed).
    Distinct from the master flag: observation may be on while execution stays dark."""
    try:
        from hosted_workspace.flags import hosted_mt5_execution_enabled
        return hosted_mt5_execution_enabled()
    except Exception:  # noqa: BLE001
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
        # ADR-0034 Execution Engine — LAYERED ARMING (Decision D). Every condition below is ANDed and
        # fail-closed; each field/flag defaults FALSE. This backend gate covers conditions 1-5 + 11; the
        # LIVE conditions 6-10 (guarded attach / live identity / connected / trade_allowed / health+pause)
        # remain the authority of the certified bridge gate immediately before every mutation.
        if not _hosted_persistent_mt5_enabled():          # condition 1 — global subsystem flag
            return ReadinessDecision(False, RW_SUBSYSTEM_DISABLED, self.key)
        if not _hosted_mt5_execution_enabled():           # condition 2 — subsystem execution flag
            return ReadinessDecision(False, RW_EXECUTION_FEATURE_DISABLED, self.key)
        # Shared lifecycle checks — ANDed, never dropped (red-team finding #1).
        if not getattr(account, "is_active", False):
            return ReadinessDecision(False, g.R_ACCOUNT_INACTIVE, self.key)
        if getattr(account, "disconnected_at", None) is not None:
            return ReadinessDecision(False, g.R_ACCOUNT_DISCONNECTED, self.key)
        if getattr(account, "is_demo", False) is not True:  # condition 11 — DEMO-ONLY subsystem, fail-closed
            return ReadinessDecision(False, RW_REAL_ACCOUNT_NOT_ENABLED, self.key)
        ws = getattr(account, "hosted_workspace", None)
        if ws is None:
            return ReadinessDecision(False, RW_WORKSPACE_MISSING, self.key)
        if getattr(ws, "execution_enabled", False) is not True:  # condition 4 — explicit per-workspace ARM
            return ReadinessDecision(False, RW_EXECUTION_DISABLED, self.key)
        # Attach truth from the M3c CANONICAL projection (ADR-0034 M3c) — the fields the ONE certified,
        # row-locked, single writer ``persist_workspace_decision`` actually maintains. (The legacy
        # ``observed_*``/``state``/``last_observed_at`` cache is deliberately NOT written by that writer and
        # must not be read here, or Provider B fail-closes forever — ADR-0034 Decision A.) This projection is
        # still a CACHE; the live order-time bridge gate (``evaluate_binding``) remains the authority.
        # Fail-closed, most-specific-first so each reason code stays reachable.
        if ws.proj_connected is not True:
            return ReadinessDecision(False, RW_WORKSPACE_NOT_CONNECTED, self.key)
        if ws.proj_account_match is not True:
            return ReadinessDecision(False, RW_ACTIVE_ACCOUNT_MISMATCH, self.key)
        # ADR-0034 Onboarding — the customer must EXPLICITLY confirm the discovered broker account is theirs
        # before it can ever arm (durable human ACK ``TradingAccount.workspace_confirmed_at``). This strictly
        # NARROWS eligibility — a fresh, connected, matched workspace still cannot become execution-ready on
        # observation alone; a human must have acknowledged it. Placed AFTER the match check so it is only
        # reached once there is a real observed account to confirm (both reason codes stay reachable), and
        # it is not the order-time authority — the live bridge gate remains that.
        if getattr(account, "workspace_confirmed_at", None) is None:
            return ReadinessDecision(False, RW_NOT_CONFIRMED, self.key)
        # Connected + matched but not canonically EXECUTION_READY (e.g. trading halted, or any non-ready
        # canonical state): not ready. ``canonical_execution_ready`` == (canonical_state == EXECUTION_READY),
        # which the M3a manager derives only when trade_allowed + fresh + the full conjunction held.
        if ws.proj_trade_allowed is not True or not ws.canonical_execution_ready:
            return ReadinessDecision(False, RW_WORKSPACE_NOT_READY, self.key)
        if not _observation_fresh(ws):
            return ReadinessDecision(False, RW_OBSERVATION_STALE, self.key)
        return ReadinessDecision(True, g.GATE_OK, self.key)


def _observation_fresh(ws) -> bool:
    """True only when the workspace's last CANONICAL decision is recent enough to gate eligibility.

    Uses ``last_decision_at`` — the timestamp the M3c writer stamps atomically with the canonical state +
    projection it dates (ADR-0034 M3c ``persist_workspace_decision``), so a stale projection can never ride a
    fresh timestamp. (This supersedes the legacy ``last_observed_at``, which the M3c writer does not touch.)"""
    from django.utils import timezone
    ts = getattr(ws, "last_decision_at", None)
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
