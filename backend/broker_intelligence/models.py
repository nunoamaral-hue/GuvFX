"""Broker Intelligence — identity foundation (WP1).

``BrokerEmailAlias``: ONE opaque, non-enumerable email alias per Model-A BrokerAccount lifecycle instance
(``<opaque>@accounts.guvfx.com``). The alias is the stable identity a broker's transactional mail (withdrawals,
etc.) is addressed to; a later, separately-deployed ingestion service resolves ``To:``-alias → BrokerAccount.

INVARIANTS (see docs/BROKER_EMAIL_IDENTITY_DESIGN.md):
  * **Never reused / never reassigned.** ``alias_local`` is globally unique across ALL rows (active + retired), so a
    retired token is permanently reserved. A re-added account is a NEW Model-A instance (new pk) → a NEW alias.
  * **Per instance.** OneToOne to ``trading.TradingAccount``; NULLABLE to admit Journey B (alias minted before the
    broker account exists), bound write-once later.
  * **Opaque.** The local-part is a server-generated CSPRNG token embedding NO account id / user / broker / PII.
  * **Lifecycle:** RESERVED(=PENDING) → ACTIVE (bound) → RETIRED (terminal; row + token retained forever).
  * Carries NO credential and NO forward FK to event/withdrawal tables (those FK TO the alias later). It is a
    public *identifier*, not a secret (never Fernet-encrypted), but PII-adjacent (owner-scoped; masked in logs).
"""
from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


def alias_domain() -> str:
    """The alias domain, configuration not a constant (data rule). Default ``accounts.guvfx.com``; a deploy may
    point at a different domain. Snapshotted into ``domain_at_creation`` at mint so a later domain change never
    orphans a brokerage registration."""
    return str(getattr(settings, "BROKER_EMAIL_ALIAS_DOMAIN", "") or "accounts.guvfx.com").strip()


class BrokerEmailAlias(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Reserved (minted, not yet bound)"
        ACTIVE = "ACTIVE", "Active (bound to a live account)"
        RETIRED = "RETIRED", "Retired (tombstoned/abandoned; never reused)"

    class Journey(models.TextChoices):
        CONNECT_EXISTING = "CONNECT_EXISTING", "Connect an existing broker account"
        OPEN_NEW = "OPEN_NEW", "Open a new broker account (alias before account)"

    # The opaque local-part — the global never-reuse anchor (unconditional unique; see Meta).
    alias_local = models.CharField(max_length=64, unique=True, editable=False, db_index=True)
    # Immutable snapshot of the domain at mint (so a later domain change never changes a registered address).
    domain_at_creation = models.CharField(max_length=255, editable=False)
    # The Model-A lifecycle instance. NULLABLE for Journey B (minted before the account exists); bound write-once.
    trading_account = models.OneToOneField(
        "trading.TradingAccount", on_delete=models.PROTECT, null=True, blank=True,
        related_name="broker_email_alias")
    # Owner — set at mint so a journey-B alias has an owner before any account exists.
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="broker_email_aliases")
    origin_journey = models.CharField(max_length=24, choices=Journey.choices, default=Journey.CONNECT_EXISTING)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    bound_at = models.DateTimeField(null=True, blank=True)
    retired_at = models.DateTimeField(null=True, blank=True)

    _IMMUTABLE = ("alias_local", "domain_at_creation")

    class Meta:
        constraints = [
            # UNCONDITIONAL (all-rows) uniqueness — a RETIRED token stays occupied forever ⇒ never recyclable.
            models.UniqueConstraint(fields=["alias_local"], name="uniq_broker_email_alias_local"),
        ]
        indexes = [models.Index(fields=["user", "status"])]

    def address(self) -> str:
        """The full routable address, assembled from the opaque local-part + the pinned domain. Pure; no I/O."""
        return f"{self.alias_local}@{self.domain_at_creation}"

    def masked(self) -> str:
        """A log/audit-safe rendering that never exposes the full opaque token (PII-adjacent linker)."""
        lp = self.alias_local or ""
        head = lp[:3] if len(lp) > 3 else "***"
        return f"{head}…@{self.domain_at_creation}"

    def save(self, *args, **kwargs):
        # Model-layer immutability (binds the DRF full-save path too): the opaque token + domain are write-once
        # always; the trading_account bind is write-once (the single NULL→account transition for Journey B is
        # allowed exactly once); status is monotonic and RETIRED is terminal.
        if self.pk is not None:
            prior = type(self).objects.filter(pk=self.pk).first()
            if prior is not None:
                for f in self._IMMUTABLE:
                    if getattr(prior, f) != getattr(self, f):
                        raise ValidationError(f"BrokerEmailAlias.{f} is immutable")
                if prior.trading_account_id is not None and prior.trading_account_id != self.trading_account_id:
                    raise ValidationError("BrokerEmailAlias.trading_account is write-once (cannot be re-pointed)")
                if prior.status == self.Status.RETIRED and self.status != self.Status.RETIRED:
                    raise ValidationError("BrokerEmailAlias.status RETIRED is terminal")
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"BrokerEmailAlias({self.masked()}, {self.status})"
