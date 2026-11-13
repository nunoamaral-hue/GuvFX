"""execution.monitoring_enrolment — generic, idempotent monitoring-ENROLMENT reconciliation (ADR-0103).

GFX-DASHBOARD-AUTO-MONITORING-ENROLMENT-087. Closes the systemic gap that a newly broker-connected MT5
account does NOT automatically become a complete dashboard data source. It does NOT invent a new onboarding
system: it is a selection loop that drives the EXISTING, already-built ADR-0075 primitives
(``monitoring_onboarding.reserve_route_for_workspace`` + the ``monitoring_allocation`` lifecycle) across
every eligible account, and a pure, read-only PROJECTION that lets the dashboard report each account's
monitoring stage truthfully — never silently omitting it, never fabricating a zero.

Two layers, deliberately separated by what they are allowed to touch:

* **Layer 1 — the readiness PROJECTION** (``evaluate_account_enrolment`` /
  ``evaluate_enrolment_for_accounts``). PURE and READ-ONLY. It reads only durable, production-ACTIVE records
  (the per-tenant ``HostedExecutionEndpoint`` state, the ``HostedMt5Workspace`` canonical projection, the
  latest ``AccountEquitySnapshot`` freshness) plus — when the ADR-0075 gateway subsystem is enabled — the
  durable ``MonitoringRoute`` readiness. It makes NO network call and NO host contact, and it NEVER changes
  which accounts are in scope — it ANNOTATES each account with a stable stage + reason + UI copy. The
  dashboard consumes it so "unavailable" is never indistinguishable from "zero". Safe when the whole
  ADR-0075 foundation is dark (there are then no routes; it derives the stage from the active endpoint /
  workspace / snapshot records).

* **Layer 2 — the idempotent RECONCILER** (``reconcile_enrolment``). DRY-RUN by default: it reports every
  account's stage. With ``apply=True`` AND the gateway + enrolment flags on (two-level darkness, like
  ``reconcile_monitoring_routes``) it drives ``reserve_route_for_workspace`` to idempotently create/register
  a deterministic ``MonitoringRoute`` from each eligible account's existing endpoint. Those primitives are
  themselves gated on ``monitoring_gateway_enabled()``, so with the gateway dark ``apply`` is a no-op. The
  reconciler writes only ``MonitoringRoute``/audit rows + governed ``reliability.AlertEvent`` rows; it
  contacts NO host, mutates NO Tailscale policy / bridge / account / workspace, places NO order, and adds NO
  production schedule by itself. Identity verification against a live gateway probe (the VERIFIED→READY
  step) remains the ADR-0075 gateway's job and a later Sponsor-gated deploy — this reconciler reserves and
  registers only; it never marks a route READY on its own.

Fail-closed, most-specific-first, secret-free: logins/account numbers never appear in a stage, reason code
or alert. Stage reason codes reuse the ADR-0075 ``RC_*`` vocabulary and the ADR-0072/0033 ``RW_*`` eligibility
codes — no new reason vocabulary is invented.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from execution import monitoring_models as mm
from execution.readiness import (
    PERSISTENT_WORKSPACE, RW_ACTIVE_ACCOUNT_MISMATCH, RW_NOT_CONFIRMED,
    RW_WORKSPACE_MISSING, RW_WORKSPACE_NOT_CONNECTED,
)

logger = logging.getLogger("guvfx.execution.monitoring.enrolment")

# Equity-snapshot freshness window used to judge "data is flowing" for an endpoint-READY account. Mirrors
# ``analytics.equity_snapshots.DEFAULT_FRESHNESS_S`` (the SAME window the equity-curve coverage uses) so the
# dashboard's "latest N of M" and this projection can never silently diverge. Kept as a local duplicate so
# ``execution`` does not import ``analytics`` (one-way dependency); the duplication is pinned against drift by
# ``tests_monitoring_enrolment.test_freshness_matches_equity_curve_window`` (asserts equality with the ledger).
SNAPSHOT_FRESHNESS_S = 900

# ── Enrolment stages (the per-account monitoring-readiness taxonomy; packet §1 table) ────────────────────────
# Exactly the Sponsor-enumerated stage vocabulary. Each is derived DETERMINISTICALLY from durable records; an
# indeterminate account is UNKNOWN (never a fabricated READY). STRUCTURALLY_EXCLUDED is reserved for an account
# that cannot be monitored on this path at all — never used merely because live positions are demo-gated (that
# is an orthogonal per-capability note; the account still monitors balance/equity/deals).
READY = "READY"
PROVISIONING = "PROVISIONING"
LOGIN_REQUIRED = "LOGIN_REQUIRED"
ENDPOINT_MISSING = "ENDPOINT_MISSING"
UNREACHABLE = "UNREACHABLE"
IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
SNAPSHOT_STALE = "SNAPSHOT_STALE"
STRUCTURALLY_EXCLUDED = "STRUCTURALLY_EXCLUDED"
TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"
UNKNOWN = "UNKNOWN"

# ── Pipeline steps (the packet's governed lifecycle) ─────────────────────────────────────────────────────────
PIPE_DISCOVERED = "DISCOVERED"
PIPE_ENDPOINT_ALLOCATED = "ENDPOINT_ALLOCATED"
PIPE_ROUTE_REGISTERED = "ROUTE_REGISTERED"
PIPE_REACHABLE = "REACHABLE"
PIPE_IDENTITY_VERIFIED = "IDENTITY_VERIFIED"
PIPE_MONITORING_READY = "MONITORING_READY"
PIPE_DASHBOARD_INCLUDED = "DASHBOARD_INCLUDED"

# stage -> (ui_state copy, structural, monitored, pipeline_step).
#   * ``ui_state`` is the safe, non-identifying customer copy the dashboard renders.
#   * ``structural`` == this will NOT self-heal by merely waiting/retrying (an operator/user/structural change
#     is needed) — the UI must therefore NEVER render "reconnecting…" for it (ADR-0100 invariant).
#   * ``monitored`` == this account currently produces dashboard data (counts toward coverage numerator). Only
#     READY is monitored; every other stage is surfaced with its reason but NOT counted as covered.
_STAGE_META: Dict[str, tuple] = {
    READY:                   ("Monitoring active",        False, True,  PIPE_DASHBOARD_INCLUDED),
    PROVISIONING:            ("Setting up monitoring",     False, False, PIPE_ENDPOINT_ALLOCATED),
    ENDPOINT_MISSING:        ("Setting up monitoring",     False, False, PIPE_DISCOVERED),
    LOGIN_REQUIRED:          ("Login required",            True,  False, PIPE_DISCOVERED),
    UNREACHABLE:             ("Temporarily unreachable",   False, False, PIPE_ROUTE_REGISTERED),
    IDENTITY_MISMATCH:       ("Identity mismatch",         True,  False, PIPE_REACHABLE),
    SNAPSHOT_STALE:          ("Snapshot stale",            False, False, PIPE_IDENTITY_VERIFIED),
    STRUCTURALLY_EXCLUDED:   ("Structurally excluded",     True,  False, PIPE_DISCOVERED),
    TEMPORARILY_UNAVAILABLE: ("Temporarily unavailable",   False, False, PIPE_DISCOVERED),
    UNKNOWN:                 ("Status unknown",            False, False, PIPE_DISCOVERED),
}

# A live open-positions read reason (ADR-0100 fetcher vocabulary) -> the enrolment stage it implies for an
# otherwise endpoint-READY account. ``account_not_demo`` is DELIBERATELY ABSENT: a LIVE terminal's positions
# are demo-gated by design (ADR-0072) but its balance/equity/deals still flow, so it does NOT demote the account
# below READY — the positions limitation is carried separately in ``detail['positions_unsupported']``. An
# unknown reason does not demote either (fail-safe: never over-claim a fault we cannot confirm).
_LIVE_REASON_STAGE = {
    "identity_refused": IDENTITY_MISMATCH,
    "endpoint_unavailable": UNREACHABLE,
    "config_missing": UNREACHABLE,
    "bridge_unreachable": UNREACHABLE,
}


@dataclass(frozen=True)
class EnrolmentStatus:
    """One account's monitoring-enrolment projection — the stable, secret-free annotation the dashboard and the
    reconciler both consume. Carries NO login, account number, host or port in the clear."""
    account_id: int
    stage: str
    pipeline_step: str
    ui_state: str
    reason_code: str
    structural: bool
    monitored: bool
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "stage": self.stage,
            "pipeline_step": self.pipeline_step,
            "ui_state": self.ui_state,
            "reason_code": self.reason_code,
            "structural": self.structural,
            "monitored": self.monitored,
            "detail": self.detail,
        }


@dataclass
class EnrolmentContext:
    """Pre-fetched, per-account inputs for the projection (built in bulk by ``evaluate_enrolment_for_accounts``
    so the dashboard hot path stays N+1-free). Every field is read-only durable state."""
    endpoint: object = None            # latest non-RETIRED HostedExecutionEndpoint, or None
    workspace: object = None           # HostedMt5Workspace, or None
    route: object = None               # non-TOMBSTONED MonitoringRoute, or None (only loaded when gateway on)
    snapshot_fresh: bool = False       # latest AccountEquitySnapshot observed within SNAPSHOT_FRESHNESS_S
    hosted: bool = False               # per-tenant (PERSISTENT_WORKSPACE) path vs legacy global-agent account


def _status(account, stage: str, *, reason_code: str, detail: Optional[dict] = None,
            pipeline: Optional[str] = None) -> EnrolmentStatus:
    ui_state, structural, monitored, pipe = _STAGE_META[stage]
    return EnrolmentStatus(
        account_id=getattr(account, "id", None),
        stage=stage, pipeline_step=(pipeline or pipe), ui_state=ui_state,
        reason_code=reason_code, structural=structural, monitored=monitored,
        detail=detail or {})


def _gateway_enabled() -> bool:
    try:
        from execution.monitoring_flags import monitoring_gateway_enabled
        return monitoring_gateway_enabled()
    except Exception:  # noqa: BLE001 — absence ⇒ dark
        return False


def _enrolment_reconcile_enabled() -> bool:
    try:
        from execution.monitoring_flags import monitoring_enrolment_reconcile_enabled
        return monitoring_enrolment_reconcile_enabled()
    except Exception:  # noqa: BLE001
        return False


# ── Layer 1 — the pure, read-only readiness projection ───────────────────────────────────────────────────────

def evaluate_account_enrolment(account, *, ctx: EnrolmentContext,
                               live_reason: Optional[str] = None) -> EnrolmentStatus:
    """Project ONE account's monitoring-enrolment stage from durable records (pure; no host contact, no write).

    ``ctx`` carries the pre-fetched endpoint/workspace/route/snapshot facts. ``live_reason`` is the OPTIONAL
    low-level reason from a just-completed live open-positions read (ADR-0100), which only REFINES an
    endpoint-READY account (identity-refused / unreachable); it never promotes an account and ``account_not_demo``
    never demotes one. Deterministic, most-specific-first, fail-safe (indeterminate ⇒ UNKNOWN, never a
    fabricated READY)."""
    # Tombstoned accounts are normally filtered out of every dashboard scope; guard defensively so a stray one
    # can never read as monitored.
    if getattr(account, "disconnected_at", None) is not None:
        return _status(account, STRUCTURALLY_EXCLUDED, reason_code=mm.RC_TOMBSTONED)

    # Prefer the durable ADR-0075 route readiness ONLY when the gateway subsystem is enabled AND the route
    # carries a DETERMINATE readiness verdict (the gateway's live probe has actually evaluated it). A route that
    # has merely been reserved/registered by the enrolment reconciler still carries the default
    # ``Readiness.UNKNOWN`` (reserve/register never call ``set_readiness``) — deferring to it would flip a
    # genuinely-READY account (fresh data flowing) to "Status unknown" during the reserve->verify window, which
    # is exactly the next deploy step. So an UNKNOWN-readiness route falls through to the active-record
    # projection below, preserving the truthful stage until the gateway verifies it. With the gateway dark
    # (production today) there are no routes and this is skipped entirely.
    if (ctx.route is not None and _gateway_enabled()
            and getattr(ctx.route, "readiness", None) != mm.MonitoringRoute.Readiness.UNKNOWN):
        return _from_route(account, ctx.route)

    if not ctx.hosted:
        # Legacy / Provider-A account (monitored via the shared global agent, not the per-tenant pipeline). The
        # per-tenant stages (ENDPOINT_MISSING / PROVISIONING) do not apply; the honest signal is whether a fresh
        # durable snapshot exists. Never STRUCTURALLY_EXCLUDED — a legacy account genuinely monitors.
        if ctx.snapshot_fresh:
            return _status(account, READY, reason_code=mm.RC_OK, detail={"path": "legacy"})
        return _status(account, SNAPSHOT_STALE, reason_code=mm.RC_STALE_HEALTH, detail={"path": "legacy"})

    ws = ctx.workspace
    ep = ctx.endpoint
    if ws is None:
        # Hosted account with no workspace row yet — still being discovered/provisioned.
        return _status(account, PROVISIONING, reason_code=RW_WORKSPACE_MISSING)

    # Not connected to the broker: a user LOGIN is required once the slot is past provisioning, otherwise the
    # workspace is still being set up. Distinguished by the canonical lifecycle state / presence of an endpoint,
    # so a login-required account is NEVER labelled a transient "reconnecting" (packet §6 test 11).
    if getattr(ws, "proj_connected", None) is not True:
        canon = str(getattr(ws, "canonical_state", "") or "")
        if canon == "WAITING_FOR_LOGIN" or ep is not None:
            return _status(account, LOGIN_REQUIRED, reason_code=RW_WORKSPACE_NOT_CONNECTED)
        return _status(account, PROVISIONING, reason_code=RW_WORKSPACE_NOT_CONNECTED)

    # Connected but the observed broker account does not match the bound identity — a structural mismatch that
    # must not be read as transient (fail-closed; never monitored under a wrong identity).
    if getattr(ws, "proj_account_match", None) is not True:
        return _status(account, IDENTITY_MISMATCH, reason_code=RW_ACTIVE_ACCOUNT_MISMATCH)

    # Connected + matched but the customer has not yet confirmed the discovered account is theirs (ADR-0034
    # human ACK). Surfaced as LOGIN_REQUIRED (an explicit user action is pending), never "reconnecting".
    if getattr(account, "workspace_confirmed_at", None) is None:
        return _status(account, LOGIN_REQUIRED, reason_code=RW_NOT_CONFIRMED)

    # Endpoint lifecycle — the record that actually gates all three ingest paths (deals / equity / positions).
    if ep is None:
        return _status(account, ENDPOINT_MISSING, reason_code=mm.RC_NO_ENDPOINT)
    try:
        from execution.models import HostedExecutionEndpoint
        ready_state = HostedExecutionEndpoint.State.READY
    except Exception:  # noqa: BLE001
        ready_state = "READY"
    if getattr(ep, "state", None) != ready_state:
        # Endpoint allocated but not yet health/identity-certified READY — provisioning is still in flight.
        return _status(account, PROVISIONING, reason_code=mm.RC_WORKSPACE_NOT_READY)

    # Endpoint READY. Refine with the just-observed live read reason, if the caller supplied one.
    refined = _LIVE_REASON_STAGE.get(live_reason or "")
    positions_unsupported = (live_reason == "account_not_demo")
    if refined is not None:
        detail = {"positions_unsupported": positions_unsupported} if positions_unsupported else {}
        rc = mm.RC_IDENTITY_MISMATCH if refined == IDENTITY_MISMATCH else mm.RC_UNREACHABLE
        return _status(account, refined, reason_code=rc, detail=detail)

    # The truthful "is data flowing" anchor: a fresh durable equity snapshot. READY ⇒ included in coverage.
    detail = {"positions_unsupported": True} if positions_unsupported else {}
    if ctx.snapshot_fresh:
        return _status(account, READY, reason_code=mm.RC_OK, detail=detail)
    # Endpoint READY but no fresh snapshot yet (just became ready, or the collector has not run) — transient.
    return _status(account, SNAPSHOT_STALE, reason_code=mm.RC_STALE_HEALTH, detail=detail)


def _from_route(account, route) -> EnrolmentStatus:
    """Map a durable ADR-0075 ``MonitoringRoute`` readiness + reason code to an enrolment stage (used only when
    the gateway subsystem is enabled and a route exists). Reuses the route's own ``RC_*`` reason verbatim."""
    R = mm.MonitoringRoute.Readiness
    rc = getattr(route, "last_reason_code", "") or mm.RC_UNKNOWN
    readiness = getattr(route, "readiness", R.UNKNOWN)
    if readiness == R.MONITORING_READY:
        return _status(account, READY, reason_code=mm.RC_OK)
    if readiness == R.MONITORING_PARTIAL:
        # A LIVE terminal whose positions are demo-gated is PARTIAL but still monitors equity/deals ⇒ READY with
        # a positions note; any other partial cause is a stale/degraded read.
        if rc == mm.RC_FEATURE_DISABLED:
            return _status(account, READY, reason_code=mm.RC_OK, detail={"positions_unsupported": True})
        return _status(account, SNAPSHOT_STALE, reason_code=rc)
    if readiness == R.UNSUPPORTED:
        return _status(account, STRUCTURALLY_EXCLUDED, reason_code=rc or mm.RC_UNSUPPORTED)
    if readiness == R.CONNECTED_NOT_READY:
        return _status(account, PROVISIONING, reason_code=rc or mm.RC_WORKSPACE_NOT_READY)
    if readiness == R.MONITORING_UNAVAILABLE:
        stage = {
            mm.RC_IDENTITY_MISMATCH: IDENTITY_MISMATCH,
            mm.RC_UNREACHABLE: UNREACHABLE,
            mm.RC_LISTENER_ABSENT: UNREACHABLE,
            mm.RC_TERMINAL_DISCONNECTED: UNREACHABLE,
            mm.RC_TERMINAL_INIT_FAILED: UNREACHABLE,
            mm.RC_NO_ENDPOINT: ENDPOINT_MISSING,
            mm.RC_WORKSPACE_NOT_READY: PROVISIONING,
            mm.RC_GATEWAY_UNHEALTHY: UNREACHABLE,
        }.get(rc, TEMPORARILY_UNAVAILABLE)
        return _status(account, stage, reason_code=rc)
    return _status(account, UNKNOWN, reason_code=rc or mm.RC_UNKNOWN)


def evaluate_enrolment_for_accounts(accounts) -> Dict[int, EnrolmentStatus]:
    """Bulk projection: ``{account_id: EnrolmentStatus}`` for ``accounts`` with a BOUNDED number of queries
    (no per-account N+1). Prefetches endpoints, workspaces, routes (only when the gateway is on) and the latest
    equity-snapshot freshness in one query each, then projects each account purely. Read-only."""
    accts = list(accounts)
    ids = [a.id for a in accts]
    ctx_by_id = _build_contexts(accts, ids)
    return {a.id: evaluate_account_enrolment(a, ctx=ctx_by_id[a.id]) for a in accts}


def _build_contexts(accts, ids) -> Dict[int, EnrolmentContext]:
    """Build an ``EnrolmentContext`` per account with a fixed, small number of queries. Fail-open: a prefetch
    error leaves the affected field at its safe default (so the projection degrades to a truthful UNKNOWN/
    PROVISIONING rather than raising on the dashboard path)."""
    from django.utils import timezone
    ctx_by_id: Dict[int, EnrolmentContext] = {a.id: EnrolmentContext() for a in accts}
    if not ids:
        return ctx_by_id

    # Hosted classification from the durable, flag-independent provisioning field (mirrors
    # ``snapshot_transport.resolve_account_snapshot_base``). A hosted account is on the per-tenant pipeline.
    for a in accts:
        ctx_by_id[a.id].hosted = (
            str(getattr(a, "readiness_provider", "") or "") == PERSISTENT_WORKSPACE)

    # Endpoints — one non-RETIRED row per account (the same precedence the read transport uses).
    try:
        from execution.models import HostedExecutionEndpoint
        for ep in (HostedExecutionEndpoint.objects
                   .filter(trading_account_id__in=ids)
                   .exclude(state=HostedExecutionEndpoint.State.RETIRED)
                   .order_by("trading_account_id", "-id")):
            c = ctx_by_id.get(ep.trading_account_id)
            if c is not None and c.endpoint is None:
                c.endpoint = ep
    except Exception:  # noqa: BLE001
        logger.warning("enrolment: endpoint prefetch failed", exc_info=True)

    # Workspaces — OneToOne on the account; carries the canonical projection used for the stage.
    try:
        from hosted_workspace.models import HostedMt5Workspace
        for ws in (HostedMt5Workspace.objects
                   .filter(trading_account_id__in=ids)
                   .only("trading_account_id", "proj_connected", "proj_account_match", "canonical_state")):
            c = ctx_by_id.get(ws.trading_account_id)
            if c is not None:
                c.workspace = ws
                # A workspace row means the account is on the hosted pipeline even if readiness_provider lags.
                c.hosted = True
    except Exception:  # noqa: BLE001
        logger.warning("enrolment: workspace prefetch failed", exc_info=True)

    # Routes — only when the gateway subsystem is on (dark today ⇒ skip the query entirely).
    if _gateway_enabled():
        try:
            for r in (mm.MonitoringRoute.objects
                      .filter(trading_account_id__in=ids)
                      .exclude(state=mm.MonitoringRoute.State.TOMBSTONED)
                      .select_related("execution_endpoint")):
                c = ctx_by_id.get(r.trading_account_id)
                if c is not None and c.route is None:
                    c.route = r
        except Exception:  # noqa: BLE001
            logger.warning("enrolment: route prefetch failed", exc_info=True)

    # Snapshot freshness — the latest EQUITY-BEARING snapshot per account, within SNAPSHOT_FRESHNESS_S (one
    # query, Postgres DISTINCT ON). This matches the equity-curve coverage anchor EXACTLY: ``portfolio_equity_curve``
    # / ``latest_valid`` only counts a snapshot that carries a non-null ``equity`` within the same window, so we
    # filter ``equity__isnull=False`` here too. Judging freshness on a balance-only / blank latest row would mark
    # an account monitored while the equity curve (and the balance fallback) treat it as stale — overstating the
    # coverage numerator. A balance-only account therefore reads SNAPSHOT_STALE until a fresh equity figure lands.
    try:
        from analytics.models import AccountEquitySnapshot
        now = timezone.now()
        rows = (AccountEquitySnapshot.objects
                .filter(trading_account_id__in=ids, equity__isnull=False)
                .order_by("trading_account_id", "-observed_at")
                .distinct("trading_account_id")
                .values_list("trading_account_id", "observed_at"))
        for aid, observed_at in rows:
            c = ctx_by_id.get(aid)
            if c is not None and observed_at is not None:
                age = (now - observed_at).total_seconds()
                c.snapshot_fresh = (0 <= age <= SNAPSHOT_FRESHNESS_S)
    except Exception:  # noqa: BLE001
        logger.warning("enrolment: snapshot prefetch failed", exc_info=True)

    return ctx_by_id


def coverage_rollup(statuses) -> dict:
    """Summarise a collection of ``EnrolmentStatus`` into truthful coverage counts for the dashboard: the total
    account universe, how many are monitored (covered), and the per-stage breakdown. Never hides an account —
    every non-monitored account is counted under its stage, so "partial" is always explained."""
    sts = list(statuses)
    by_stage: Dict[str, int] = {}
    monitored = 0
    for s in sts:
        by_stage[s.stage] = by_stage.get(s.stage, 0) + 1
        if s.monitored:
            monitored += 1
    return {
        "total": len(sts),
        "monitored": monitored,
        "not_monitored": len(sts) - monitored,
        "by_stage": by_stage,
    }


# ── Layer 2 — the idempotent reconciler (dry-run by default; apply is dark-gated) ────────────────────────────

def select_accounts(*, owner=None, account_id: Optional[int] = None):
    """The enrolment universe: the owner's (or all) connected, non-tombstoned accounts — the SAME canonical
    universe the dashboard uses (``filter(disconnected_at__isnull=True)``). Owner/account scoping is applied
    when given (a single account is for targeted diagnosis)."""
    from trading.models import TradingAccount
    qs = TradingAccount.objects.filter(disconnected_at__isnull=True)
    if owner is not None:
        qs = qs.filter(user=owner)
    if account_id is not None:
        qs = qs.filter(id=account_id)
    return list(qs.order_by("id"))


def reconcile_enrolment(*, accounts=None, owner=None, account_id: Optional[int] = None,
                        apply: bool = False, actor: str = "enrolment_reconcile",
                        emit_alerts: bool = True) -> dict:
    """Reconcile monitoring enrolment across the selected accounts.

    DRY-RUN (default): project every account's stage and return the report — no write, no host contact.

    APPLY (``apply=True``): additionally, FOR EACH eligible hosted account, idempotently drive
    ``reserve_route_for_workspace`` to reserve + register a deterministic ``MonitoringRoute`` from the account's
    existing endpoint. This is TWO-LEVEL DARK: it is a no-op unless BOTH ``monitoring_gateway_enabled()`` and
    ``monitoring_enrolment_reconcile_enabled()`` are on (and the underlying primitive is itself gated on the
    gateway flag). It reserves/registers only — it NEVER marks a route READY (that needs the gateway's live
    identity probe) and NEVER contacts a host. Returns created/registered/unchanged/blocked counts + the
    per-account stage report. Fail-closed per account (one bad account never aborts the sweep)."""
    accts = list(accounts) if accounts is not None else select_accounts(owner=owner, account_id=account_id)
    statuses = evaluate_enrolment_for_accounts(accts)
    status_by_id = statuses

    apply_enabled = bool(apply) and _gateway_enabled() and _enrolment_reconcile_enabled()
    report = {
        "considered": len(accts),
        "apply": bool(apply),
        "apply_effective": apply_enabled,
        "coverage": coverage_rollup(status_by_id.values()),
        "created": 0, "unchanged": 0, "blocked": 0, "skipped_not_hosted": 0,
        "alerts_opened": 0, "alerts_resolved": 0,
        "accounts": [status_by_id[a.id].as_dict() for a in accts],
    }
    if not apply:
        return report

    if apply_enabled:
        from execution import monitoring_onboarding as onboarding
        from execution import monitoring_allocation as alloc
        # Prefetch workspaces once for the reserve calls (reserve_route_for_workspace needs the workspace object).
        ws_by_id = _workspaces_by_account(accts)
        for a in accts:
            ws = ws_by_id.get(a.id)
            if ws is None:
                report["skipped_not_hosted"] += 1
                continue
            try:
                res = onboarding.reserve_route_for_workspace(ws, actor=actor)
                if res is None:
                    # Preconditions not met / subsystem returned no result (fail-closed) — never a crash.
                    report["blocked"] += 1
                elif getattr(res, "ok", False):
                    # ALLOC_OK == a NEW route reserved; ALLOC_REACTIVATED (or any other ok) == an existing active
                    # route returned with no new occupancy — idempotent, counted as unchanged.
                    if getattr(res, "reason", "") == alloc.ALLOC_OK:
                        report["created"] += 1
                    else:
                        report["unchanged"] += 1
                else:
                    report["blocked"] += 1
            except Exception:  # noqa: BLE001 — one account's failure never aborts the sweep
                logger.exception("enrolment: reserve failed for account %s", a.id)
                report["blocked"] += 1
    else:
        # --apply requested but the route-mutation subsystem is dark: report the stages + manage alerts (below),
        # but reserve NO route (documented two-level-dark no-op on the data mutation).
        report["apply_skipped"] = "subsystem_disabled"

    # Alert reconciliation runs in --apply mode INDEPENDENT of the route-mutation flags. Alerts are
    # observational (a reliability.AlertEvent, never a bridge/ACL/account/route mutation), so a recovered
    # account's alert is resolved and a genuinely-stuck account is surfaced even before — or after — the dark
    # route-writing flags are flipped. Still gated on emit_alerts and on --apply (dry-run/report writes nothing).
    if emit_alerts:
        opened, resolved = _reconcile_alerts(accts, status_by_id)
        report["alerts_opened"] = opened
        report["alerts_resolved"] = resolved
    return report


def _workspaces_by_account(accts) -> dict:
    out: dict = {}
    try:
        from hosted_workspace.models import HostedMt5Workspace
        ids = [a.id for a in accts]
        for ws in (HostedMt5Workspace.objects.filter(trading_account_id__in=ids)
                   .select_related("execution_node", "workspace_node")):
            out[ws.trading_account_id] = ws
    except Exception:  # noqa: BLE001
        logger.warning("enrolment: workspace prefetch (apply) failed", exc_info=True)
    return out


# Enrolment-stage alerting reuses the DIRECT reliability pattern (no reliability migration, no constants edit).
# A distinct namespaced component keeps enrolment alerts separable from the route-reconcile alerts.
_ALERT_COMPONENT = "MONITORING_ENROL"   # ≤32 chars; reliability tolerates an unknown component string
_STAGE_SEVERITY = {
    IDENTITY_MISMATCH: "CRITICAL",
    ENDPOINT_MISSING: "WARN",
    LOGIN_REQUIRED: "INFO",
    STRUCTURALLY_EXCLUDED: "WARN",
    UNREACHABLE: "WARN",
}


def _reconcile_alerts(accts, status_by_id) -> tuple:
    """Open a governed alert for each account STUCK in a non-ready, actionable stage, and resolve its alerts ONLY
    once the account is POSITIVELY monitored again (READY). Dedup per (account, stage). Never raises.

    Resolution is gated on ``st.monitored`` (a positive READY verdict), NOT merely on "this stage has no
    configured severity" — otherwise a transient/fail-open degradation (a prefetch error that drops a genuinely
    IDENTITY_MISMATCH account to UNKNOWN/PROVISIONING) would auto-resolve a real OPEN alert and flap. A
    non-actionable, non-ready stage (PROVISIONING / SNAPSHOT_STALE / TEMPORARILY_UNAVAILABLE / UNKNOWN /
    LOGIN_REQUIRED-INFO) therefore leaves existing alerts untouched; only a positive recovery clears them."""
    opened = resolved = 0
    try:
        from reliability.models import AlertEvent
        from django.utils import timezone
    except Exception:  # noqa: BLE001
        return 0, 0
    for a in accts:
        st = status_by_id[a.id]
        keep = f"monenrol:{st.stage}:{a.id}"
        try:
            if st.monitored:
                # POSITIVE recovery (READY) — resolve every open enrolment alert for this account.
                resolved += (AlertEvent.objects
                             .filter(component=_ALERT_COMPONENT, trading_account_id=a.id,
                                     status=AlertEvent.Status.OPEN, dedup_key__startswith="monenrol:")
                             .update(status=AlertEvent.Status.RESOLVED, resolved_at=timezone.now()))
                continue
            sev = _STAGE_SEVERITY.get(st.stage)
            if sev and sev != "INFO":
                if not AlertEvent.objects.filter(dedup_key=keep, status=AlertEvent.Status.OPEN).exists():
                    AlertEvent.objects.create(
                        severity=sev, component=_ALERT_COMPONENT, trading_account_id=a.id,
                        title=f"Monitoring enrolment {st.stage} (acct {a.id})"[:200], body="",
                        dedup_key=keep, status=AlertEvent.Status.OPEN,
                        detail={"stage": st.stage, "reason": st.reason_code})
                    opened += 1
                # Cause CHANGED to another ACTIONABLE stage — resolve the stale alert for the prior actionable
                # cause (never for a non-actionable/degraded stage, which cannot reach this branch).
                resolved += (AlertEvent.objects
                             .filter(component=_ALERT_COMPONENT, trading_account_id=a.id,
                                     status=AlertEvent.Status.OPEN, dedup_key__startswith="monenrol:")
                             .exclude(dedup_key=keep)
                             .update(status=AlertEvent.Status.RESOLVED, resolved_at=timezone.now()))
            # else: a non-actionable, non-ready stage — LEAVE existing alerts as they are (no false resolve).
        except Exception:  # noqa: BLE001
            logger.exception("enrolment: alert reconcile failed for account %s", a.id)
    return opened, resolved
