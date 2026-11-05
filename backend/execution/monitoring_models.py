"""execution.monitoring_models — deterministic per-host monitoring-route ownership (ADR-0075).

GFX-MT5-BRIDGE-SCALE-001, Phase 3. The MINIMUM governed data model for scalable, collision-free,
restart-safe monitoring-route ownership behind a per-host authenticated gateway. Additive: three NEW
tables in the ``execution`` app; it touches no existing table and no existing row. DARK: nothing reads
these rows until ``execution.monitoring_flags.monitoring_gateway_enabled()`` is on.

Why these three:
  * ``PhysicalHostGateway`` — the physical Windows host identity (today only an implicit ``rdp_host``
    string) PLUS the ONE backend-reachable gateway endpoint on that host and its attested health. This is
    the model element that lets a whole host be reached on a single Tailnet port — the scale change.
  * ``MonitoringRoute`` — one immutable occupancy of a monitoring route for a trading account: the durable
    ownership record (host, local endpoint, generation, lifecycle state, readiness, verified identity,
    capability set, health). It carries the runtime-identity invariant from ``.claude/rules/architecture``:
    ``(host, local_port, generation)`` identifies one occupancy; generation increments by exactly one on
    release after TOMBSTONE and never decreases, repeats, or skips.
  * ``MonitoringRouteAudit`` — append-only provenance for every state/generation transition (evidence +
    monotonicity proof + operator forensics).

Nothing here contacts a host, places an order, or arms execution. The live order-time bridge gate remains
the sole order authority; these rows are OBSERVATION/ROUTING bookkeeping only.
"""
from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q


# ── stable, secret-free reason codes for the readiness/route contract (ADR-0075 Phase 5) ──
# These are the per-account causes that today collapse into one bare ``stale``/``None`` at the dashboard;
# the route carries the SPECIFIC one so the UI/API can distinguish unavailable from zero.
RC_OK = "monitoring_ok"
RC_NO_ENDPOINT = "no_endpoint_allocated"
RC_UNREACHABLE = "endpoint_unreachable"            # allocated but backend cannot reach the monitoring boundary
RC_LISTENER_ABSENT = "listener_absent"
RC_IDENTITY_MISMATCH = "bridge_identity_mismatch"
RC_TERMINAL_DISCONNECTED = "terminal_disconnected"
RC_TERMINAL_INIT_FAILED = "terminal_init_failed"   # bridge reachable but MT5 not initialised (e.g. mt5_init_failed)
RC_FEATURE_DISABLED = "feature_disabled"           # capability not advertised / positions gate refuses (account_not_demo)
RC_UNSUPPORTED = "unsupported_account"
RC_STALE_HEALTH = "stale_health"
RC_GATEWAY_UNHEALTHY = "gateway_unhealthy"
RC_DUPLICATE_ROUTE = "duplicate_route"
RC_TOMBSTONED = "route_tombstoned"
RC_WORKSPACE_NOT_READY = "workspace_not_monitoring_ready"
RC_SUBSYSTEM_DISABLED = "monitoring_subsystem_disabled"
RC_NO_POSITION = "no_open_position"                # genuinely flat — NOT an error, distinct from unavailable
RC_UNKNOWN = "unknown_fail_closed"


class PhysicalHostGateway(models.Model):
    """One authenticated monitoring gateway per PHYSICAL Windows host (keyed by ``host_key`` == the
    co-resident ``TerminalNode.rdp_host``). Fills the gap that physical-host identity was previously only
    an implicit string. The gateway is the SINGLE backend-reachable port for the whole host; behind it,
    per-account local bridges are addressed on the host's loopback (never the Tailnet), so adding an
    account needs no new Tailscale grant.

    DARK: provisioning/arming the real host-side gateway process is a SEPARATE deploy packet; this row is
    the backend's durable record of the gateway's identity, endpoint and attested health. Never routable
    until ``status == HEALTHY`` and an attestation has been recorded (fail-closed)."""

    class Status(models.TextChoices):
        REGISTERED = "REGISTERED", "Registered (not yet health-attested)"
        HEALTHY = "HEALTHY", "Healthy (attested, routable)"
        UNHEALTHY = "UNHEALTHY", "Unhealthy (fail-closed)"
        DRAINING = "DRAINING", "Draining"
        RETIRED = "RETIRED", "Retired"

    host_key = models.CharField(
        max_length=255, unique=True,
        help_text="Physical host identity — equals the co-resident TerminalNode.rdp_host.")
    display_name = models.CharField(max_length=128, blank=True, default="")

    # The ONE backend-reachable gateway endpoint on this physical host.
    gateway_host = models.CharField(max_length=255, blank=True, default="")
    gateway_port = models.PositiveIntegerField(null=True, blank=True)
    gateway_base_url = models.CharField(max_length=255, blank=True, default="")
    protocol_version = models.CharField(max_length=32, blank=True, default="")

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.REGISTERED, db_index=True)
    # Gateway self-attestation (server-recorded identity assertion; NEVER a secret value).
    attested_identity = models.CharField(max_length=128, blank=True, default="")
    last_attestation_at = models.DateTimeField(null=True, blank=True)
    last_health_ok = models.BooleanField(null=True, blank=True)
    last_health_at = models.DateTimeField(null=True, blank=True)
    last_reason = models.CharField(max_length=64, blank=True, default="")

    # Gateway-side route ceiling. The EFFECTIVE ceiling is min(max_routes or ∞, HostCapacity.max_total_occupants)
    # — i.e. this never OVERRIDES the certified physical-host capacity (ADR-0069); it only narrows it.
    max_routes = models.PositiveIntegerField(default=0, help_text="0 = unset (defer to HostCapacity).")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "execution"
        indexes = [models.Index(fields=["status"])]

    @property
    def is_routable(self) -> bool:
        """A gateway may be routed through only when attested HEALTHY (fail-closed)."""
        return self.status == self.Status.HEALTHY and self.last_health_ok is True

    def __str__(self):  # pragma: no cover - trivial
        return f"HostGateway({self.host_key} {self.status})"


class MonitoringRoute(models.Model):
    """One immutable occupancy of a monitoring route for a trading account (ADR-0075 Phase 3).

    Runtime-identity invariant (`.claude/rules/architecture`): the SLOT is ``(host_key, local_port)``;
    ``(host_key, local_port, allocation_generation)`` identifies one occupancy; ``allocation_generation``
    increments by exactly one on release after TOMBSTONE and must never remain unchanged after release,
    decrease, or skip. The route's logical identity is the account (one ACTIVE route per account).

    Lifecycle: RESERVED (capacity reserved, not routable) → REGISTERED (gateway/local endpoint bound) →
    VERIFIED (identity attested + reachable) → READY (all readiness gates pass). DRAINING and TOMBSTONED
    retire it (port reusable only after TOMBSTONE + a generation bump). QUARANTINED is a fail-closed
    integrity state requiring operator review (never silently repaired)."""

    class State(models.TextChoices):
        RESERVED = "RESERVED", "Reserved (capacity held, not routable)"
        REGISTERED = "REGISTERED", "Registered (endpoint/gateway bound)"
        VERIFIED = "VERIFIED", "Verified (identity attested + reachable)"
        READY = "READY", "Ready (monitoring gates pass)"
        DRAINING = "DRAINING", "Draining"
        TOMBSTONED = "TOMBSTONED", "Tombstoned (retired; reusable after generation bump)"
        QUARANTINED = "QUARANTINED", "Quarantined (integrity failure — operator review)"

    class Readiness(models.TextChoices):
        UNKNOWN = "UNKNOWN", "Unknown"
        CONNECTED_NOT_READY = "CONNECTED_NOT_READY", "Connected to broker, route not ready"
        MONITORING_READY = "MONITORING_READY", "Monitoring route ready"
        MONITORING_PARTIAL = "MONITORING_PARTIAL", "Monitoring partial"
        MONITORING_UNAVAILABLE = "MONITORING_UNAVAILABLE", "Monitoring unavailable"
        UNSUPPORTED = "UNSUPPORTED", "Unsupported"

    class Health(models.TextChoices):
        OK = "OK", "OK"
        STALE = "STALE", "Stale"
        DEGRADED = "DEGRADED", "Degraded"
        FAILED = "FAILED", "Failed"
        UNKNOWN = "UNKNOWN", "Unknown"

    trading_account = models.ForeignKey(
        "trading.TradingAccount", on_delete=models.CASCADE, related_name="monitoring_routes")
    gateway = models.ForeignKey(
        PhysicalHostGateway, on_delete=models.PROTECT, related_name="routes")
    # Soft link to the per-account physical local bridge endpoint (REUSE, never replace: the endpoint
    # lifecycle in ``endpoint_service`` stays authoritative for the physical port).
    execution_endpoint = models.OneToOneField(
        "execution.HostedExecutionEndpoint", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="monitoring_route")

    host_key = models.CharField(max_length=255, db_index=True,
                                help_text="Denormalised gateway.host_key for physical-host-wide uniqueness.")
    local_port = models.PositiveIntegerField(
        help_text="The per-account LOCAL bridge port behind the gateway (host-loopback, not Tailnet-exposed).")
    workspace_uuid = models.UUIDField(null=True, blank=True, default=None,
                                      help_text="Immutable occupancy binding (HostedMt5Workspace.workspace_uuid).")
    allocation_generation = models.PositiveIntegerField(
        default=1, help_text="Monotonic per-slot occupancy counter (+1 on release after TOMBSTONE).")

    state = models.CharField(max_length=12, choices=State.choices, default=State.RESERVED, db_index=True)
    readiness = models.CharField(max_length=24, choices=Readiness.choices, default=Readiness.UNKNOWN)
    last_reason_code = models.CharField(max_length=64, blank=True, default=RC_SUBSYSTEM_DISABLED)

    # Attested identity snapshot (server-derived; logins MUST be masked wherever logged).
    verified_login = models.CharField(max_length=64, blank=True, default="")
    verified_server = models.CharField(max_length=160, blank=True, default="")
    verified_trade_mode = models.IntegerField(null=True, blank=True)
    verified_is_demo = models.BooleanField(null=True, blank=True)

    capability_set = models.JSONField(default=list, blank=True,
                                      help_text="Advertised read-only capabilities, e.g. ['health','account','positions'].")

    health_state = models.CharField(max_length=10, choices=Health.choices, default=Health.UNKNOWN)
    last_failure_reason = models.CharField(max_length=64, blank=True, default="")

    reserved_at = models.DateTimeField(null=True, blank=True)
    registered_at = models.DateTimeField(null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    last_verified_at = models.DateTimeField(null=True, blank=True)
    last_health_at = models.DateTimeField(null=True, blank=True)
    tombstoned_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    _ACTIVE_STATES = (State.RESERVED, State.REGISTERED, State.VERIFIED, State.READY, State.DRAINING)

    class Meta:
        app_label = "execution"
        constraints = [
            # One ACTIVE monitoring route per account (a TOMBSTONED row frees the account for a new occupancy).
            models.UniqueConstraint(
                fields=["trading_account"], condition=~Q(state="TOMBSTONED"),
                name="uniq_active_route_per_account"),
            # No active endpoint collision on a physical host. A DRAINING route STILL OCCUPIES its physical
            # port (the bridge is being retired, not yet gone), so DRAINING participates in uniqueness —
            # only TOMBSTONED frees the port for a new occupancy, matching ``_free_local_port``.
            models.UniqueConstraint(
                fields=["host_key", "local_port"],
                condition=~Q(state="TOMBSTONED"),
                name="uniq_active_host_local_port"),
            # Occupancy identity (`.claude/rules/architecture`): the SLOT is (host_key, local_port) and a
            # generation is never reused for the same slot — so (host_key, local_port, generation) uniquely
            # identifies one occupancy across ALL occupants of the slot, not just one account's reuses.
            models.UniqueConstraint(
                fields=["host_key", "local_port", "allocation_generation"],
                name="uniq_slot_generation"),
        ]
        indexes = [
            models.Index(fields=["gateway", "state"]),
            models.Index(fields=["state", "readiness"]),
            models.Index(fields=["host_key", "state"]),
        ]

    @property
    def is_active(self) -> bool:
        return self.state in self._ACTIVE_STATES

    @property
    def is_routable(self) -> bool:
        """Only a READY route is routable for monitoring (fail-closed)."""
        return self.state == self.State.READY

    def __str__(self):  # pragma: no cover - trivial
        return f"MonitoringRoute(acct={self.trading_account_id} {self.host_key}:{self.local_port} "\
               f"gen={self.allocation_generation} {self.state}/{self.readiness})"


class MonitoringRouteAudit(models.Model):
    """Append-only provenance for every monitoring-route state/generation change (ADR-0075). One row per
    material transition — the evidence trail for generation monotonicity and operator forensics. Never
    updated or deleted."""

    route = models.ForeignKey(MonitoringRoute, on_delete=models.CASCADE, related_name="audits")
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.CharField(max_length=64, default="monitoring_gateway")
    event = models.CharField(max_length=48)
    prev_state = models.CharField(max_length=12, blank=True, default="")
    new_state = models.CharField(max_length=12, blank=True, default="")
    prev_generation = models.IntegerField(null=True, blank=True)
    new_generation = models.IntegerField(null=True, blank=True)
    reason = models.CharField(max_length=64, blank=True, default="")
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        app_label = "execution"
        ordering = ["-at"]
        indexes = [models.Index(fields=["route", "at"])]

    def __str__(self):  # pragma: no cover - trivial
        return f"RouteAudit(route={self.route_id} {self.event} {self.prev_state}->{self.new_state})"
