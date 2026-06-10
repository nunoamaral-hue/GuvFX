"""
Strategy Live Status Health Check

Single-run command that checks whether a TBP strategy is "live" end-to-end.

Checks:
1. Strategy exists and is_active
2. Assignment exists for account+strategy and is_active
3. Recent scheduler activity (PLACE_ORDER jobs in last 24h)
4. Windows agent connectivity (deals + OHLC agents)
5. Ingest worker health (recent SYNC_POSITIONS jobs)

Usage:
    python manage.py strategy_live_status --strategy-id 12 --account-id 13
"""

import os
import json
import urllib.request
import urllib.parse
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from execution.models import ExecutionJob
from strategies.models import Strategy, StrategyAssignment
from trading.models import TradingAccount


class Command(BaseCommand):
    help = "Check if a strategy is live and functioning end-to-end"

    def add_arguments(self, parser):
        parser.add_argument("--strategy-id", type=int, required=True, help="Strategy ID")
        parser.add_argument("--account-id", type=int, required=True, help="Trading Account ID")
        parser.add_argument("--json", action="store_true", help="Output as JSON (for API use)")

    def handle(self, *args, **options):
        strategy_id = options["strategy_id"]
        account_id = options["account_id"]
        output_json = options.get("json", False)

        checks = []
        now = timezone.now()
        lookback_24h = now - timedelta(hours=24)

        # ---------------------------------------------------------------
        # Check 1: Strategy exists and is_active
        # ---------------------------------------------------------------
        strategy = Strategy.objects.filter(id=strategy_id).first()
        if not strategy:
            checks.append({"name": "strategy_exists", "status": "FAIL", "detail": f"Strategy {strategy_id} not found"})
        elif not strategy.is_active:
            checks.append({"name": "strategy_active", "status": "FAIL", "detail": f"Strategy '{strategy.name}' is_active=False"})
        else:
            checks.append({"name": "strategy_active", "status": "PASS", "detail": f"Strategy '{strategy.name}' is_active=True"})

        # ---------------------------------------------------------------
        # Check 2: Account exists and is_active + is_demo
        # ---------------------------------------------------------------
        account = TradingAccount.objects.filter(id=account_id).first()
        if not account:
            checks.append({"name": "account_exists", "status": "FAIL", "detail": f"Account {account_id} not found"})
        elif not account.is_active:
            checks.append({"name": "account_active", "status": "FAIL", "detail": f"Account {account_id} is_active=False"})
        elif not account.is_demo:
            checks.append({"name": "account_demo", "status": "WARN", "detail": f"Account {account_id} is_demo=False (live accounts not supported yet)"})
        else:
            checks.append({"name": "account_active", "status": "PASS", "detail": f"Account {account_id} is_active=True, is_demo=True"})

        # ---------------------------------------------------------------
        # Check 3: Assignment exists and is_active
        # ---------------------------------------------------------------
        assignment = None
        if strategy and account:
            assignment = StrategyAssignment.objects.filter(
                strategy=strategy,
                account=account,
                is_active=True,
            ).first()

        if not assignment:
            checks.append({"name": "assignment_active", "status": "FAIL",
                           "detail": f"No active assignment for strategy={strategy_id} account={account_id}"})
        else:
            checks.append({"name": "assignment_active", "status": "PASS",
                           "detail": f"Assignment id={assignment.id} is_active=True"})

        # ---------------------------------------------------------------
        # Check 4: Recent PLACE_ORDER jobs (scheduler activity)
        # ---------------------------------------------------------------
        recent_place_orders = ExecutionJob.objects.filter(
            account_id=account_id,
            strategy_id=strategy_id,
            job_type=ExecutionJob.JobType.PLACE_ORDER,
            created_at__gte=lookback_24h,
        ).order_by("-created_at")

        po_count = recent_place_orders.count()
        if po_count > 0:
            latest = recent_place_orders.first()
            checks.append({"name": "scheduler_recent", "status": "PASS",
                           "detail": f"{po_count} PLACE_ORDER jobs in last 24h, latest={latest.status} at {latest.created_at.isoformat()}"})
        else:
            # Also check SIGNAL_EVALUATED audit events
            from core.models import AuditEvent
            recent_evals = AuditEvent.objects.filter(
                event_type="SIGNAL_EVALUATED",
                entity_type="strategy",
                entity_id=str(strategy_id),
                created_at__gte=lookback_24h,
            ).count()
            if recent_evals > 0:
                checks.append({"name": "scheduler_recent", "status": "PASS",
                               "detail": f"0 PLACE_ORDER jobs but {recent_evals} SIGNAL_EVALUATED events in 24h (scheduler ran, no signal triggered)"})
            else:
                checks.append({"name": "scheduler_recent", "status": "FAIL",
                               "detail": "No PLACE_ORDER jobs or SIGNAL_EVALUATED events in last 24h"})

        # ---------------------------------------------------------------
        # Check 5: Windows deals agent reachable
        # ---------------------------------------------------------------
        deals_url = (os.getenv("GUVFX_AGENT_URL") or os.getenv("WINDOWS_AGENT_BASE") or "").rstrip("/")
        deals_token = (os.getenv("WINDOWS_AGENT_TOKEN") or os.getenv("GUVFX_AGENT_TOKEN") or "").strip()
        windows_username = ""
        if account and account.mt5_instance:
            windows_username = getattr(account.mt5_instance, "windows_username", "") or ""

        if not deals_url:
            checks.append({"name": "deals_agent", "status": "WARN", "detail": "GUVFX_AGENT_URL not configured"})
        else:
            try:
                url = f"{deals_url}/mt5/snapshots/deals?username={urllib.parse.quote(windows_username)}&count=1"
                req = urllib.request.Request(url, method="GET")
                if deals_token:
                    req.add_header("X-GuvFX-Agent-Token", deals_token)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8", "ignore"))
                    if data.get("ok") or isinstance(data.get("data"), (list, dict)):
                        checks.append({"name": "deals_agent", "status": "PASS", "detail": f"Deals agent reachable at {deals_url}"})
                    else:
                        checks.append({"name": "deals_agent", "status": "WARN", "detail": f"Deals agent returned unexpected shape: {list(data.keys())}"})
            except Exception as e:
                checks.append({"name": "deals_agent", "status": "FAIL", "detail": f"Deals agent unreachable: {e}"})

        # ---------------------------------------------------------------
        # Check 6: Windows OHLC agent reachable
        # ---------------------------------------------------------------
        ohlc_url = (os.getenv("GUVFX_WINDOWS_AGENT_BASE_URL") or "").rstrip("/")
        # 8788 OHLC bridge authenticates with GUVFX_WINDOWS_AGENT_TOKEN, not the 8787 token.
        ohlc_token = (os.getenv("GUVFX_WINDOWS_AGENT_TOKEN") or os.getenv("GUVFX_AGENT_TOKEN") or "").strip()

        if not ohlc_url:
            checks.append({"name": "ohlc_agent", "status": "WARN", "detail": "GUVFX_WINDOWS_AGENT_BASE_URL not configured"})
        else:
            try:
                url = f"{ohlc_url}/mt5/snapshots/rates?symbol=EURUSD&timeframe=H4&count=5"
                req = urllib.request.Request(url, method="GET")
                if ohlc_token:
                    req.add_header("X-GuvFX-Agent-Token", ohlc_token)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8", "ignore"))
                    if data.get("ok"):
                        bar_count = len(data.get("data", []))
                        checks.append({"name": "ohlc_agent", "status": "PASS", "detail": f"OHLC agent ok, returned {bar_count} bars"})
                    else:
                        checks.append({"name": "ohlc_agent", "status": "FAIL", "detail": f"OHLC agent returned ok=false: {data.get('error', '?')}"})
            except Exception as e:
                checks.append({"name": "ohlc_agent", "status": "FAIL", "detail": f"OHLC agent unreachable: {e}"})

        # ---------------------------------------------------------------
        # Check 7: Ingest worker health (recent SYNC_POSITIONS)
        # ---------------------------------------------------------------
        recent_syncs = ExecutionJob.objects.filter(
            account_id=account_id,
            job_type=ExecutionJob.JobType.SYNC_POSITIONS,
            created_at__gte=lookback_24h,
        ).order_by("-created_at")

        sync_count = recent_syncs.count()
        if sync_count > 0:
            latest_sync = recent_syncs.first()
            checks.append({"name": "ingest_worker", "status": "PASS",
                           "detail": f"{sync_count} SYNC_POSITIONS in 24h, latest={latest_sync.status} at {latest_sync.created_at.isoformat()}"})
        else:
            # Not a hard failure — syncs only happen after trades
            checks.append({"name": "ingest_worker", "status": "WARN",
                           "detail": "No SYNC_POSITIONS jobs in last 24h (normal if no trades placed)"})

        # ---------------------------------------------------------------
        # Overall verdict
        # ---------------------------------------------------------------
        fail_count = sum(1 for c in checks if c["status"] == "FAIL")
        warn_count = sum(1 for c in checks if c["status"] == "WARN")

        if fail_count > 0:
            overall = "FAIL"
        elif warn_count > 0:
            overall = "DEGRADED"
        else:
            overall = "PASS"

        result = {
            "overall": overall,
            "strategy_id": strategy_id,
            "account_id": account_id,
            "checked_at": now.isoformat(),
            "checks": checks,
        }

        if output_json:
            self.stdout.write(json.dumps(result, indent=2))
        else:
            # Human-readable output
            self.stdout.write("")
            self.stdout.write(f"Strategy Live Status: strategy={strategy_id} account={account_id}")
            self.stdout.write("=" * 60)
            for check in checks:
                icon = {"PASS": "✓", "FAIL": "✗", "WARN": "⚠"}[check["status"]]
                color = {"PASS": self.style.SUCCESS, "FAIL": self.style.ERROR, "WARN": self.style.WARNING}[check["status"]]
                self.stdout.write(color(f"  {icon} [{check['status']}] {check['name']}: {check['detail']}"))
            self.stdout.write("=" * 60)
            overall_color = {"PASS": self.style.SUCCESS, "FAIL": self.style.ERROR, "DEGRADED": self.style.WARNING}[overall]
            self.stdout.write(overall_color(f"  OVERALL={overall}"))
            self.stdout.write("")
