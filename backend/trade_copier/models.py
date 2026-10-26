"""trade_copier.models — durable master-observation store (PR A).

Three concerns, all READ-ONLY with respect to the broker (nothing here mutates a master account):

* ``CopierMaster``            — which TradingAccounts are designated masters to observe (the worklist).
* ``MasterObservationPoll``   — one atomic, identity-verified observation of a master's open positions.
* ``MasterPositionSnapshot``  — the per-position rows of a poll (the durable open-position store that
                                did not exist before — see the day-1 capability map).
* ``MasterEvent``             — the append-only event ledger derived by diffing consecutive polls.

Raw observation is IMMUTABLE (``.claude/rules/data.md``): poll rows and events are written once and
never edited in place; ``save()`` refuses an update.

PR B (below the PR-A observation models) adds the FOLLOWER side — follower configuration
(``CopierFollower`` + append-only ``CopierFollowerAudit``), the verified cross-broker symbol map
(``CopierSymbolMap``), and the replication INTENT ledger (``CopierReplication``). PR B records what
it WOULD copy (sized volume, mapped symbol, translated SL/TP) and creates ZERO ExecutionJobs — the
gated DEMO execution-job creation is a separate, tightly-scoped follow-up. Nothing here mutates a
broker or places an order.

Everything is DARK: these models are inert foundation until ``trade_copier.flags.copier_observer_enabled``
is turned on AND a ``CopierMaster`` is marked ``observe_enabled``. No execution path reads them.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone


class _ImmutableRawEvidence(models.Model):
    """Base for append-only raw-observation rows: insert-once, never update (data.md)."""

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):  # noqa: D401
        if self.pk is not None and not self._state.adding:
            raise ValueError(
                f"{type(self).__name__} is append-only raw evidence and must not be updated "
                f"(pk={self.pk}); write a new row instead."
            )
        return super().save(*args, **kwargs)


class CopierMaster(models.Model):
    """A TradingAccount designated as a copy-trading MASTER to be observed (read-only).

    This is the observer's worklist only. Follower relationships, sizing and replication policy are
    PR B. ``on_delete=CASCADE`` keeps the config disposable so an account purge is never blocked by
    copier data (the PURGE flows delete AccountProvisioning then User)."""

    trading_account = models.OneToOneField(
        "trading.TradingAccount", on_delete=models.CASCADE, related_name="copier_master"
    )
    observe_enabled = models.BooleanField(
        default=False,
        help_text="When True AND COPIER_OBSERVER_ENABLED is on, the observer polls this master.",
    )
    last_observed_at = models.DateTimeField(null=True, blank=True)
    last_poll = models.ForeignKey(
        "trade_copier.MasterObservationPoll", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
        help_text="The most recent successful, identity-verified poll (the diff baseline).",
    )
    # Bounded single-flight observation (ADR-0051 BLOCKER-2): a lease claimed BEFORE the remote read
    # so at most one observation per master is in flight; a second pass skips. The lease is
    # time-bounded (auto-expires) and ownership is re-checked under the persist lock, so a slow/hung
    # read whose lease expired and was taken over cannot commit a stale snapshot. This makes acquisition
    # order == persist order and removes any dependence on response-arrival wall-clock timestamps.
    observation_lease_token = models.CharField(max_length=36, blank=True, default="")
    observation_lease_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Copier master"
        verbose_name_plural = "Copier masters"

    def __str__(self) -> str:
        return f"CopierMaster(acct={self.trading_account_id}, enabled={self.observe_enabled})"


class MasterObservationPoll(_ImmutableRawEvidence):
    """One atomic, identity-verified observation of a master's OPEN positions.

    A poll is created ONLY for a read whose observed session identity matched the account (the
    snapshot-transport downstream firewall). A refused/failed read creates NO poll — the observer
    never invents an observation and never advances the diff baseline on a gap."""

    master = models.ForeignKey(CopierMaster, on_delete=models.CASCADE, related_name="polls")
    observed_at = models.DateTimeField(default=timezone.now, db_index=True)
    read_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When the bridge read completed — the monotonic ordering key used to discard a "
                  "stale (older) read that arrives after a newer one (ADR-0051 BLOCKER-2).")
    # Observed MT5 session identity (from account_info in the same session that produced positions).
    observed_login = models.CharField(max_length=64, blank=True)
    observed_server = models.CharField(max_length=128, blank=True)
    observed_trade_mode = models.IntegerField(
        null=True, blank=True, help_text="MT5 trade_mode: 0=DEMO, 1=CONTEST, 2=REAL."
    )
    position_count = models.PositiveIntegerField(default=0)
    transport_reason = models.CharField(
        max_length=64, blank=True, help_text="snapshot_transport resolution reason_code (audit)."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["master", "id"])]

    def __str__(self) -> str:
        return f"Poll(id={self.pk}, master={self.master_id}, n={self.position_count})"


class MasterPositionSnapshot(_ImmutableRawEvidence):
    """One open position as observed in a poll (the durable open-position store)."""

    poll = models.ForeignKey(MasterObservationPoll, on_delete=models.CASCADE, related_name="positions")
    position_ticket = models.BigIntegerField()
    symbol = models.CharField(max_length=64)
    side = models.CharField(max_length=8)  # BUY / SELL
    volume = models.DecimalField(max_digits=12, decimal_places=3)
    sl = models.DecimalField(max_digits=24, decimal_places=10, null=True, blank=True)
    tp = models.DecimalField(max_digits=24, decimal_places=10, null=True, blank=True)
    price_open = models.DecimalField(max_digits=24, decimal_places=10, null=True, blank=True)
    price_current = models.DecimalField(max_digits=24, decimal_places=10, null=True, blank=True)
    magic = models.BigIntegerField(null=True, blank=True)
    comment = models.CharField(max_length=128, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["poll", "position_ticket"], name="uq_copier_snapshot_poll_ticket"
            )
        ]
        indexes = [models.Index(fields=["poll", "position_ticket"])]

    def __str__(self) -> str:
        return f"Snap(poll={self.poll_id}, ticket={self.position_ticket}, vol={self.volume})"


class MasterEvent(_ImmutableRawEvidence):
    """An append-only master trade-operation event derived by diffing two consecutive polls.

    ``sequence`` is a per-master monotone counter (allocated under a row lock by the observer);
    ``(master, sequence)`` is unique so ordering is durable and auditable. ``(from_poll,
    position_ticket, event_type)`` is unique so re-running the diff cannot double-write an event
    (replay safety). ``reason``/``origin`` are left blank when unknown (deal.reason is not observed
    today) — never guessed."""

    class EventType(models.TextChoices):
        OPEN = "OPEN", "Open"
        FULL_CLOSE = "FULL_CLOSE", "Full close"
        PARTIAL_CLOSE = "PARTIAL_CLOSE", "Partial close"
        VOLUME_INCREASE = "VOLUME_INCREASE", "Volume increase"
        SL_SET = "SL_SET", "SL set"
        SL_CHANGE = "SL_CHANGE", "SL change"
        SL_REMOVE = "SL_REMOVE", "SL remove"
        TP_SET = "TP_SET", "TP set"
        TP_CHANGE = "TP_CHANGE", "TP change"
        TP_REMOVE = "TP_REMOVE", "TP remove"

    master = models.ForeignKey(CopierMaster, on_delete=models.CASCADE, related_name="events")
    sequence = models.PositiveBigIntegerField()
    from_poll = models.ForeignKey(
        MasterObservationPoll, on_delete=models.CASCADE, related_name="events"
    )
    event_type = models.CharField(max_length=24, choices=EventType.choices)
    position_ticket = models.BigIntegerField()
    symbol = models.CharField(max_length=64, blank=True)
    side = models.CharField(max_length=8, blank=True)
    volume_before = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    volume_after = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    volume_delta = models.DecimalField(max_digits=13, decimal_places=3, default=0)
    prev_sl = models.DecimalField(max_digits=24, decimal_places=10, null=True, blank=True)
    new_sl = models.DecimalField(max_digits=24, decimal_places=10, null=True, blank=True)
    prev_tp = models.DecimalField(max_digits=24, decimal_places=10, null=True, blank=True)
    new_tp = models.DecimalField(max_digits=24, decimal_places=10, null=True, blank=True)
    reason = models.CharField(max_length=32, blank=True, help_text="Broker close reason if known (else blank).")
    origin = models.CharField(max_length=16, blank=True, help_text="manual/ea/script/guvfx if derivable (else blank).")
    observed_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["master_id", "sequence"]
        constraints = [
            models.UniqueConstraint(fields=["master", "sequence"], name="uq_copier_event_master_seq"),
            models.UniqueConstraint(
                fields=["from_poll", "position_ticket", "event_type"],
                name="uq_copier_event_poll_ticket_type",
            ),
        ]
        indexes = [
            models.Index(fields=["master", "sequence"]),
            models.Index(fields=["master", "position_ticket"]),
        ]

    def __str__(self) -> str:
        return f"Event(master={self.master_id}, seq={self.sequence}, {self.event_type}, ticket={self.position_ticket})"


# ─────────────────────────────── PR B — FOLLOWER SIDE ───────────────────────────────
# Follower configuration + the replication INTENT ledger. PR B records what it WOULD copy and
# creates NO ExecutionJobs. All DARK; cross-user and self-follow are rejected at the service layer.


class SizingMode(models.TextChoices):
    FIXED_LOT = "FIXED_LOT", "Fixed lot"
    MULTIPLIER = "MULTIPLIER", "Master lot multiplier"
    BALANCE_PROPORTIONAL = "BALANCE_PROPORTIONAL", "Balance/equity proportional"
    RISK_BASED = "RISK_BASED", "Risk-based (SL distance)"


class SltpTranslation(models.TextChoices):
    SAME_PRICE = "SAME_PRICE", "Same absolute price"
    SAME_DISTANCE = "SAME_DISTANCE", "Equivalent distance from entry"
    NONE = "NONE", "Do not replicate SL/TP"


class DisablePolicy(models.TextChoices):
    A_KEEP_MANAGING = "A_KEEP_MANAGING", "Keep managing existing positions"
    B_DETACH = "B_DETACH", "Detach and manage manually"
    C_CLOSE = "C_CLOSE", "Close copied positions"


class FollowerSyncStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    SYNCED = "SYNCED", "Synced"
    REPLICATING = "REPLICATING", "Replicating"
    DEGRADED = "DEGRADED", "Degraded"
    OUT_OF_SYNC = "OUT_OF_SYNC", "Out of sync"
    PROTECTION_FAILED = "PROTECTION_FAILED", "Protection failed"
    RECONNECT_REQUIRED = "RECONNECT_REQUIRED", "Reconnect required"
    DISABLED_NEW_ENTRIES = "DISABLED_NEW_ENTRIES", "Disabled for new entries / managing existing"
    DETACHED_MANUAL = "DETACHED_MANUAL", "Detached — manual management"
    CLOSING = "CLOSING", "Closing"
    RESIDUAL_EXPOSURE = "RESIDUAL_EXPOSURE", "Residual exposure"


class CopierFollower(models.Model):
    """One follower account that copies a master, with its sizing/caps/policy configuration.

    Ownership invariant (enforced at the service layer, fail-closed): ``follower_account.user`` MUST
    equal ``master.trading_account.user`` and the follower MUST NOT be the master account itself. A
    follower cannot activate itself — ``enabled`` is set only through the audited service."""

    master = models.ForeignKey(CopierMaster, on_delete=models.CASCADE, related_name="followers")
    follower_account = models.ForeignKey(
        "trading.TradingAccount", on_delete=models.CASCADE, related_name="copier_follower_link"
    )
    enabled = models.BooleanField(default=False)

    # Sizing
    sizing_mode = models.CharField(max_length=24, choices=SizingMode.choices, default=SizingMode.MULTIPLIER)
    fixed_lot = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    multiplier = models.DecimalField(max_digits=10, decimal_places=4, default=1)
    risk_per_trade_pct = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)

    # Caps / guards
    max_volume = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    max_aggregate_exposure = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    max_concurrent_positions = models.PositiveIntegerField(null=True, blank=True)
    max_spread_points = models.PositiveIntegerField(null=True, blank=True)
    max_slippage_points = models.PositiveIntegerField(null=True, blank=True)

    # Translation / policy
    sltp_translation = models.CharField(max_length=16, choices=SltpTranslation.choices,
                                        default=SltpTranslation.SAME_PRICE)
    disable_policy = models.CharField(max_length=20, choices=DisablePolicy.choices,
                                      default=DisablePolicy.A_KEEP_MANAGING)

    # Pre-authorized protective auto-close of UNPROTECTED (no-SL) copied positions. Config only in PR B;
    # actuation is a separate LIVE-gated step. Account-specific, versioned, revocable (packet §11).
    unprotected_close_enabled = models.BooleanField(default=False)
    unprotected_close_authorization_version = models.PositiveIntegerField(default=0)
    unprotected_close_authorized_at = models.DateTimeField(null=True, blank=True)
    unprotected_close_authorized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    # Future execution routing (populated when the gated DEMO execution follow-up wires jobs).
    strategy_assignment = models.ForeignKey(
        "strategies.StrategyAssignment", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    sync_status = models.CharField(max_length=24, choices=FollowerSyncStatus.choices,
                                   default=FollowerSyncStatus.PENDING)
    last_outcome = models.CharField(max_length=128, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["master", "follower_account"],
                                    name="uq_copier_follower_master_account"),
        ]
        indexes = [models.Index(fields=["master", "enabled"])]

    def __str__(self) -> str:
        return f"Follower(master={self.master_id}, acct={self.follower_account_id}, enabled={self.enabled})"


class CopierFollowerAudit(_ImmutableRawEvidence):
    """Append-only audit of every follower enable/disable/configuration change (packet §6)."""

    follower = models.ForeignKey(CopierFollower, on_delete=models.CASCADE, related_name="audits")
    action = models.CharField(max_length=48)
    detail = models.JSONField(default=dict, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                              blank=True, related_name="+")
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["follower", "id"])]


class CopierSymbolMap(models.Model):
    """VERIFIED cross-broker symbol equivalence (packet §8 — never select a destination symbol by
    name resemblance). Same-broker needs no row (identity). A cross-broker replication with no
    verified row fails CLOSED. ``verified`` must be True before the map is honored."""

    master_broker = models.CharField(max_length=128)
    master_symbol = models.CharField(max_length=64)
    follower_broker = models.CharField(max_length=128)
    follower_symbol = models.CharField(max_length=64)
    verified = models.BooleanField(default=False)
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                    blank=True, related_name="+")
    verified_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=256, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["master_broker", "master_symbol", "follower_broker"],
                                    name="uq_copier_symbolmap_master_follower"),
        ]
        indexes = [models.Index(fields=["master_broker", "master_symbol"])]

    def __str__(self) -> str:
        return (f"SymbolMap({self.master_broker}:{self.master_symbol} -> "
                f"{self.follower_broker}:{self.follower_symbol}, verified={self.verified})")


class ReplicationStatus(models.TextChoices):
    SHADOW_RECORDED = "SHADOW_RECORDED", "Shadow intent recorded (no execution)"
    REJECTED = "REJECTED", "Rejected (eligibility/sizing/symbol)"
    SKIPPED = "SKIPPED", "Skipped (disabled / not applicable)"
    # B-exec producer states (ADR-0051 PR C/B-exec P1): a SHADOW JOB is NOT an execution request —
    # it is a suppressed, un-claimable job that sends no order. Only a real (armed DEMO) job becomes
    # EXECUTION_REQUESTED, and only broker-result evidence may ever become EXECUTION_VERIFIED.
    SHADOW_JOB_RECORDED = "SHADOW_JOB_RECORDED", "Shadow job recorded — no order sent"
    EXECUTION_REQUESTED = "EXECUTION_REQUESTED", "Execution requested (real DEMO job created)"
    EXECUTION_VERIFIED = "EXECUTION_VERIFIED", "Execution verified on follower (broker evidence)"
    EXECUTION_FAILED = "EXECUTION_FAILED", "Execution failed"


class CopierReplication(models.Model):
    """The replication ledger: ONE row per (follower, master_event). Idempotent — a master event is
    replicated to a given follower at most once. In PR B it records the computed INTENT (sized volume,
    mapped symbol, translated SL/TP) with ``status=SHADOW_RECORDED`` and NO ExecutionJob; the gated
    DEMO follow-up fills ``execution_job``/``follower_ticket``/result and advances the status."""

    master_event = models.ForeignKey(MasterEvent, on_delete=models.CASCADE, related_name="replications")
    follower = models.ForeignKey(CopierFollower, on_delete=models.CASCADE, related_name="replications")
    requested_op = models.CharField(max_length=24)
    status = models.CharField(max_length=24, choices=ReplicationStatus.choices)
    reason = models.CharField(max_length=64, blank=True)

    target_volume = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    sizing_detail = models.CharField(max_length=64, blank=True)
    follower_symbol = models.CharField(max_length=64, blank=True)
    translated_sl = models.DecimalField(max_digits=24, decimal_places=10, null=True, blank=True)
    translated_tp = models.DecimalField(max_digits=24, decimal_places=10, null=True, blank=True)
    # STRUCTURED B-exec admission signals (never parse the truncated sizing_detail text to authorize a
    # job — ADR-0051 R2). B-exec must still INDEPENDENTLY re-read fresh broker constraints/symbol/
    # readiness at order time; these flags let it fail closed early without string parsing.
    constraints_authoritative = models.BooleanField(
        default=False, help_text="True only when destination broker lot bounds came from synced metadata.")
    sltp_translation_ok = models.BooleanField(
        default=True, help_text="False when the SL/TP translation could not be completed (e.g. SAME_DISTANCE "
                                "without a follower entry) — B-exec must reject such an intent.")

    # Filled by the gated DEMO execution follow-up (null in PR B).
    execution_job = models.ForeignKey("execution.ExecutionJob", on_delete=models.SET_NULL,
                                      null=True, blank=True, related_name="+")
    follower_ticket = models.BigIntegerField(null=True, blank=True)
    broker_result = models.JSONField(null=True, blank=True)
    latency_ms = models.IntegerField(null=True, blank=True)
    reconciliation_status = models.CharField(max_length=24, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["master_event", "follower"],
                                    name="uq_copier_replication_event_follower"),
        ]
        indexes = [
            models.Index(fields=["follower", "status"]),
            models.Index(fields=["master_event"]),
        ]

    def __str__(self) -> str:
        return (f"Replication(event={self.master_event_id}, follower={self.follower_id}, "
                f"{self.status}, vol={self.target_volume})")


# ───────────────────── MULTI-BROKER INSTRUMENT MATRIX (additive discovery layer) ─────────────────────
# Canonical economic identity + a durable, directional master→follower mapping record. ADDITIVE: these
# are NEW tables; nothing above is altered. They are a DISCOVERY/audit layer and are NOT on the execution
# path — execution symbol translation stays with ``CopierSymbolMap`` via ``symbols.resolve_follower_symbol``.
# ``CopierSymbolMap`` remains the sole execution authority; ``InstrumentMapping`` only reflects it for audit.


class CanonicalInstrument(models.Model):
    """A canonical economic-instrument identity that relates broker-specific symbols WITHOUT replacing
    their native names (packet §2). Keyed on the price reference (asset class + underlying + quote
    currency + optional expiry) via the stable ``key`` slug; settlement currency, contract size and
    contract type are descriptive attributes recorded as *material differences* on a mapping, never
    folded into the identity. Persisted only by the refresh command; the read/API path compares keys
    directly, so a read never creates a row."""

    key = models.CharField(max_length=64, unique=True, help_text="Stable slug, e.g. METAL:XAU:USD.")
    asset_class = models.CharField(max_length=16)
    underlying = models.CharField(max_length=24)
    quote_currency = models.CharField(max_length=8)
    contract_type = models.CharField(max_length=16, default="UNKNOWN")
    expiry = models.DateField(null=True, blank=True)
    display_name = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Canonical instrument"
        indexes = [models.Index(fields=["asset_class", "underlying", "quote_currency"])]

    def __str__(self) -> str:
        return f"CanonicalInstrument({self.key})"


class MappingClassification(models.TextChoices):
    VERIFIED = "VERIFIED", "Verified (explicit symbol map)"
    UNVERIFIED = "UNVERIFIED", "Unverified (needs explicit verification)"
    AMBIGUOUS = "AMBIGUOUS", "Ambiguous (fail closed)"
    STALE = "STALE", "Stale catalogue"
    INELIGIBLE = "INELIGIBLE", "Ineligible (not tradable / expired)"
    ABSENT = "ABSENT", "Absent (no follower counterpart)"


class InstrumentMapping(models.Model):
    """One ORDERED directional candidate: ``master_account → follower_account`` for one master symbol.

    The uniqueness contract is the WHOLE directional key — ``(master_account, follower_account,
    master_symbol, follower_symbol)`` — so a reverse direction is a separate row and adding one direction
    can never overwrite another. ``verified_via_symbol_map`` REFLECTS a verified ``CopierSymbolMap`` at
    refresh time (audit snapshot only); it is never read by the execution path. ``on_delete=CASCADE`` on
    the accounts keeps an account purge unblocked (consistent with the other copier config models)."""

    master_account = models.ForeignKey("trading.TradingAccount", on_delete=models.CASCADE, related_name="+")
    master_broker = models.CharField(max_length=128)
    master_symbol = models.CharField(max_length=64)
    follower_account = models.ForeignKey("trading.TradingAccount", on_delete=models.CASCADE, related_name="+")
    follower_broker = models.CharField(max_length=128)
    follower_symbol = models.CharField(max_length=64, blank=True)  # blank for ABSENT (no counterpart)
    canonical_instrument = models.ForeignKey(
        CanonicalInstrument, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    classification = models.CharField(max_length=16, choices=MappingClassification.choices)
    reason = models.CharField(max_length=64, blank=True)
    confidence = models.CharField(max_length=8, blank=True)
    material_differences = models.JSONField(default=dict, blank=True)
    verified_via_symbol_map = models.BooleanField(default=False)
    observed_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["master_account", "follower_account", "master_symbol", "follower_symbol"],
                name="uq_instrument_mapping_direction",
            ),
        ]
        indexes = [
            models.Index(fields=["master_account", "follower_account"]),
            models.Index(fields=["canonical_instrument"]),
        ]

    def __str__(self) -> str:
        return (f"InstrumentMapping({self.master_account_id}:{self.master_symbol} -> "
                f"{self.follower_account_id}:{self.follower_symbol or '∅'}, {self.classification})")
