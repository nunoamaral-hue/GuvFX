"""capture_equity_snapshots — persist broker-observed equity snapshots into the durable ledger.

DARK unless ``EQUITY_SNAPSHOT_LEDGER_ENABLED`` (or --force). Reuses the EXISTING identity-firewalled account read
(``_fetch_mt5_account_balance`` — an attach to the account's already-running per-tenant terminal, NOT a new
terminal/login) rather than opening a fresh MT5 connection. Idempotent/throttled (~5min) so a cron cadence or an
overlapping run never double-writes. Observation-only; never places or modifies an order.

Intended cadence: a ~5-minute cron (see deploy/soak-report for the precedent). A future optimisation is to persist
directly from the persistent capability observer (run_hosted_observations) once it carries financial fields — that
would remove even the per-call attach; documented in KNOWN_ISSUES.
"""
import os

from django.conf import settings
from django.core.management.base import BaseCommand

_TRUTHY = ("1", "true", "yes", "on")


def _flag(name: str, default: str = "") -> bool:
    val = getattr(settings, name, None)
    if val is None:
        val = os.getenv(name, default)
    return str(val).strip().lower() in _TRUTHY


class Command(BaseCommand):
    help = ("Capture broker-observed equity snapshots into the durable ledger. "
            "DARK unless EQUITY_SNAPSHOT_LEDGER_ENABLED (or --force).")

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true",
                            help="Run even if EQUITY_SNAPSHOT_LEDGER_ENABLED is off (controlled validation).")
        parser.add_argument("--account-id", type=int, default=None, help="Limit to one TradingAccount id.")

    def handle(self, *args, **opts):
        if not (_flag("EQUITY_SNAPSHOT_LEDGER_ENABLED") or opts["force"]):
            self.stdout.write("equity snapshot ledger DARK (EQUITY_SNAPSHOT_LEDGER_ENABLED off); no-op")
            return

        from trading.models import TradingAccount
        from analytics.views_trade_history import _fetch_mt5_account_balance, _account_windows_username
        from analytics.equity_snapshots import capture_account_snapshot

        qs = TradingAccount.objects.filter(disconnected_at__isnull=True).select_related("broker_server")
        if opts["account_id"]:
            qs = qs.filter(id=opts["account_id"])

        created = throttled = refused = skipped = 0
        for acct in qs:
            try:
                snap = _fetch_mt5_account_balance(acct, _account_windows_username(acct))
            except Exception as exc:  # noqa: BLE001 — a broker read must never break the batch
                self.stderr.write(f"acct {acct.id}: read error {exc!r}")
                skipped += 1
                continue
            if not isinstance(snap, dict) or (snap.get("balance") is None and snap.get("equity") is None):
                skipped += 1                      # no fresh/usable observation -> do NOT manufacture a snapshot
                continue
            # Use the TRUE observed session identity the read surfaced (already firewall-verified). Do NOT fall back
            # to the account's expected identity — that would falsify the persisted audit provenance and make the
            # capture-layer re-verification a tautology. If the read carried no identity, leave it empty so
            # capture_account_snapshot's identity check fails closed (a row is never written for an unverifiable read).
            obs_login = snap.get("account_login") or snap.get("login") or ""
            obs_server = snap.get("account_server") or snap.get("server") or ""
            _row, reason = capture_account_snapshot(
                acct, balance=snap.get("balance"), equity=snap.get("equity"), currency=snap.get("currency"),
                observed_login=obs_login, observed_server=obs_server, source="account_read", verify_identity=True)
            created += (reason == "created")
            throttled += (reason == "throttled")
            refused += (reason == "identity_refused")
            skipped += reason in ("no_values", "duplicate")

        self.stdout.write(
            f"equity snapshots: created={created} throttled={throttled} identity_refused={refused} skipped={skipped}")
