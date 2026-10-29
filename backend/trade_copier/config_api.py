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
from trade_copier.flags import copier_ui_enabled_for, execution_banner, execution_status
from trade_copier.models import CopierFollower, CopierMaster, CopierSymbolMap, MasterEvent, SizingMode
from trade_copier.serializers import _account_environment, _broker_label
from trade_copier.symbols import broker_key
from trading.models import TradingAccount

# The execution status + banner on this surface are DERIVED LIVE from the execute flags
# (``trade_copier.flags.execution_status`` / ``execution_banner``) — NOT constants — so the config page can
# never keep asserting "EXECUTION OFF" once a Sponsor arms the DEMO producer. While the producer is off (the
# default) the helper reads exactly as before: mode 'observation', execution 'off', 'Observation mode' /
# 'Copy execution off', and its default note is "Saving configuration does not activate trading."
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


def current_live_accounts(user):
    """Authoritative 'current account' queryset for Copy Trading eligibility: the user's OWN, LIVE (non-demo),
    NON-decommissioned accounts — the SAME tombstone-excluding predicate the Broker Accounts list
    (``trading`` TradingAccount list action) and the dashboard portfolio (``analytics.portfolio.owned_accounts``)
    use: ``disconnected_at IS NULL``. A disconnected row is RETAINED for audit/history but must NEVER be offered
    as a current master or follower.

    is_active is DELIBERATELY NOT filtered: the platform's current-account surfaces (/accounts, dashboard) do not
    filter it — a *current* account that is merely stopped (``is_active=False``) still appears there and is a valid
    SHADOW master/follower (observation is read-only; the tombstone is ``disconnected_at``, not ``is_active``). In
    production every current support@ account (46/48/49) is ``is_active=False`` yet current; requiring
    ``is_active=True`` would wrongly empty the eligible set and drop the live master→follower pair."""
    return (TradingAccount.objects.filter(user=user, is_demo=False, disconnected_at__isnull=True)
            .select_related("broker_server").order_by("id"))


def _eligible_accounts(user):
    """The caller's OWN current (LIVE, non-decommissioned) accounts — the only accounts the page may offer."""
    qs = current_live_accounts(user)
    out = []
    for a in qs:
        state, age = _freshness(a.id)
        out.append({
            "account_id": a.id, "name": a.name or "", "broker": _broker_label(a),
            "environment": _account_environment(a), "masked_login": _masked_login(a),
            "currency": _observed_currency(a.id) or None,
            "monitoring_state": state, "data_age_seconds": age,
            # Freshness ONLY — never Copy-Trading readiness. The picker says whether live data is flowing;
            # a pair's copy-readiness (verified map + observed constraints + valid sizing) is a SEPARATE
            # per-follower verdict (see `_pair_view.readiness` / `config_status`). Terminal freshness alone
            # must never imply copy-readiness (readiness-semantics repair).
            "data_fresh": state == "monitored",
            "readiness_scope": "monitoring_freshness_only",
            "ready": state == "monitored",   # DEPRECATED freshness alias (not copy-readiness) — back-compat only
        })
    return out


def _verified_maps(master_account, follower_account):
    """Verified cross-broker symbol maps that apply to this master→follower broker pair (same-broker = identity)."""
    mb, fb = broker_key(master_account), broker_key(follower_account)
    if mb == fb:
        return [{"identity": True, "note": "same broker — identity mapping"}]
    rows = CopierSymbolMap.objects.filter(master_broker=mb, follower_broker=fb, verified=True)
    return [{"master_symbol": r.master_symbol, "follower_symbol": r.follower_symbol, "verified": True} for r in rows]


def _map_status(master_account, follower_account):
    """Structured instrument-map readiness for a master→follower broker pair. Pure evidence, nothing invented: the
    exact source (master) symbol, the follower symbol, the verification state, the last verification time, and an
    EXPLICIT failure reason when the pair is not map-ready. A same-broker pair is an identity mapping."""
    mb, fb = broker_key(master_account), broker_key(follower_account)
    if mb and fb and mb == fb:
        return {"master_broker": mb, "follower_broker": fb, "same_broker": True,
                "state": "identity", "ready": True, "failure_reason": None,
                "last_verified_at": None, "pairs": []}
    rows = list(CopierSymbolMap.objects.filter(master_broker=mb, follower_broker=fb).order_by("master_symbol"))
    pairs = [{"master_symbol": r.master_symbol, "follower_symbol": r.follower_symbol,
              "verified": bool(r.verified),
              "verified_at": r.verified_at.isoformat() if r.verified_at else None} for r in rows]
    verified = [p for p in pairs if p["verified"]]
    if not rows:
        state, ready, reason = "missing", False, "no_symbol_map"
    elif not verified:
        state, ready, reason = "unverified", False, "map_present_but_unverified"
    else:
        state, ready, reason = "verified", True, None
    last_verified = max((r.verified_at for r in rows if r.verified and r.verified_at), default=None)
    return {"master_broker": mb, "follower_broker": fb, "same_broker": False,
            "state": state, "ready": ready, "failure_reason": reason,
            "last_verified_at": last_verified.isoformat() if last_verified else None, "pairs": pairs}


def _spec_dec(v):
    if v in (None, ""):
        return None
    try:
        d = Decimal(str(v))
        return d if d.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _observed_constraints(follower_account, map_status):
    """The follower's BROKER-OBSERVED volume constraints (from synced ``BrokerInstrument`` rows) for the symbols it
    would actually trade — the VERIFIED follower symbols of the map. These are *observed* limits, kept deliberately
    distinct from the operator's *configured* sizing so the UI never conflates the two. Unavailability is reported
    honestly, with a reason — never defaulted to a hard-coded broker value."""
    from execution.models import BrokerInstrument
    out = {"available": False, "reason": None, "symbols": {}}
    if map_status.get("same_broker"):
        # Identity mapping: the follower's traded symbol set is the master's live set, which this read does not
        # enumerate. Report it as not-independently-verified rather than invent a constraint.
        out["reason"] = "identity_mapping_symbol_set_not_enumerated"
        return out
    follower_symbols = [p["follower_symbol"] for p in map_status.get("pairs", []) if p["verified"]]
    if not follower_symbols:
        out["reason"] = "no_verified_map"
        return out
    rows = {bi.broker_symbol: (bi.metadata or {}) for bi in
            BrokerInstrument.objects.filter(account=follower_account, broker_symbol__in=follower_symbols)}
    for sym in follower_symbols:
        m = rows.get(sym)
        if not m:
            continue
        out["symbols"][sym] = {k: m.get(k) for k in (
            "volume_min", "volume_step", "volume_max", "contract_size",
            "currency_base", "currency_profit", "trade_mode")}
    if out["symbols"]:
        out["available"] = True
    else:
        out["reason"] = "follower_instruments_unsynced"
    return out


def _validate_sizing_against_constraints(cfg, constraints):
    """Reject a configured lot the follower's broker cannot execute: below ``volume_min``, above ``volume_max``, or
    off the ``volume_step`` grid — for ANY observed symbol. ``max_volume`` is a ceiling, so it is only checked
    against ``volume_max``. A no-op when constraints are unavailable: that case is a fail-closed *readiness* state
    (the follower is shown not execution-ready), never a silent pass and never a hard save error."""
    if not constraints.get("available"):
        return
    fl = cfg.get("fixed_lot")
    mv = cfg.get("max_volume")
    for sym, spec in constraints["symbols"].items():
        vmin, vstep, vmax = _spec_dec(spec.get("volume_min")), _spec_dec(spec.get("volume_step")), _spec_dec(spec.get("volume_max"))
        if fl is not None:
            if vmin is not None and fl < vmin:
                raise ValueError(f"fixed_lot {fl} is below {sym} minimum volume {vmin}")
            if vmax is not None and fl > vmax:
                raise ValueError(f"fixed_lot {fl} exceeds {sym} maximum volume {vmax}")
            if vstep is not None and vstep > 0 and vmin is not None:
                steps = (fl - vmin) / vstep
                if steps != steps.to_integral_value():
                    raise ValueError(f"fixed_lot {fl} is off the {sym} volume step {vstep} (min {vmin})")
        if mv is not None and vmax is not None and mv > vmax:
            raise ValueError(f"max_volume {mv} exceeds {sym} maximum volume {vmax}")


def follower_config_readiness(master, follower):
    """Fail-closed per-follower CONFIGURATION readiness (NOT execution authorisation): verified instrument map +
    the follower's OWN broker-observed volume constraints + the configured lot validated against that contract.
    Returns the status, an explicit blocked reason — only ever the NEGATIVE ("not execution-ready — <reason>"),
    never a positive "execution-ready" claim — and whether the follower is still current. SHARED by the founder
    config page (``_pair_view``) and the read-only monitoring overview so both surfaces tell the SAME truth about
    a follower (a "ready" config is still observation-only until a separate explicit production authorisation)."""
    facct = follower.follower_account
    map_status = _map_status(master.trading_account, facct)
    constraints = _observed_constraints(facct, map_status)
    follower_current = facct.disconnected_at is None
    blocked_reason = None
    if not follower_current:
        blocked_reason = "follower_disconnected"
    elif not map_status["ready"]:
        blocked_reason = map_status["failure_reason"] or "instrument_map_not_ready"
    elif not constraints["available"]:
        blocked_reason = constraints.get("reason") or "symbol_constraints_unavailable"
    else:
        try:
            _validate_sizing_against_constraints(
                {"fixed_lot": follower.fixed_lot, "max_volume": follower.max_volume}, constraints)
        except ValueError as e:
            blocked_reason = f"sizing_invalid: {e}"
    return {"config_status": "ready" if blocked_reason is None else "blocked",
            "config_blocked_reason": blocked_reason, "follower_current": follower_current,
            "map_status": map_status, "constraints": constraints}


def _pair_view(master, follower):
    """Per-pair display: per-follower sizing, instrument-map evidence, broker-observed constraints, a fail-closed
    configuration-readiness verdict (distinct from execution authorisation, which stays OFF), honest unavailable."""
    facct = follower.follower_account
    maps = _verified_maps(master.trading_account, facct)       # back-compat shape
    readiness = follower_config_readiness(master, follower)
    map_status = readiness["map_status"]
    constraints = readiness["constraints"]
    follower_current = readiness["follower_current"]
    last_event = (MasterEvent.objects.filter(master=master).order_by("-sequence")
                  .values("event_type", "symbol", "observed_at").first())
    # Separated readiness dimensions (readiness-semantics repair): each is an independent truth, so the UI can
    # show precisely WHICH condition is unmet instead of a single conflated "Ready". A pair is copy-ready only
    # when every copy dimension holds; `config_status` is the fail-closed roll-up and `config_blocked_reason`
    # the precise cause. LIVE follower execution is always blocked here (execution OFF).
    follower_fresh_state, _ = _freshness(facct.id)
    _cfg_ready = readiness["config_status"] == "ready"
    readiness_breakdown = {
        "terminal_connected": bool(follower_current),
        "snapshot_fresh": follower_fresh_state == "monitored",
        "direction_mapped": bool(map_status.get("pairs")) or bool(map_status.get("same_broker")),
        "mapping_verified": bool(map_status.get("ready")),
        "constraints_available": bool(constraints.get("available")),
        "sizing_valid": _cfg_ready,
        "shadow_ready": _cfg_ready and bool(follower_current) and follower_fresh_state == "monitored",
        "live_blocked": True,
    }
    return {
        "follower_account_id": follower.follower_account_id,
        "enabled": follower.enabled,
        # A retained but now-disconnected follower is NOT current: surface it so the UI can disclose that operator
        # attention is required, rather than silently dropping or substituting it.
        "follower_current": follower_current,
        "sizing_mode": follower.sizing_mode,
        "fixed_lot": str(follower.fixed_lot) if follower.fixed_lot is not None else None,
        "max_volume": str(follower.max_volume) if follower.max_volume is not None else None,
        "max_concurrent_positions": follower.max_concurrent_positions,
        "instrument_map_ready": bool(maps),
        "instrument_maps": maps,
        # Structured map evidence (Workstream B): source symbol, follower symbol, verification state + time, reason.
        "instrument_map": map_status,
        # Broker-OBSERVED limits, kept distinct from the CONFIGURED sizing above (Workstream A).
        "observed_constraints": constraints,
        # Fail-closed configuration verdict (Workstream A): "ready" | "blocked" + an explicit reason. Not execution.
        "config_status": readiness["config_status"],
        "config_blocked_reason": readiness["config_blocked_reason"],
        # Separated per-dimension readiness (NOT a single conflated "Ready"); copy-readiness ⇔ config_status=="ready".
        "readiness": readiness_breakdown,
        "observer_last_observed_at": master.last_observed_at.isoformat() if master.last_observed_at else None,
        "last_observed_event": last_event or None,
        "supported_risk_controls": ["FIXED_LOT", "max_volume", "max_concurrent_positions"],
        # Honest unavailability (not invented): per-account margin/leverage are not exposed by the deployed snapshot.
        "unavailable_risk_metadata": ["margin_mode", "free_margin", "used_margin", "margin_level", "leverage"],
        "live_execution_blocked": _account_environment(facct) == "live",
    }


def _current_config(user):
    """The caller's current SHADOW config: the (single) master they own + its followers, or None."""
    master = (CopierMaster.objects.select_related("trading_account")
              .filter(trading_account__user=user).order_by("-observe_enabled", "id").first())
    if master is None:
        return None
    followers = list(master.followers.select_related("follower_account").order_by("id"))
    master_current = master.trading_account.disconnected_at is None
    follower_views = [_pair_view(master, f) for f in followers]
    # Fail-safe disclosure (never silent substitution): if the configured master, or any ENABLED follower, has
    # since been disconnected, the config references a now-ineligible account and needs operator attention. We
    # still show the saved rows (no auto-delete/auto-substitute, no execution) — the UI raises the flag.
    needs_attention = (not master_current) or any(
        (not fv["follower_current"]) for fv, f in zip(follower_views, followers) if f.enabled)
    return {
        "master_account_id": master.trading_account_id,
        "observe_enabled": master.observe_enabled,
        "master_current": master_current,
        "needs_operator_attention": needs_attention,
        "followers": follower_views,
    }


class CopyTradingConfigView(APIView):
    """GET/POST the founder SHADOW configuration. Cohort-gated (default-DENY), owner-scoped, audited, idempotent."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not copier_ui_enabled_for(request.user):
            return Response({"enabled": False, "banner": execution_banner(), "execution_status": execution_status()})
        return Response({
            "enabled": True, "banner": execution_banner(), "execution": execution_status()["execution"],
            "execution_status": execution_status(),
            "explain": ("GuvFX is observing and comparing potential replication. No follower broker order is placed. "
                        "Saving configuration does NOT enable execution."),
            "eligible_accounts": _eligible_accounts(request.user),
            "config": _current_config(request.user),
        })

    def _owned_live(self, request, account_id):
        """The caller's OWN CURRENT (live, non-decommissioned) account by id, or None. A foreign / demo /
        inactive-tombstone (disconnected) id returns None → the caller 404s, never leaking existence and never
        accepting a historical/disconnected account as a master or follower (the eligibility safety fix)."""
        return current_live_accounts(request.user).filter(id=account_id).first()

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
                # Reject a lot this follower's broker cannot execute (below-min / above-max / off-step), using the
                # follower's OWN observed symbol constraints. When constraints are unavailable (no verified map or
                # un-synced instruments, e.g. Pepperstone #49) this is a no-op here and the follower is instead
                # surfaced as config "blocked" by _pair_view — fail-closed, never a silent accept.
                constraints = _observed_constraints(facct, _map_status(master_acct, facct))
                _validate_sizing_against_constraints(cfg, constraints)
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
            "ok": True, "banner": execution_banner(), "execution": execution_status()["execution"],
            "audit": audit, "config": _current_config(request.user),
        })
