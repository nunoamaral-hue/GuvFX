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
