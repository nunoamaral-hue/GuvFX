"""trade_copier.models — durable master-observation store (PR A).

Three concerns, all READ-ONLY with respect to the broker (nothing here mutates a master account):

* ``CopierMaster``            — which TradingAccounts are designated masters to observe (the worklist).
* ``MasterObservationPoll``   — one atomic, identity-verified observation of a master's open positions.
* ``MasterPositionSnapshot``  — the per-position rows of a poll (the durable open-position store that
                                did not exist before — see the day-1 capability map).
* ``MasterEvent``             — the append-only event ledger derived by diffing consecutive polls.

Raw observation is IMMUTABLE (``.claude/rules/data.md``): poll rows and events are written once and
never edited in place; ``save()`` refuses an update. The follower side (configuration, replication
ledger, symbol maps) is PR B and is NOT in this module.

Everything is DARK: these models are inert foundation until ``trade_copier.flags.copier_observer_enabled``
is turned on AND a ``CopierMaster`` is marked ``observe_enabled``. No execution path reads them.
"""
from __future__ import annotations

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
