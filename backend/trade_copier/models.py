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
    # Monotone high-water mark for the gated EVENT projection (ADR-0074): the highest
    # ``CopierIngestedRevision.id`` already folded into the authoritative ``MasterEvent`` ledger for this
    # master. A re-run reads only rows above it, so promotion is idempotent and never re-emits a folded change.
    last_projected_ingest_id = models.BigIntegerField(default=0)
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
    # Detection provenance (ADR-0074). ``POLL`` = derived by the authoritative observer poll diff (the historical
    # and still-default producer). ``EVENT`` = promoted from a signed fast-path emission by the gated event
    # projection, which shares THIS ledger, sequence space and poll baseline (no second event namespace).
    source = models.CharField(max_length=8, default="POLL", help_text="POLL (observer) or EVENT (fast-path projection).")
    # The true broker change time, when the detection source can provide it. The POLL path cannot (MT5's
    # positions feed exposes no per-change broker timestamp — see diff.py), so it stays NULL for poll events and
    # the lifecycle falls back to ``observed_at`` exactly as before. The EVENT path DOES carry it, so a stale
    # buffered emission's admissibility deadline is measured from the real change time → no late entry.
    broker_event_time = models.DateTimeField(null=True, blank=True)
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
    their native names (packet §2). Keyed on the PRICE REFERENCE only (asset class + underlying + quote
    currency + optional expiry) via the stable ``key`` slug. Settlement/P&L currency, margin currency,
    contract type and contract size are broker-instance dimensions recorded as *material differences* on a
    mapping — never folded into the identity (so XAUUSD and XAUUSD_SB share ``METAL:XAU:USD`` even though
    one settles USD and the other GBP). Persisted only by the refresh command; the read/API path compares
    keys directly, so a read never creates a row."""

    key = models.CharField(max_length=64, unique=True, help_text="Stable slug, e.g. METAL:XAU:USD.")
    asset_class = models.CharField(max_length=16)
    underlying = models.CharField(max_length=24)
    quote_currency = models.CharField(max_length=8)
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


class ReviewStatus(models.TextChoices):
    """Human DISCOVERY-APPROVAL layer, kept DELIBERATELY SEPARATE from the machine ``classification`` and
    from execution authority (``CopierSymbolMap`` / ``verified_via_symbol_map``).

    ``APPROVED`` records that a named operator reviewed a SAFE-EXACT discovery candidate and accepted it for
    the audit/discovery matrix. It does NOT change ``classification`` (which stays ``UNVERIFIED`` until an
    execution-authoritative ``CopierSymbolMap`` exists), does NOT create or mutate a ``CopierSymbolMap``, and
    never makes the row executable — the execution path does not read ``InstrumentMapping`` at all."""
    UNREVIEWED = "UNREVIEWED", "Unreviewed (no human disposition)"
    APPROVED = "APPROVED", "Approved for discovery/audit by a named reviewer"
    REJECTED = "REJECTED", "Rejected by a named reviewer"


class InstrumentMapping(models.Model):
    """One ORDERED directional candidate: ``master_account → follower_account`` for one master symbol.

    Mapping IDENTITY includes the canonical instrument. Two uniqueness constraints, together, give the
    agreed contract:

    * ``uq_instrument_mapping_identity`` — unique on the WHOLE directional key including canonical:
      ``(master_account, follower_account, canonical_instrument, master_symbol, follower_symbol)``. A
      reverse direction is a separate row; adding one direction can never overwrite another.
    * ``uq_instrument_mapping_native_pair`` — unique on the native pair ``(master_account,
      follower_account, master_symbol, follower_symbol)``. This guarantees a native pair cannot be
      ambiguously attached to MORE THAN ONE canonical identity (a stronger guard the nullable-canonical
      identity constraint alone cannot give, since SQL treats NULL canonicals as distinct).

    ``verified_via_symbol_map`` REFLECTS a verified ``CopierSymbolMap`` at refresh time (audit snapshot
    only); it is never read by the execution path. ``on_delete=CASCADE`` on the accounts keeps an account
    purge unblocked (consistent with the other copier config models)."""

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
    identity_confidence = models.CharField(max_length=8, blank=True)
    material_differences = models.JSONField(default=dict, blank=True)
    compatibility_warnings = models.JSONField(default=list, blank=True)
    verified_via_symbol_map = models.BooleanField(default=False)
    # --- Human discovery-approval layer (separate from machine ``classification`` and execution authority) ---
    # A named operator may APPROVE a SAFE-EXACT candidate for the discovery/audit matrix. This NEVER relabels
    # ``classification`` as VERIFIED, NEVER creates/mutates a CopierSymbolMap, and NEVER makes the row
    # executable (the execution path does not read InstrumentMapping). See ADR-0062.
    review_status = models.CharField(max_length=16, choices=ReviewStatus.choices,
                                      default=ReviewStatus.UNREVIEWED)
    reviewed_by = models.CharField(max_length=128, blank=True, default="",
                                   help_text="Durable operator identity who approved this discovery candidate.")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_evidence_ref = models.CharField(max_length=256, blank=True, default="",
                                           help_text="Bounded provenance reference for the review evidence (e.g. a ticket/Notion id). Never a secret.")
    review_note = models.CharField(max_length=512, blank=True, default="")
    observed_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["master_account", "follower_account", "canonical_instrument",
                        "master_symbol", "follower_symbol"],
                name="uq_instrument_mapping_identity",
            ),
            models.UniqueConstraint(
                fields=["master_account", "follower_account", "master_symbol", "follower_symbol"],
                name="uq_instrument_mapping_native_pair",
            ),
        ]
        indexes = [
            models.Index(fields=["master_account", "follower_account"]),
            models.Index(fields=["canonical_instrument"]),
        ]

    def __str__(self) -> str:
        return (f"InstrumentMapping({self.master_account_id}:{self.master_symbol} -> "
                f"{self.follower_account_id}:{self.follower_symbol or '∅'}, {self.classification})")


# ───────────────────── LIVE COPIER EXACT-PAIR AUTHORIZATION (ADR-0057) ─────────────────────
# The ONLY durable, human-created record that can let the DEMO-only copier producer admit a LIVE
# (non-demo) follower — and ONLY for the exact (user, master, follower, symbol) it names, within its
# window and bounded order allowance, with every identity pinned. It is DELIBERATELY separate from the
# strategy ``execution.LiveExecutionAuthorization`` so a broad strategy authorization can never
# accidentally authorize a copier order. It grants NO authority by itself: it is one required gate among
# the full fail-closed chain in ``trade_copier.live_execution.admit_open_live``, and the final money-path
# authorities (per-runtime ``MT5_ALLOW_LIVE`` + the order-time bridge identity pin) remain downstream.


class CopierLiveAuthorizationStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    CONSUMED = "CONSUMED", "Consumed (order allowance exhausted)"
    REVOKED = "REVOKED", "Revoked"


class CopierLiveAuthorization(models.Model):
    """A human-created authorization for LIVE copy execution of ONE exact pair (ADR-0057).

    Binds the tenant/user, the exact master and follower accounts WITH their immutable pinned broker
    identities (login/server/environment), the exact source→follower symbol, the sizing and concurrency
    caps, a bounded order allowance, a validity window, a future-event watermark, and the human approver +
    reason. Admission (``live_execution.admit_open_live``) consults it fail-closed and re-verifies every
    bound fact against the live account at order time; any drift, expiry, revocation, exhausted allowance
    or kill switch denies. Never auto-created, never LLM-created — only the staff-gated
    ``grant_copier_live_authorization`` management command writes one."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    master_account = models.ForeignKey("trading.TradingAccount", on_delete=models.CASCADE, related_name="+")
    follower_account = models.ForeignKey("trading.TradingAccount", on_delete=models.CASCADE, related_name="+")

    # Immutable identity snapshots taken at grant time (what the human reviewed). Drift ⇒ invalid.
    master_login = models.CharField(max_length=64)
    master_server = models.CharField(max_length=128)
    master_environment = models.CharField(max_length=8)   # DEMO | LIVE (policy string)
    follower_login = models.CharField(max_length=64)
    follower_server = models.CharField(max_length=128)
    follower_environment = models.CharField(max_length=8)

    source_symbol = models.CharField(max_length=64)
    follower_symbol = models.CharField(max_length=64)

    max_volume = models.DecimalField(max_digits=12, decimal_places=3)
    max_concurrent_positions = models.PositiveIntegerField(default=1)
    max_orders = models.PositiveIntegerField(default=1, help_text="Bounded order allowance (1 = one test order).")
    orders_consumed = models.PositiveIntegerField(default=0)

    # ─── ADR-0083: separately-auditable lifecycle authority (distinct from the one-shot ENTRY allowance) ───
    # The ENTRY authority is the bounded, consume-once ``max_orders`` above. These three are the SEPARATE,
    # explicit grants that let an already-bound follower position be managed; each DEFAULTS FALSE (a legacy /
    # ADR-0057 authorization confers NO lifecycle authority — fail-closed) and is recorded on the grant for
    # audit. They never confer entry authority and can never OPEN or INCREASE exposure. ``modify_authority``
    # only changes SL/TP; the two close authorities only reduce/close exposure.
    modify_authority = models.BooleanField(
        default=False, help_text="Permit LIVE SL/TP MODIFY of the bound follower position (no exposure change).")
    partial_close_authority = models.BooleanField(
        default=False, help_text="Permit LIVE PARTIAL CLOSE of the bound follower position (reduce only).")
    full_close_authority = models.BooleanField(
        default=False, help_text="Permit LIVE FULL CLOSE of the bound follower position (close only).")
    close_only_after_expiry = models.BooleanField(
        default=False,
        help_text="Policy: after the entry window expires, still propagate a SAFETY-REDUCING close/partial "
                  "close to the bound position (never a new entry, never a modify, never an increase).")

    valid_from = models.DateTimeField()
    expires_at = models.DateTimeField()
    # Future-event-only watermark (ADR-0057 §6): an OPEN qualifies ONLY if its MasterEvent.sequence is
    # strictly greater than watermark_event_sequence AND its observed_at is strictly after watermark_at.
    # Captured from the master's latest observed event at grant time, so no historical/open/previously
    # evaluated position can ever replay.
    watermark_at = models.DateTimeField()
    watermark_event_sequence = models.BigIntegerField(
        help_text="The master's max MasterEvent.sequence at grant time; only a strictly later OPEN qualifies.")

    status = models.CharField(max_length=12, choices=CopierLiveAuthorizationStatus.choices,
                              default=CopierLiveAuthorizationStatus.ACTIVE)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                    blank=True, related_name="+")
    reason = models.CharField(max_length=256)
    # Stable correlation id threaded through every lifecycle decision, command, job and audit row for this
    # authorization (ADR-0083). Plain opaque token, never a secret. Blank on legacy rows.
    correlation_id = models.CharField(max_length=64, blank=True, default="")
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                    blank=True, related_name="+")
    revoke_reason = models.CharField(max_length=256, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Copier LIVE authorization"
        constraints = [
            # At most ONE active authorization per exact pair (a second active grant for the same pair is a
            # governance error). Mirrors the one-APPROVED-per-key partial-unique idiom in the approvals app.
            models.UniqueConstraint(
                fields=["master_account", "follower_account"],
                condition=models.Q(status="ACTIVE"),
                name="uq_copier_live_authz_active_pair"),
        ]
        indexes = [
            models.Index(fields=["master_account", "follower_account", "status"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self) -> str:
        return (f"CopierLiveAuthorization(user={self.user_id}, {self.master_account_id}->"
                f"{self.follower_account_id}, {self.source_symbol}->{self.follower_symbol}, "
                f"{self.status}, {self.orders_consumed}/{self.max_orders})")


class CopierLiveKillSwitch(models.Model):
    """Durable one-action emergency disarm for ALL LIVE copier dispatch (ADR-0057 §12).

    When ANY row is ``engaged`` the LIVE admission denies every pair, independent of flags — so a disarm
    survives a flag flip and a process restart, and preserves its own audit (who/when/why). This is the
    second, DB-backed disarm path; flipping ``COPIER_LIVE_EXECUTE_ENABLED`` OFF is the first. Engaged by
    the ``copier_live_disarm`` command only."""

    engaged = models.BooleanField(default=True)
    engaged_at = models.DateTimeField(auto_now_add=True)
    engaged_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                   blank=True, related_name="+")
    reason = models.CharField(max_length=256, blank=True, default="")

    class Meta:
        verbose_name = "Copier LIVE kill switch"
        indexes = [models.Index(fields=["engaged"])]

    def __str__(self) -> str:
        return f"CopierLiveKillSwitch(engaged={self.engaged}, at={self.engaged_at})"


# ───────────────────── PER-MASTER-TICKET LIFECYCLE (ADR-0071 hybrid change propagation) ─────────────────────
# A durable, additive PROJECTION of the per-(follower, master, master-ticket) lifecycle, derived by
# ``trade_copier.lifecycle`` from the existing MasterEvent ledger + reconcile state. It is a DECISION/record
# layer only: it holds the explicit state machine, the immutable follower-ticket LINK, the entry-admissibility
# deadline, the ordering posture (pending op) and latency/failure signals. It creates NO ExecutionJob and
# contacts NO broker — the execute/arm gates remain the sole authority for any order. All DARK (projected only
# when ``COPIER_LIFECYCLE_ENABLED`` is on).


class LifecycleStateChoices(models.TextChoices):
    OBSERVED_OPEN = "OBSERVED_OPEN", "Observed open (admissible; no follower attempt)"
    ENTRY_PENDING = "ENTRY_PENDING", "Entry attempted — awaiting link"
    LINKED = "LINKED", "Linked to a follower ticket"
    MODIFY_PENDING = "MODIFY_PENDING", "SL/TP modify queued"
    CLOSE_PENDING = "CLOSE_PENDING", "Close queued (supersedes modify)"
    ENTRY_FAILED_FINAL = "ENTRY_FAILED_FINAL", "Entry failed — terminal, never re-entered"
    CLOSED = "CLOSED", "Closed (follower full close confirmed)"
    CONTAINED_FAILURE = "CONTAINED_FAILURE", "Contained failure — needs an operator"


class LifecyclePendingOp(models.TextChoices):
    NONE = "NONE", "None"
    MODIFY = "MODIFY", "Modify queued"
    CLOSE = "CLOSE", "Close queued"


class CopierPositionLifecycle(models.Model):
    """One lifecycle per ``(follower, master, master_position_ticket)`` — the projected state machine.

    ``follower_ticket`` is the IMMUTABLE master→follower link, set once on reaching LINKED and never rewritten.
    ``applied_sequence`` is the per-ticket monotone high-water mark that makes the projection idempotent and
    rejects stale/out-of-order master events. ``pending_op`` records the queued propagation (a CLOSE supersedes
    a MODIFY). Timestamps carry the latency signals; the poll path cannot provide a true broker-event time, so
    latency is detection→apply (see ``trade_copier.cadence.measure_latency``)."""

    master = models.ForeignKey(CopierMaster, on_delete=models.CASCADE, related_name="position_lifecycles")
    follower = models.ForeignKey(CopierFollower, on_delete=models.CASCADE, related_name="position_lifecycles")
    master_position_ticket = models.BigIntegerField()

    state = models.CharField(max_length=24, choices=LifecycleStateChoices.choices,
                             default=LifecycleStateChoices.OBSERVED_OPEN)
    pending_op = models.CharField(max_length=8, choices=LifecyclePendingOp.choices,
                                  default=LifecyclePendingOp.NONE)
    is_terminal = models.BooleanField(default=False)

    applied_sequence = models.BigIntegerField(
        default=0, help_text="Highest master-event sequence applied to this lifecycle (monotone HWM).")
    entry_reference_at = models.DateTimeField(
        null=True, blank=True, help_text="The OPEN's reference time — the admissibility clock origin.")
    entry_deadline_at = models.DateTimeField(
        null=True, blank=True, help_text="entry_reference_at + entry-admissibility window (reporting).")
    attempted = models.BooleanField(default=False, help_text="An armed follower entry was attempted.")
    follower_ticket = models.BigIntegerField(
        null=True, blank=True, help_text="Immutable follower broker ticket — set once on LINKED, never rewritten.")

    open_event = models.ForeignKey(MasterEvent, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    failure_reason = models.CharField(max_length=64, blank=True, default="")

    # Latency signals (timestamps; latency computed on read — cadence.measure_latency).
    open_observed_at = models.DateTimeField(null=True, blank=True)
    linked_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    last_transition_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Copier position lifecycle"
        constraints = [
            models.UniqueConstraint(fields=["follower", "master", "master_position_ticket"],
                                    name="uq_copier_lifecycle_follower_master_ticket"),
        ]
        indexes = [
            models.Index(fields=["master", "state"]),
            models.Index(fields=["follower", "state"]),
            models.Index(fields=["is_terminal"]),
        ]

    def __str__(self) -> str:
        return (f"Lifecycle(follower={self.follower_id}, ticket={self.master_position_ticket}, "
                f"{self.state}, pending={self.pending_op})")


class CopierLifecycleTransition(_ImmutableRawEvidence):
    """Append-only audit of every lifecycle state transition (the ordering/admissibility evidence).

    Insert-once (``_ImmutableRawEvidence``): a transition row is written when the projection applies a state
    change and is never edited. ``sequence`` is the master-event sequence that drove it (0 for a
    follower-side link update). ``detail`` is sanitised — never a secret, login or raw payload."""

    lifecycle = models.ForeignKey(CopierPositionLifecycle, on_delete=models.CASCADE, related_name="transitions")
    from_state = models.CharField(max_length=24, blank=True, default="")
    to_state = models.CharField(max_length=24)
    trigger = models.CharField(max_length=48, help_text="The transition reason code (e.g. 'linked', 'superseded_by_close').")
    sequence = models.BigIntegerField(default=0)
    detail = models.JSONField(default=dict, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Copier lifecycle transition"
        ordering = ["id"]
        indexes = [models.Index(fields=["lifecycle", "id"])]

    def __str__(self) -> str:
        return f"Transition(lifecycle={self.lifecycle_id}, {self.from_state}->{self.to_state}, {self.trigger})"


# ───────────────────── HYBRID COMPLETION (ADR-0074) — fast-path ingestion, dispatch, replay store ─────────────────────
# Additive, DARK. None of these models is on the execution path. ``CopierIngestedRevision`` is the durable,
# IMMUTABLE record of a signed fast-path emission (the fast half of the hybrid detector); ``CopierIngestNonce``
# is the single-use replay-burn store for the signed endpoint; ``CopierLifecycleDispatch`` is the SYNTHETIC,
# append-only record of a lifecycle→producer-boundary SHADOW dispatch decision — it creates NO ExecutionJob.


class CopierIngestedRevision(_ImmutableRawEvidence):
    """One accepted, normalised position-state emission from the SIGNED fast-path event-ingestion endpoint
    (ADR-0074). IMMUTABLE raw evidence (``.claude/rules/data.md``): written once on acceptance, never edited; a
    correction is a new revision, never an overwrite.

    Idempotency is the monotone per-(master, ticket) ``revision`` contract (``trade_copier.revisions``): the
    unique ``(master, ticket, revision)`` makes a re-sent emission a DB duplicate (the endpoint maps the
    IntegrityError to a 200 ``duplicate``). It persists OBSERVATIONS only — it is never read by an order path
    and creates no ExecutionJob and no authoritative ``MasterEvent``."""

    master = models.ForeignKey(CopierMaster, on_delete=models.CASCADE, related_name="ingested_revisions")
    account_id = models.BigIntegerField(help_text="The signed account scope (TradingAccount id) — audit only.")
    ticket = models.BigIntegerField()
    revision = models.BigIntegerField(help_text="Monotone per-(account, ticket) emission ordinal (starts at 1).")
    source = models.CharField(max_length=8, default="EVENT", help_text="Detection source (EVENT fast path).")
    event_id = models.CharField(max_length=64, blank=True, default="", help_text="Bridge-unique emission id (audit).")
    payload_digest = models.CharField(max_length=64, blank=True, default="", help_text="Digest of the signed body.")

    closed = models.BooleanField(default=False)
    symbol = models.CharField(max_length=64, blank=True)
    side = models.CharField(max_length=8, blank=True)
    volume = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    original_volume = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    sl = models.DecimalField(max_digits=24, decimal_places=10, null=True, blank=True)
    tp = models.DecimalField(max_digits=24, decimal_places=10, null=True, blank=True)
    price_open = models.DecimalField(max_digits=24, decimal_places=10, null=True, blank=True)
    open_time = models.DateTimeField(null=True, blank=True)
    close_reason = models.CharField(max_length=32, blank=True, default="")
    broker_login = models.CharField(max_length=64, blank=True, default="")
    broker_server = models.CharField(max_length=128, blank=True, default="")

    broker_event_time = models.DateTimeField(null=True, blank=True)
    observed_time = models.DateTimeField(help_text="When the bridge observed/emitted the revision.")
    correlation_id = models.CharField(max_length=40, blank=True, default="")
    ingested_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Copier ingested revision"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["master", "ticket", "revision"],
                                    name="uq_copier_ingested_master_ticket_rev"),
        ]
        indexes = [
            models.Index(fields=["master", "ticket", "revision"]),
            models.Index(fields=["master", "event_id"]),
        ]

    def __str__(self) -> str:
        return f"IngestedRevision(master={self.master_id}, ticket={self.ticket}, rev={self.revision})"


class CopierIngestNonce(models.Model):
    """Single-use replay-burn store for the signed event-ingestion endpoint (ADR-0074). A nonce is inserted
    ONLY after the signature verifies; the unique constraint makes a replayed request fail closed. Rows are
    disposable (expired nonces may be pruned) — they carry no secret."""

    nonce = models.CharField(max_length=80, unique=True)
    expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Copier ingest nonce"
        indexes = [models.Index(fields=["expires_at"])]

    def __str__(self) -> str:
        return f"IngestNonce(…{self.nonce[-8:]}, expires={self.expires_at:%Y-%m-%dT%H:%M:%SZ})"


class CopierLifecycleDispatch(_ImmutableRawEvidence):
    """A SYNTHETIC, append-only record of a lifecycle→producer-boundary SHADOW dispatch decision (ADR-0074).

    The dispatch pass re-checks the EXISTING producer-boundary admission (``execution_producer.admit_*`` — which
    create nothing) for an actionable lifecycle decision and records the verdict + the deterministic idempotency
    key the producer WOULD use. It NEVER calls ``produce_*`` and creates NO ``ExecutionJob``: ``would_execute``
    records what WOULD happen once the execute/arm gates are flipped, so the wiring is provably correct while
    execution stays OFF. ``(lifecycle, idempotency_key)`` is unique so the pass is idempotent (a re-run records
    nothing new)."""

    lifecycle = models.ForeignKey(CopierPositionLifecycle, on_delete=models.CASCADE, related_name="dispatches")
    replication = models.ForeignKey(CopierReplication, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="+")
    op = models.CharField(max_length=16, help_text="OPEN / MODIFY / CLOSE / PARTIAL_CLOSE.")
    idempotency_key = models.CharField(max_length=48, help_text="The producer's deterministic key (e.g. copier:123).")
    admit_ok = models.BooleanField(default=False, help_text="The producer-boundary admission verdict (read-only).")
    admit_reason = models.CharField(max_length=64, blank=True, default="")
    would_execute = models.BooleanField(default=False,
                                        help_text="True iff this intent WOULD create an order once armed (synthetic).")
    detail = models.JSONField(default=dict, blank=True, help_text="Sanitised — never a secret/login/raw payload.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Copier lifecycle dispatch"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["lifecycle", "idempotency_key"],
                                    name="uq_copier_dispatch_lifecycle_key"),
        ]
        indexes = [
            models.Index(fields=["lifecycle", "id"]),
            models.Index(fields=["op", "admit_ok"]),
        ]

    def __str__(self) -> str:
        return f"Dispatch(lifecycle={self.lifecycle_id}, op={self.op}, admit_ok={self.admit_ok}, would_exec={self.would_execute})"


class CopierAlertDelivery(models.Model):
    """Durable outbound-DELIVERY outbox for a governed copier operator alert (ADR-0074 Phase-9 completion).

    Mirrors the audited ``execution.notifications`` outbox/dispatcher/transport pattern, scoped to copier
    ``reliability.AlertEvent`` rows (there is no reusable governed AlertEvent delivery mechanism — the only
    inline push covers ComponentHealth-derived alerts, not these). The flow:

      * ``failure_alerts.raise_copier_alert`` CREATES the immutable ``AlertEvent`` (surfaced read-only) and,
        while ``copier_alerts_enabled`` is on, ALSO enqueues ONE row here — idempotent via the unique
        ``dedup_key`` (= the alert's ``dedup_key``), so a re-raise never double-enqueues.
      * A SEPARATE gated dispatcher (``copier_alert_dispatch_enabled``) atomically claims PENDING/FAILED rows,
        sends through an INJECTED transport that DEFAULTS to an inert dry-run/NULL transport with NO real
        destination, records a per-attempt ``CopierAlertDeliveryAttempt`` and advances the durable state +
        attempt counter (FAILED is retried until ``max_attempts`` → terminal DEAD).

    The row is mutable (state machine) so it is NOT ``_ImmutableRawEvidence``; the per-attempt audit IS
    immutable. The payload is sanitised (ids + reason codes only — never a secret, login or raw body) and
    nothing here ever recovers a trade. PENDING/FAILED/DEAD counts are surfaced read-only on the operations
    summary so a disabled or unsuccessful delivery is operator-visible."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing"
        SENT = "SENT", "Sent"
        FAILED = "FAILED", "Failed (will retry)"
        DEAD = "DEAD", "Dead (retries exhausted)"

    alert = models.ForeignKey("reliability.AlertEvent", on_delete=models.CASCADE, related_name="+",
                              null=True, blank=True)
    dedup_key = models.CharField(max_length=200, unique=True,
                                 help_text="Deterministic idempotency key (= the alert dedup_key).")
    category = models.CharField(max_length=32)
    severity = models.CharField(max_length=16, blank=True, default="")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=5)
    payload = models.JSONField(default=dict, blank=True, help_text="Sanitised delivery payload — never a secret.")
    last_error = models.CharField(max_length=128, blank=True, default="")
    claimed_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Copier alert delivery"
        ordering = ["id"]
        indexes = [models.Index(fields=["status", "id"])]

    def __str__(self) -> str:
        return f"AlertDelivery(id={self.pk}, {self.category}, {self.status}, attempts={self.attempts})"


class CopierAlertDeliveryAttempt(_ImmutableRawEvidence):
    """Append-only audit of ONE outbound delivery attempt for a ``CopierAlertDelivery`` (ADR-0074).

    Insert-once (``_ImmutableRawEvidence``): the dispatcher writes one row per attempt with the transport name,
    the attempt ordinal, whether it transmitted, and a sanitised detail code. Never edited; carries no secret."""

    delivery = models.ForeignKey(CopierAlertDelivery, on_delete=models.CASCADE, related_name="attempts_audit")
    attempt = models.PositiveIntegerField()
    transport = models.CharField(max_length=32, blank=True, default="")
    transmitted = models.BooleanField(default=False)
    detail = models.CharField(max_length=128, blank=True, default="")
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Copier alert delivery attempt"
        ordering = ["id"]
        indexes = [models.Index(fields=["delivery", "id"])]

    def __str__(self) -> str:
        return f"DeliveryAttempt(delivery={self.delivery_id}, n={self.attempt}, transmitted={self.transmitted})"


class CopierRealtimeDispatch(models.Model):
    """Per-event latency evidence for the ADR-0094 hybrid real-time dispatch path. One row per master event that
    the SYNCHRONOUS immediate-dispatch orchestrator handled (or attempted), capturing the honest end-to-end
    timestamp chain so a ``source → follower-submission`` latency can be measured and the ≤2s acceptance gate
    asserted WITHOUT attributing broker fill time to GuvFX.

    Privacy-safe: ids, reason codes, the master position ticket (an audit identifier, never a credential/login)
    and plain timestamps only — never a login, secret, credential or raw payload. Mutable (a later reconcile/ack
    fills ``broker_acknowledged_at`` / ``completed_at``);
    it holds NO authority and is never read by an order path — it is evidence only. Written only when
    ``copier_immediate_dispatch_enabled`` armed the synchronous path (so with the gate OFF this table is inert)."""

    class Source(models.TextChoices):
        EVENT_PUSH = "EVENT_PUSH", "Immediate signed push"
        FAST_POLL = "FAST_POLL", "Fast bounded poll (emitter fallback)"
        RECONCILE = "RECONCILE", "Slow observer reconciliation (recovery only)"

    class Outcome(models.TextChoices):
        OPEN_SUBMITTED = "OPEN_SUBMITTED", "Follower OPEN order submitted"
        MODIFY_SUBMITTED = "MODIFY_SUBMITTED", "Follower MODIFY submitted"
        CLOSE_SUBMITTED = "CLOSE_SUBMITTED", "Follower CLOSE submitted"
        NO_ACTION = "NO_ACTION", "No follower action (dedup / gated / no qualifying event)"
        CONTAINED = "CONTAINED", "Fail-closed containment (UNKNOWN / mismatch / unreadable)"

    master = models.ForeignKey(CopierMaster, on_delete=models.CASCADE, related_name="realtime_dispatches")
    master_event = models.ForeignKey(MasterEvent, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    replication = models.ForeignKey(CopierReplication, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="+")
    follower_account_id = models.BigIntegerField(null=True, blank=True)
    master_ticket = models.BigIntegerField(null=True, blank=True, help_text="Master position ticket (audit).")
    event_type = models.CharField(max_length=16, blank=True, default="", help_text="OPEN / MODIFY / FULL_CLOSE.")
    dispatch_source = models.CharField(max_length=12, choices=Source.choices, default=Source.EVENT_PUSH)
    outcome = models.CharField(max_length=16, choices=Outcome.choices, default=Outcome.NO_ACTION)
    reason = models.CharField(max_length=64, blank=True, default="", help_text="Sanitised reason code.")
    correlation_id = models.CharField(max_length=40, blank=True, default="")

    # The HONEST timestamp chain (any may be null when genuinely unknown — NEVER invented).
    event_detected_at = models.DateTimeField(null=True, blank=True, help_text="Master broker_event_time / open_time.")
    emitted_at = models.DateTimeField(null=True, blank=True, help_text="Emitter observed/emitted the revision.")
    backend_received_at = models.DateTimeField(null=True, blank=True, help_text="Ingestion persisted the revision.")
    accepted_at = models.DateTimeField(null=True, blank=True, help_text="Event accepted into the authoritative ledger.")
    dispatch_started_at = models.DateTimeField(null=True, blank=True)
    follower_submitted_at = models.DateTimeField(null=True, blank=True, help_text="Follower job created / command sent.")
    broker_acknowledged_at = models.DateTimeField(null=True, blank=True, help_text="Broker ack/fill — measured separately.")
    completed_at = models.DateTimeField(null=True, blank=True)

    # Derived, stored for queryability (ms). GuvFX-attributable latency excludes broker ack/fill.
    source_to_submission_ms = models.IntegerField(null=True, blank=True)
    detection_to_received_ms = models.IntegerField(null=True, blank=True)
    received_to_submission_ms = models.IntegerField(null=True, blank=True)
    is_late = models.BooleanField(default=False, help_text="source→submission exceeded the 2000ms acceptance ceiling.")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # The acceptance ceiling (ms). GuvFX source→follower-submission must sit at or under this under healthy
    # synthetic/test conditions; anything above is flagged ``is_late`` and must NOT be reported as meeting the gate.
    ACCEPTANCE_CEILING_MS = 2000
    TARGET_MS = 1000

    class Meta:
        verbose_name = "Copier real-time dispatch"
        ordering = ["id"]
        indexes = [
            models.Index(fields=["master", "id"]),
            models.Index(fields=["dispatch_source", "outcome"]),
            models.Index(fields=["is_late"]),
        ]

    @staticmethod
    def _ms(a, b):
        """Milliseconds from ``a`` to ``b`` (both aware datetimes), or None when either is missing — NEVER 0 for
        a missing endpoint (a fabricated zero would understate latency)."""
        if a is None or b is None:
            return None
        return int(round((b - a).total_seconds() * 1000))

    def recompute_latencies(self):
        """Fill the derived ms fields + ``is_late`` from the honest timestamp chain. The acceptance gate is
        measured from the TRUE broker detection time (``event_detected_at``) when known — NOT the emitter's
        self-reported ``emitted_at``, which a buffered or clock-skewed emitter could understate (RULE 11). A
        missing source/submission leaves the latency None (unknown), and a physically-impossible NEGATIVE
        (forward clock skew) is a data-quality fault — both FAIL the ≤ceiling gate (``is_late``), never a silent
        zero or a flattering 'on time'."""
        src = self.event_detected_at or self.emitted_at
        self.source_to_submission_ms = self._ms(src, self.follower_submitted_at)
        self.detection_to_received_ms = self._ms(src, self.backend_received_at)
        self.received_to_submission_ms = self._ms(self.backend_received_at, self.follower_submitted_at)
        s2s = self.source_to_submission_ms
        self.is_late = bool(s2s is None or s2s < 0 or s2s > self.ACCEPTANCE_CEILING_MS)
        return self

    def __str__(self) -> str:
        return (f"RealtimeDispatch(master={self.master_id}, {self.event_type}, {self.outcome}, "
                f"src2sub_ms={self.source_to_submission_ms}, late={self.is_late})")
