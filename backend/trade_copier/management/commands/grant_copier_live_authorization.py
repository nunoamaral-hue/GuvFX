"""grant_copier_live_authorization — the human-gated ceremony that creates ONE CopierLiveAuthorization
for an exact LIVE copier pair (ADR-0057).

This is the ONLY path that writes a CopierLiveAuthorization. It is never automatic and never LLM-driven: a
human operator runs it, ``--approved-by`` must be a staff user, and it validates every static precondition
fail-closed before creating an ACTIVE authorization. The dynamic order-time admission
(``trade_copier.live_execution.admit_open_live``) re-verifies everything again. Nothing here arms a flag,
contacts a broker, or places an order.

Identities are snapshotted at grant time (what the human reviewed); drift later invalidates the authorization.
No secret is read or printed; the login is masked in output.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Max
from django.utils import timezone

User = get_user_model()


def _mask(v) -> str:
    s = str(v or "")
    return ("…" + s[-4:]) if len(s) >= 4 else "****"


class Command(BaseCommand):
    help = "Create ONE human-approved CopierLiveAuthorization for an exact LIVE copier pair (ADR-0057)."

    def add_arguments(self, parser):
        parser.add_argument("--user-email", required=True, help="Owner of BOTH the master and follower accounts.")
        parser.add_argument("--master-account-id", required=True, type=int)
        parser.add_argument("--follower-account-id", required=True, type=int)
        parser.add_argument("--source-symbol", required=True)
        parser.add_argument("--follower-symbol", required=True)
        parser.add_argument("--max-volume", required=True, help="Maximum follower lot (e.g. 0.01).")
        parser.add_argument("--max-concurrent", type=int, default=1)
        parser.add_argument("--max-orders", type=int, default=1, help="Bounded order allowance (1 = one test order).")
        parser.add_argument("--valid-minutes", type=int, default=120,
                            help="Validity window length from now, in minutes (ignored if --expires-at given).")
        parser.add_argument("--expires-at", default="", help="ISO-8601 expiry (overrides --valid-minutes).")
        parser.add_argument("--approved-by", required=True, help="STAFF user email recording human approval.")
        parser.add_argument("--reason", required=True)

    def handle(self, *args, **o):
        from trade_copier.models import (
            CopierFollower, CopierLiveAuthorization, CopierLiveAuthorizationStatus, CopierMaster)
        from trading.account_policy import account_environment, is_live_environment
        from trading.models import TradingAccount
        from trade_copier.symbols import resolve_follower_symbol
        from execution.models import BrokerInstrument

        owner = User.objects.filter(email__iexact=o["user_email"]).first()
        if owner is None:
            raise CommandError(f"no user with email {o['user_email']}")
        approver = User.objects.filter(email__iexact=o["approved_by"]).first()
        if approver is None or not approver.is_staff:
            raise CommandError("--approved-by must be an existing STAFF user (human approval is mandatory)")

        macct = TradingAccount.objects.filter(id=o["master_account_id"]).select_related("broker_server").first()
        facct = TradingAccount.objects.filter(id=o["follower_account_id"]).select_related("broker_server").first()
        if macct is None or facct is None:
            raise CommandError("master and/or follower account not found")
        if not (macct.user_id == facct.user_id == owner.id):
            raise CommandError("both accounts must be owned by --user-email (tenant isolation)")
        if macct.id == facct.id:
            raise CommandError("master and follower must differ (self-follow)")

        # The follower MUST be an authoritatively LIVE, active account (this authorization only exists for LIVE).
        if not is_live_environment(facct):
            raise CommandError("follower account does not cleanly resolve to a LIVE environment (fail-closed)")
        if not facct.is_active:
            raise CommandError("follower account is not active (is_active=False)")
        # The master must also be authoritatively LIVE — a DEMO master driving a real-money follower order is
        # never authorized here (demo fills/prices must not drive a LIVE position).
        if not is_live_environment(macct):
            raise CommandError("master account does not cleanly resolve to a LIVE environment (fail-closed)")

        cm = CopierMaster.objects.filter(trading_account_id=macct.id).first()
        if cm is None:
            raise CommandError("master account has no CopierMaster (not an observed master)")
        follower_link = CopierFollower.objects.filter(
            master=cm, follower_account_id=facct.id).first()
        if follower_link is None:
            raise CommandError("no CopierFollower binds this master->follower pair")

        # Exact verified symbol map must resolve source -> follower symbol.
        sym = resolve_follower_symbol(macct, o["source_symbol"], facct)
        if not sym.ok or sym.follower_symbol != o["follower_symbol"]:
            raise CommandError(
                f"symbol map does not verify {o['source_symbol']} -> {o['follower_symbol']} "
                f"(resolved: ok={sym.ok}, follower_symbol={getattr(sym, 'follower_symbol', None)})")

        # Destination instrument must be enabled on the follower.
        if not BrokerInstrument.objects.filter(
                account_id=facct.id, broker_symbol=o["follower_symbol"], enabled=True).exists():
            raise CommandError(f"follower instrument {o['follower_symbol']} not enabled on account {facct.id}")

        # One ACTIVE authorization per pair.
        if CopierLiveAuthorization.objects.filter(
                master_account_id=macct.id, follower_account_id=facct.id,
                status=CopierLiveAuthorizationStatus.ACTIVE).exists():
            raise CommandError("an ACTIVE CopierLiveAuthorization already exists for this pair (revoke it first)")

        try:
            max_volume = Decimal(str(o["max_volume"])).quantize(Decimal("0.001"))
        except Exception:
            raise CommandError("--max-volume must be a decimal (e.g. 0.01)")
        if max_volume <= 0:
            raise CommandError("--max-volume must be positive at 0.001 lot precision (e.g. >= 0.001)")
        if int(o["max_orders"]) < 1 or int(o["max_concurrent"]) < 1:
            raise CommandError("--max-orders and --max-concurrent must each be >= 1")

        now = timezone.now()
        if o["expires_at"]:
            from django.utils.dateparse import parse_datetime
            expires_at = parse_datetime(o["expires_at"])
            if expires_at is None:
                raise CommandError("--expires-at is not a valid ISO-8601 datetime")
            if timezone.is_naive(expires_at):
                expires_at = timezone.make_aware(expires_at)
        else:
            expires_at = now + timedelta(minutes=int(o["valid_minutes"]))
        if expires_at <= now:
            raise CommandError("expiry must be in the future")

        # Future-event-only watermark: the master's latest observed event sequence NOW. Only a strictly later
        # OPEN qualifies at order time, so no historical/open/previously-evaluated position can replay.
        watermark_seq = (cm.events.aggregate(m=Max("sequence")).get("m") or 0)

        # Immutable identity snapshots (what the human reviewed).
        m_env = account_environment(macct)
        f_env = account_environment(facct)

        authz = CopierLiveAuthorization.objects.create(
            user=owner, master_account=macct, follower_account=facct,
            master_login=str(macct.account_number or "").strip(),
            master_server=str(getattr(macct.broker_server, "server_name", "") or "").strip(),
            master_environment=m_env,
            follower_login=str(facct.account_number or "").strip(),
            follower_server=str(getattr(facct.broker_server, "server_name", "") or "").strip(),
            follower_environment=f_env,
            source_symbol=o["source_symbol"], follower_symbol=o["follower_symbol"],
            max_volume=max_volume, max_concurrent_positions=int(o["max_concurrent"]),
            max_orders=int(o["max_orders"]), orders_consumed=0,
            valid_from=now, expires_at=expires_at,
            watermark_at=now, watermark_event_sequence=int(watermark_seq),
            status=CopierLiveAuthorizationStatus.ACTIVE, approved_by=approver, reason=o["reason"][:256])

        self.stdout.write(self.style.SUCCESS(
            f"CopierLiveAuthorization id={authz.id} ACTIVE: user={owner.id} "
            f"master={macct.id}({_mask(macct.account_number)}/{authz.master_server}/{m_env}) -> "
            f"follower={facct.id}({_mask(facct.account_number)}/{authz.follower_server}/{f_env}) "
            f"{authz.source_symbol}->{authz.follower_symbol} max_vol={max_volume} "
            f"max_concurrent={authz.max_concurrent_positions} max_orders={authz.max_orders} "
            f"window=[{now.isoformat()},{expires_at.isoformat()}) "
            f"watermark_seq={watermark_seq} approved_by={approver.email}"))
        self.stdout.write("NOTE: DARK — no flag armed, no order placed. Arming needs COPIER_ENABLED + "
                          "COPIER_EXECUTE_ENABLED + COPIER_LIVE_EXECUTE_ENABLED and HOSTED_LIVE_EXECUTION_ENABLED.")
