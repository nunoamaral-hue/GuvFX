"""copier_live_demo_preflight — a READ-ONLY, machine-readable readiness probe for a governed #46->#48 live
copy-trading demo (ADR-0057 control-plane completion, 2026-11-10).

Makes NO database, host, flag, authorization or broker mutation. Evaluates every backend-verifiable gate
authoritatively and emits structured JSON with per-gate reason codes and a final ``verdict`` of
``READY_TO_ARM`` / ``NOT_READY``. Host-side facts the backend cannot read without the agent token (bridge /
watchdog / host-admission / live follower positions) are reported as ``HOST_PROBE_REQUIRED`` and keep the
verdict ``NOT_READY`` (fail-closed) until an operator confirms them out of band. Only secret NAMES are ever
reported, never values.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand
from django.utils import timezone


def _mask(v) -> str:
    s = str(v or "")
    return ("…" + s[-4:]) if len(s) >= 4 else "****"


class Command(BaseCommand):
    help = "Read-only machine-readable readiness probe for a governed live copier demo (no mutation)."

    def add_arguments(self, parser):
        parser.add_argument("--master-account-id", required=True, type=int)
        parser.add_argument("--follower-account-id", required=True, type=int)
        parser.add_argument("--source-symbol", required=True)
        parser.add_argument("--follower-symbol", required=True)

    def handle(self, *args, **o):
        now = timezone.now()
        gates: dict[str, dict] = {}
        blockers: list[str] = []
        host_probe: list[str] = []

        def gate(name, ok, code, **extra):
            gates[name] = {"ok": bool(ok), "code": code, **extra}
            if not ok and code != "HOST_PROBE_REQUIRED":
                blockers.append(code)
            if code == "HOST_PROBE_REQUIRED":
                host_probe.append(name)

        from trading.models import TradingAccount
        from trading.account_policy import is_live_environment
        mpk, fpk = o["master_account_id"], o["follower_account_id"]
        macct = TradingAccount.objects.select_related("broker_server", "user").filter(pk=mpk).first()
        facct = TradingAccount.objects.select_related("broker_server", "user").filter(pk=fpk).first()

        # 1 — identity + environment + ownership
        if macct and facct:
            same_owner = macct.user_id == facct.user_id
            both_live = is_live_environment(macct) and is_live_environment(facct)
            gate("identity", same_owner and both_live and macct.id != facct.id,
                 "IDENTITY_OK" if (same_owner and both_live) else "IDENTITY_FAILED",
                 master={"id": macct.id, "login": _mask(macct.account_number),
                         "server": macct.broker_server.server_name if macct.broker_server_id else None,
                         "is_demo": macct.is_demo, "is_active": macct.is_active, "owner": macct.user_id},
                 follower={"id": facct.id, "login": _mask(facct.account_number),
                           "server": facct.broker_server.server_name if facct.broker_server_id else None,
                           "is_demo": facct.is_demo, "is_active": facct.is_active, "owner": facct.user_id})
        else:
            gate("identity", False, "ACCOUNT_NOT_FOUND")
            self.stdout.write(json.dumps({"verdict": "NOT_READY", "gates": gates, "blockers": blockers}, default=str))
            return

        # 2 — verified symbol map
        try:
            from trade_copier.symbols import resolve_follower_symbol
            sym = resolve_follower_symbol(macct, o["source_symbol"], facct)
            ok = bool(getattr(sym, "ok", False)) and getattr(sym, "follower_symbol", None) == o["follower_symbol"]
            gate("symbol_map", ok, "SYMBOL_MAP_OK" if ok else "SYMBOL_MAP_UNVERIFIED",
                 resolved=getattr(sym, "follower_symbol", None))
        except Exception as e:  # noqa: BLE001
            gate("symbol_map", False, "SYMBOL_MAP_ERROR", detail=type(e).__name__)

        # 3 — route (sole #master->#follower; no sibling enabled)
        from trade_copier.models import (CopierFollower, CopierLiveAuthorization, CopierLiveAuthorizationStatus,
                                         CopierLiveKillSwitch, CopierMaster, CopierReplication, MasterEvent)
        enabled = list(CopierFollower.objects.filter(enabled=True)
                       .values_list("id", "master__trading_account_id", "follower_account_id"))
        route_ok = enabled == [(enabled[0][0], mpk, fpk)] if len(enabled) == 1 else False
        gate("route", route_ok, "ROUTE_OK" if route_ok else "ROUTE_NOT_SOLE", enabled_followers=enabled)

        # 4 — destination instrument enabled
        from execution.models import BrokerInstrument
        inst_ok = BrokerInstrument.objects.filter(account_id=fpk, broker_symbol=o["follower_symbol"], enabled=True).exists()
        gate("instrument", inst_ok, "INSTRUMENT_OK" if inst_ok else "INSTRUMENT_NOT_READY")

        # 5 — authorization status + remaining lifetime
        active = list(CopierLiveAuthorization.objects.filter(
            master_account_id=mpk, follower_account_id=fpk, status=CopierLiveAuthorizationStatus.ACTIVE))
        unexpired = [a for a in active if a.expires_at > now]
        if len(unexpired) == 1 and len(active) == 1:
            a = unexpired[0]
            gate("authorization", True, "AUTHZ_OK", id=a.id,
                 remaining_seconds=int((a.expires_at - now).total_seconds()),
                 modify=a.modify_authority, full_close=a.full_close_authority,
                 max_orders=a.max_orders, orders_consumed=a.orders_consumed, watermark_seq=a.watermark_event_sequence)
        elif not active:
            gate("authorization", False, "AUTHZ_NONE")
        elif not unexpired:
            gate("authorization", False, "AUTHZ_EXPIRED_ACTIVE", ids=[a.id for a in active])
        else:
            gate("authorization", False, "AUTHZ_MULTIPLE", ids=[a.id for a in active])

        # 6 — backend execution flags (all expected OFF pre-arm)
        from trade_copier import flags
        fnames = ["copier_enabled", "copier_execute_enabled", "copier_live_execute_enabled",
                  "copier_mutation_dispatch_enabled", "copier_mutation_transport_enabled",
                  "copier_lifecycle_guard_enabled", "copier_live_modify_enabled", "copier_live_full_close_enabled"]
        fvals = {n: bool(getattr(flags, n)()) for n in fnames if hasattr(flags, n)}
        gate("flags_off", not any(fvals.values()), "FLAGS_OFF" if not any(fvals.values()) else "FLAGS_ARMED", values=fvals)

        # 7 — kill switch clear
        ks = CopierLiveKillSwitch.objects.filter(engaged=True).count()
        gate("kill_switch", ks == 0, "KILLSWITCH_CLEAR" if ks == 0 else "KILLSWITCH_ENGAGED", engaged=ks)

        # 8 — zero queued/running copier mutation/place jobs
        from execution.models import ExecutionJob
        jobs = ExecutionJob.objects.filter(
            job_type__in=["PLACE_ORDER", "MODIFY_POSITION", "CLOSE_TRADE", "PARTIAL_CLOSE_TRADE"],
            status__in=["QUEUED", "RUNNING", "PENDING"]).count()
        gate("jobs_zero", jobs == 0, "JOBS_ZERO" if jobs == 0 else "JOBS_PRESENT", count=jobs)

        # 9 — zero copier broker tickets
        tix = CopierReplication.objects.filter(follower_ticket__isnull=False).count()
        gate("tickets_zero", tix == 0, "TICKETS_ZERO" if tix == 0 else "TICKETS_PRESENT", count=tix)

        # 10 — watermark / replay boundary + master position cleanliness
        cm = CopierMaster.objects.filter(trading_account_id=mpk).first()
        if cm:
            from django.db.models import Max
            wm = cm.events.aggregate(m=Max("sequence")).get("m") or 0
            rows = list(MasterEvent.objects.filter(master=cm).order_by("position_ticket", "sequence")
                        .values("position_ticket", "event_type"))
            latest = {}
            for r in rows:
                latest[r["position_ticket"]] = r["event_type"]
            open_pos = sum(1 for et in latest.values() if et not in ("FULL_CLOSE", "CLOSE"))
            gate("master_clean", open_pos == 0, "MASTER_CLEAN" if open_pos == 0 else "MASTER_HAS_OPEN",
                 watermark_seq=wm, open_positions=open_pos,
                 observer_last_observed=cm.last_observed_at.isoformat() if cm.last_observed_at else None)
        else:
            gate("master_clean", False, "NO_COPIER_MASTER")

        # 11 — follower per-tenant endpoint resolves (fail-closed, never a global/sibling bridge)
        try:
            from execution.snapshot_transport import resolve_account_snapshot_base
            from django.conf import settings as _s
            st = resolve_account_snapshot_base(facct, global_base_url=getattr(_s, "GUVFX_WINDOWS_AGENT_BASE_URL", "") or "")
            e_ok = bool(getattr(st, "ok", False)) and bool(getattr(st, "per_tenant", False))
            gate("follower_endpoint", e_ok, "ENDPOINT_OK" if e_ok else "ENDPOINT_NOT_PER_TENANT",
                 reason=getattr(st, "reason_code", None), per_tenant=getattr(st, "per_tenant", None))
        except Exception as e:  # noqa: BLE001
            gate("follower_endpoint", False, "ENDPOINT_RESOLVE_ERROR", detail=type(e).__name__)

        # 11b — ADR-0094 follower live-position READ readiness (governed MODIFY/CLOSE ticket-linkage capability).
        # Informational, NOT a blocker: the dedicated follower-read authority is DARK/default-OFF in production,
        # so NOT_AUTHORIZED is the expected posture until a separate Sponsor-gated enablement. Reported directly
        # (not via gate()) so it never adds to ``blockers`` or flips the verdict.
        try:
            from execution.bridge_config import follower_positions_read_readiness
            fr_code, fr_detail = follower_positions_read_readiness(facct)
        except Exception as e:  # noqa: BLE001
            fr_code, fr_detail = ("FOLLOWER_POSITIONS_READ_UNAVAILABLE", type(e).__name__)
        gates["follower_positions_read"] = {"ok": fr_code == "FOLLOWER_POSITIONS_READ_OK",
                                            "code": fr_code, "detail": fr_detail}

        # 12 — worker availability (the authoritative command sequence)
        from django.core.management import get_commands
        cmds = set(get_commands().keys())
        needed = ["run_copier_observer", "run_copier_replication", "run_copier_execute", "run_copier_lifecycle",
                  "run_copier_execute_modifies", "run_copier_execute_closes", "run_copier_mutation_dispatch",
                  "provision_authorized_live_copier_bridge", "restore_copier_bridge_execution_off",
                  "grant_copier_live_authorization", "revoke_copier_live_authorization"]
        missing = [c for c in needed if c not in cmds]
        gate("workers", not missing, "WORKERS_OK" if not missing else "WORKERS_MISSING", missing=missing)

        # 13 — host-side facts the backend cannot read without the agent token (fail-closed)
        gate("host_bridge_watchdog", False, "HOST_PROBE_REQUIRED",
             probe="port 8804 LISTEN + GuvFX_TenantBridge_48/Watchdog Running + /health via agent token")
        gate("host_admission_off", False, "HOST_PROBE_REQUIRED",
             probe="COPIER_LIVE_MUTATION_ADMIT_ENABLED/MT5_ALLOW_LIVE/MT5_LIVE_EXPIRY absent on #48 bridge")
        gate("follower_positions_clean", False, "HOST_PROBE_REQUIRED",
             probe="#48 has no unrelated XAUUSD.std position/pending order (read-only MT5)")

        # required secret NAMES only (never values)
        secret_names = ["GUVFX_AGENT_TOKEN|WINDOWS_AGENT_TOKEN|GUVFX_WINDOWS_AGENT_TOKEN (bridge auth)",
                        "COPIER_INGEST_HMAC_KEY (event ingest)"]

        verdict = "READY_TO_ARM" if not blockers and not host_probe else "NOT_READY"
        out = {"verdict": verdict, "checked_at": now.isoformat(),
               "route": {"master": mpk, "follower": fpk,
                         "source_symbol": o["source_symbol"], "follower_symbol": o["follower_symbol"]},
               "blockers": sorted(set(blockers)), "host_probe_required": host_probe,
               "required_secret_names": secret_names, "gates": gates}
        self.stdout.write(json.dumps(out, default=str, indent=1))
