"""analytics models.

``AccountEquitySnapshot`` — a durable, append-only, broker-OBSERVED per-account equity/balance time-series. It is
the ONLY authoritative source for truthful account and portfolio equity curves (never Trade rows, never
interpolation). One row = one identity-verified, fresh broker observation. Financial values are Decimal (no float
persistence). Observation-only; nothing here places or modifies an order.
"""
from django.db import models


class AccountEquitySnapshot(models.Model):
    """One broker-observed equity/balance observation for one TradingAccount at one UTC instant.

    Provenance-first: a row is written ONLY from an authoritative, identity-verified, fresh broker read (see
    ``analytics.equity_snapshots.capture_account_snapshot``). ``observed_at`` is the UTC time GuvFX observed the
    broker state. Values are stored in the account's NATIVE currency (``account_currency``); USD normalisation for
    the portfolio curve happens at read time via ``portfolio_fx`` (honest PARTIAL when a non-USD historical rate is
    unavailable). Append-only: corrections are new rows, never in-place edits (data.md: raw evidence is immutable).
    """
    trading_account = models.ForeignKey(
        "trading.TradingAccount", on_delete=models.CASCADE, related_name="equity_snapshots", db_index=True)
    observed_at = models.DateTimeField(db_index=True, help_text="UTC instant GuvFX observed this broker state.")
    account_currency = models.CharField(max_length=8, default="USD")
    # Financial values — Decimal only. Null when the broker/bridge did not report the field.
    balance = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    equity = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    floating_pnl = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    margin = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    free_margin = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    margin_level = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    # Identity provenance — the observed MT5 session identity that produced this row (already firewall-verified
    # before persistence; retained for audit so a mis-routed read can never masquerade as this account later).
    observed_login = models.CharField(max_length=64, blank=True, default="")
    observed_server = models.CharField(max_length=160, blank=True, default="")
    source = models.CharField(max_length=40, default="account_read",
                              help_text="Provenance of the observation (e.g. account_read, observer).")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["trading_account_id", "observed_at"]
        indexes = [
            models.Index(fields=["trading_account", "observed_at"], name="aes_acct_observed_idx"),
            models.Index(fields=["observed_at"], name="aes_observed_idx"),
        ]
        constraints = [
            # Idempotency backstop: one row per (account, exact observed instant). The capture throttle spaces
            # observations ~5min apart; this prevents a retry/overlap from writing the identical instant twice.
            models.UniqueConstraint(fields=["trading_account", "observed_at"],
                                    name="uniq_account_equity_observed_at"),
        ]

    def __str__(self):
        return f"AccountEquitySnapshot(acct={self.trading_account_id}, at={self.observed_at}, eq={self.equity})"


# ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
# DailyFxObservation — append-only daily FX rate revisions (ADR-0058, GBP->USD portfolio conversion, DARK).
# ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
class DailyFxObservation(models.Model):
    """One IMMUTABLE revision of one source's official daily FX rate for (base, quote, effective_date).

    APPEND-ONLY: a source may REVISE a published rate; each revision is a NEW immutable row — never an in-place
    edit or delete (data.md: raw evidence is immutable; corrections are new records). ``rate`` is the EXACT
    published rate, quote units per 1 base (USD per GBP for the Fed H.10 ``DEXUSUK`` series), stored UNROUNDED at
    full precision for calculation and audit; displayed portfolio money is quantised separately with Decimal
    ROUND_HALF_EVEN at read time.

    Idempotency vs revision: ``observation_checksum`` is the sha256 of the canonical observation tuple
    (source|series|base|quote|effective_date|exact-rate-string). Re-ingesting the IDENTICAL published value is a
    no-op (the unique constraint rejects the duplicate); a genuinely REVISED value has a different checksum and is
    stored as a NEW revision (``revision_number`` increments per (source, base, quote, effective_date)).

    Selection (``analytics.fx_source``): the EFFECTIVE revision for a date is the latest ACCEPTED revision (max
    ``revision_number`` with ``acceptance_state == ACCEPTED``); every prior revision is retained. The selected
    revision id is surfaced in audit/provenance evidence.
    """
    SOURCE_FED_H10 = "FED_H10"
    SOURCE_BOE = "BOE"            # monitoring / discrepancy alerts ONLY — never auto-substituted into a Fed total
    SOURCE_ECB = "ECB"
    SOURCE_CHOICES = [(SOURCE_FED_H10, "Federal Reserve H.10"), (SOURCE_BOE, "Bank of England"),
                      (SOURCE_ECB, "European Central Bank")]

    STATE_ACCEPTED = "ACCEPTED"
    STATE_REJECTED = "REJECTED"  # retained for audit; never selected for conversion
    STATE_CHOICES = [(STATE_ACCEPTED, "Accepted"), (STATE_REJECTED, "Rejected")]

    base_currency = models.CharField(max_length=3)
    quote_currency = models.CharField(max_length=3)
    effective_date = models.DateField(help_text="Source publication date for this rate (the rate's 'as of' day).")
    rate = models.DecimalField(max_digits=20, decimal_places=10,
                               help_text="EXACT published rate: quote units per 1 base (USD per GBP). Unrounded.")
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES)
    source_series = models.CharField(max_length=32, help_text="e.g. DEXUSUK (Fed H.10).")
    source_revision_id = models.CharField(max_length=64, blank=True, default="",
                                          help_text="Source's own publication/revision id where available.")
    retrieved_at = models.DateTimeField(help_text="When GuvFX fetched this observation from the source.")
    observation_checksum = models.CharField(
        max_length=64, help_text="sha256 of the canonical observation tuple (dedup / revision key).")
    revision_number = models.PositiveIntegerField(
        default=1, help_text="1,2,3... per (source, base, quote, effective_date).")
    supersedes = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by",
        help_text="The revision this one corrects, where applicable (informational; every prior revision retained).")
    acceptance_state = models.CharField(max_length=16, choices=STATE_CHOICES, default=STATE_ACCEPTED)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["base_currency", "quote_currency", "effective_date", "revision_number"]
        indexes = [
            models.Index(fields=["base_currency", "quote_currency", "source", "effective_date"],
                         name="fx_obs_sel_idx"),
            models.Index(fields=["source", "effective_date"], name="fx_obs_src_date_idx"),
        ]
        constraints = [
            # Reject an IDENTICAL duplicate payload (idempotent re-ingest) while ALLOWING multiple immutable
            # revisions for the same source/date (a genuinely revised value carries a different checksum).
            models.UniqueConstraint(
                fields=["source", "base_currency", "quote_currency", "effective_date", "observation_checksum"],
                name="uniq_fx_obs_no_identical_duplicate"),
            # Exactly one row per revision number within a (source, base, quote, effective_date) group.
            models.UniqueConstraint(
                fields=["source", "base_currency", "quote_currency", "effective_date", "revision_number"],
                name="uniq_fx_obs_revision_number"),
        ]

    def __str__(self):
        return (f"DailyFxObservation({self.base_currency}->{self.quote_currency} {self.effective_date} "
                f"rev{self.revision_number} {self.source}:{self.source_series} {self.acceptance_state})")

    # Append-only guard (defence in depth). The AUTHORITATIVE control is the database runtime-role grant (the app
    # runtime role has no UPDATE/DELETE on this table — see ops/erasure runtime-role separation). The request path
    # never writes this model; the daily ingestion command is the only writer and only ever CREATES rows.
    _IMMUTABLE = ("base_currency", "quote_currency", "effective_date", "rate", "source", "source_series",
                  "observation_checksum", "revision_number")

    def save(self, *args, **kwargs):
        if self.pk is not None:
            orig = type(self).objects.filter(pk=self.pk).values(*self._IMMUTABLE).first()
            if orig is not None:
                for f in self._IMMUTABLE:
                    if getattr(self, f) != orig[f]:
                        raise ValueError(
                            "DailyFxObservation is append-only: insert a new revision, never edit the immutable "
                            f"field {f!r} in place.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("DailyFxObservation is append-only and immutable: deletion is not permitted.")
