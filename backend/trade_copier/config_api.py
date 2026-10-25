"""trade_copier.config_api — founder-only SHADOW configuration API (owner-scoped, audited, idempotent).

A GET+POST config endpoint for the founder Copy Trading page. It lets the authorised owner designate exactly
ONE master and one-or-more followers from THEIR OWN live accounts, and persists the SHADOW configuration through
the existing audited ``trade_copier.services`` front door. It is deliberately incapable of enabling execution:

* gated by the founder cohort (``copier_ui_enabled_for``, default-DENY) AND ``IsAuthenticated``;
* every account is re-scoped to ``request.user`` (cross-tenant ids are rejected, never leaked);
* it writes ONLY ``CopierMaster`` / ``CopierFollower`` rows via ``services`` (append-only audit on every change);
* it never touches an execution flag, worker, kill switch, credential, bridge or port — ``enabled=True`` here means
  SHADOW participation only (real orders stay blocked by the copier execute flags + the LIVE/demo wall);
* it is transactional and idempotent — re-POSTing the same configuration makes no change and writes no audit row.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.db import transaction
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from trade_copier import services
from trade_copier.flags import copier_ui_enabled_for
from trade_copier.models import CopierFollower, CopierMaster, CopierSymbolMap, MasterEvent, SizingMode
from trade_copier.serializers import _account_environment, _broker_label
from trade_copier.symbols import broker_key
from trading.models import TradingAccount

_SHADOW_BANNER = "SHADOW — EXECUTION OFF"
_FRESH_SECONDS = 15 * 60   # an account/observation older than this is "stale", never shown as ready


def _masked_login(account) -> str:
    n = str(getattr(account, "account_number", "") or "")
    return ("••••" + n[-4:]) if len(n) >= 4 else ("••••" if n else "")


def _observed_currency(account_id: int) -> str:
    from analytics.models import AccountEquitySnapshot
    cur = (AccountEquitySnapshot.objects.filter(trading_account_id=account_id)
           .order_by("-observed_at").values_list("account_currency", flat=True).first())
    return (cur or "").strip().upper()


def _freshness(account_id: int):
    """(_state, age_seconds) from the latest equity snapshot. Conservative: no snapshot => 'no_data'."""
    from django.utils import timezone
    from analytics.models import AccountEquitySnapshot
    ts = (AccountEquitySnapshot.objects.filter(trading_account_id=account_id)
          .order_by("-observed_at").values_list("observed_at", flat=True).first())
    if ts is None:
        return "no_data", None
    age = (timezone.now() - ts).total_seconds()
    return ("monitored" if age <= _FRESH_SECONDS else "stale"), int(age)


def _eligible_accounts(user):
    """The caller's OWN live (non-demo), non-decommissioned accounts — the only accounts the page may offer."""
    qs = (TradingAccount.objects.filter(user=user, is_demo=False)
          .select_related("broker_server").order_by("id"))
    out = []
    for a in qs:
        state, age = _freshness(a.id)
        out.append({
            "account_id": a.id, "name": a.name or "", "broker": _broker_label(a),
            "environment": _account_environment(a), "masked_login": _masked_login(a),
            "currency": _observed_currency(a.id) or None,
            "monitoring_state": state, "data_age_seconds": age,
            "ready": state == "monitored",   # disconnected/stale is NEVER represented as ready
        })
    return out


def _verified_maps(master_account, follower_account):
    """Verified cross-broker symbol maps that apply to this master→follower broker pair (same-broker = identity)."""
    mb, fb = broker_key(master_account), broker_key(follower_account)
    if mb == fb:
        return [{"identity": True, "note": "same broker — identity mapping"}]
    rows = CopierSymbolMap.objects.filter(master_broker=mb, follower_broker=fb, verified=True)
    return [{"master_symbol": r.master_symbol, "follower_symbol": r.follower_symbol, "verified": True} for r in rows]


def _pair_view(master, follower):
    """Per-pair display: instrument-map readiness, observer/replication health, last event, honest unavailable."""
    maps = _verified_maps(master.trading_account, follower.follower_account)
    last_event = (MasterEvent.objects.filter(master=master).order_by("-sequence")
                  .values("event_type", "symbol", "observed_at").first())
    return {
        "follower_account_id": follower.follower_account_id,
        "enabled": follower.enabled,
        "sizing_mode": follower.sizing_mode,
        "fixed_lot": str(follower.fixed_lot) if follower.fixed_lot is not None else None,
        "max_volume": str(follower.max_volume) if follower.max_volume is not None else None,
        "max_concurrent_positions": follower.max_concurrent_positions,
        "instrument_map_ready": bool(maps),
        "instrument_maps": maps,
        "observer_last_observed_at": master.last_observed_at.isoformat() if master.last_observed_at else None,
        "last_observed_event": last_event or None,
        "supported_risk_controls": ["FIXED_LOT", "max_volume", "max_concurrent_positions"],
        # Honest unavailability (not invented): per-account margin/leverage are not exposed by the deployed snapshot.
        "unavailable_risk_metadata": ["margin_mode", "free_margin", "used_margin", "margin_level", "leverage"],
        "live_execution_blocked": _account_environment(follower.follower_account) == "live",
    }


def _current_config(user):
    """The caller's current SHADOW config: the (single) master they own + its followers, or None."""
    master = (CopierMaster.objects.select_related("trading_account")
              .filter(trading_account__user=user).order_by("-observe_enabled", "id").first())
    if master is None:
        return None
    followers = list(master.followers.select_related("follower_account").order_by("id"))
    return {
        "master_account_id": master.trading_account_id,
        "observe_enabled": master.observe_enabled,
        "followers": [_pair_view(master, f) for f in followers],
    }


class CopyTradingConfigView(APIView):
    """GET/POST the founder SHADOW configuration. Cohort-gated (default-DENY), owner-scoped, audited, idempotent."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not copier_ui_enabled_for(request.user):
            return Response({"enabled": False, "banner": _SHADOW_BANNER})
        return Response({
            "enabled": True, "banner": _SHADOW_BANNER, "execution": "off",
            "explain": ("GuvFX is observing and comparing potential replication. No follower broker order is placed. "
                        "Saving configuration does NOT enable execution."),
            "eligible_accounts": _eligible_accounts(request.user),
            "config": _current_config(request.user),
        })

    def _owned_live(self, request, account_id):
        """The caller's OWN live account by id, or None (never leak existence of a foreign/demo account)."""
        return (TradingAccount.objects.filter(id=account_id, user=request.user, is_demo=False).first())

    def _parse_sizing(self, raw: dict):
        """Validate/coerce the SHADOW sizing subset (FIXED_LOT only here). Fail-closed on bad values."""
        mode = str(raw.get("sizing_mode") or SizingMode.FIXED_LOT)
        if mode not in dict(SizingMode.choices):
            raise ValueError(f"unsupported sizing_mode: {mode}")
        cfg = {"sizing_mode": mode}
        def _dec(name, lo):
            v = raw.get(name)
            if v in (None, ""):
                return None
            try:
                d = Decimal(str(v))
            except (InvalidOperation, ValueError, TypeError):
                raise ValueError(f"{name} must be a number")
            if not d.is_finite():                       # reject NaN / Infinity (Decimal() accepts them)
                raise ValueError(f"{name} must be a finite number")
            if d <= lo:
                raise ValueError(f"{name} must be > {lo}")
            if d > Decimal("1000"):                     # sane upper bound (also prevents numeric(12,3) overflow)
                raise ValueError(f"{name} is unreasonably large (max 1000)")
            return d
        fl = _dec("fixed_lot", Decimal("0"))
        mv = _dec("max_volume", Decimal("0"))
        if mode == SizingMode.FIXED_LOT and fl is None:
            raise ValueError("fixed_lot is required for FIXED_LOT sizing")
        if fl is not None:
            cfg["fixed_lot"] = fl
        if mv is not None:
            if fl is not None and mv < fl:
                raise ValueError("max_volume must be >= fixed_lot")
            cfg["max_volume"] = mv
        mcp = raw.get("max_concurrent_positions")
        if mcp not in (None, ""):
            try:
                mcp = int(mcp)
            except (ValueError, TypeError):
                raise ValueError("max_concurrent_positions must be an integer")
            if mcp < 1:
                raise ValueError("max_concurrent_positions must be >= 1")
            if mcp > 1000:                              # sane upper bound (prevents int overflow)
                raise ValueError("max_concurrent_positions is unreasonably large (max 1000)")
            cfg["max_concurrent_positions"] = mcp
        return cfg

    def post(self, request):
        if not copier_ui_enabled_for(request.user):
            return Response({"detail": "not_available"}, status=403)
        body = request.data if isinstance(request.data, dict) else {}
        master_id = body.get("master_account_id")
        followers_in = body.get("followers")
        if not isinstance(master_id, int):
            return Response({"detail": "master_account_id (int) required"}, status=400)
        if not isinstance(followers_in, list) or not followers_in:
            return Response({"detail": "followers (non-empty list) required"}, status=400)

        master_acct = self._owned_live(request, master_id)
        if master_acct is None:
            return Response({"detail": "master account not found"}, status=404)

        # Validate every follower up front (fail-closed, no partial writes): owned + live + not the master + unique.
        seen = set()
        desired = {}
        for item in followers_in:
            if not isinstance(item, dict) or not isinstance(item.get("account_id"), int):
                return Response({"detail": "each follower needs an integer account_id"}, status=400)
            fid = item["account_id"]
            if fid == master_id:
                return Response({"detail": "a master account cannot also be a follower"}, status=400)
            if fid in seen:
                return Response({"detail": f"duplicate follower account_id {fid}"}, status=400)
            seen.add(fid)
            facct = self._owned_live(request, fid)
            if facct is None:
                return Response({"detail": "follower account not found"}, status=404)
            try:
                cfg = self._parse_sizing(item)
            except ValueError as e:
                return Response({"detail": str(e)}, status=400)
            desired[fid] = (facct, cfg)

        audit = {"created": [], "updated": [], "enabled": [], "disabled": [], "demoted_masters": []}
        with transaction.atomic():
            # Enforce exactly ONE active master per owner. Demote every OTHER CopierMaster this owner has (stop
            # observing + disable its enabled followers) so a master switch can never leave a stale/invisible
            # master or a circular SHADOW config (A↔B) behind. This also breaks the case where the new master was
            # itself an enabled follower elsewhere: that other master is demoted and its follower rows disabled.
            for om in (CopierMaster.objects.select_for_update()
                       .filter(trading_account__user=request.user)
                       .exclude(trading_account_id=master_acct.id)):
                for of in om.followers.select_for_update().filter(enabled=True):
                    services.set_follower_enabled(of, False, actor=request.user)
                    audit["disabled"].append(of.follower_account_id)
                if om.observe_enabled:
                    om.observe_enabled = False
                    om.save(update_fields=["observe_enabled", "updated_at"])
                    audit["demoted_masters"].append(om.trading_account_id)
            master, _created = CopierMaster.objects.select_for_update().get_or_create(
                trading_account=master_acct, defaults={"observe_enabled": True})
            if not master.observe_enabled:
                master.observe_enabled = True
                master.save(update_fields=["observe_enabled", "updated_at"])
            existing = {f.follower_account_id: f for f in master.followers.select_for_update()}
            # Reconcile to the desired set (declarative, idempotent).
            for fid, (facct, cfg) in desired.items():
                f = existing.get(fid)
                if f is None:
                    f = services.add_follower(master, facct, actor=request.user, **cfg)
                    audit["created"].append(fid)
                else:
                    before = {k: getattr(f, k) for k in cfg}
                    services.update_follower_config(f, actor=request.user, **cfg)
                    f.refresh_from_db()
                    if any(before.get(k) != getattr(f, k) for k in cfg):
                        audit["updated"].append(fid)
                if not f.enabled:                                   # SHADOW participation (NOT execution)
                    services.set_follower_enabled(f, True, actor=request.user)
                    audit["enabled"].append(fid)
            # Followers no longer selected -> disabled (kept for audit; never deleted).
            for fid, f in existing.items():
                if fid not in desired and f.enabled:
                    services.set_follower_enabled(f, False, actor=request.user)
                    audit["disabled"].append(fid)

        return Response({
            "ok": True, "banner": _SHADOW_BANNER, "execution": "off",
            "audit": audit, "config": _current_config(request.user),
        })
