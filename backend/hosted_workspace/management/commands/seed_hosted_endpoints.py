"""P0-B1.1 — seed HostedExecutionEndpoint rows for EXISTING live hosted tenants onto their CURRENT bridges,
so enabling HOSTED_PER_TENANT_TRANSPORT_ENABLED does not re-home them.

For every hosted workspace whose execution node already carries an ``order_bridge_base_url`` (i.e. a live
per-node bridge is serving that tenant today — support@ on :8789, Customer Zero on :8788), create/reactivate
its endpoint at EXACTLY that host:port (server-derived from the node URL; never a fresh port) and mark it
READY (its bridge is already live). Idempotent and read-only outside the endpoint table. NO hardcoded account
id. Dry-run by default; pass --apply to write.
"""
from __future__ import annotations

import re

from django.core.management.base import BaseCommand

from execution import endpoint_service


def _split_host_port(url: str):
    m = re.match(r"^https?://([^:/]+):(\d+)/?$", str(url or "").strip())
    return (m.group(1), int(m.group(2))) if m else (None, None)


class Command(BaseCommand):
    help = "Seed HostedExecutionEndpoint rows for existing live tenants onto their current per-node bridges."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write changes (default: dry-run).")

    def handle(self, *args, **opts):
        from hosted_workspace.models import HostedMt5Workspace

        apply = bool(opts["apply"])
        seeded, skipped = [], []
        qs = HostedMt5Workspace.objects.exclude(execution_node=None).select_related(
            "execution_node", "trading_account")
        for ws in qs.order_by("id"):
            node = ws.execution_node
            host, port = _split_host_port(getattr(node, "order_bridge_base_url", ""))
            acct_id = ws.trading_account_id
            if host is None or port is None:
                skipped.append((acct_id, "node_has_no_bridge_url"))
                continue
            base = "http://%s:%d" % (host, port)
            if not apply:
                seeded.append((acct_id, base, "DRY-RUN"))
                continue
            try:
                res = endpoint_service.allocate_endpoint(
                    ws, actor="seed_hosted_endpoints", explicit_port=port, explicit_base_url=base)
                endpoint_service.mark_ready(ws, health_ok=True, actor="seed_hosted_endpoints")
                seeded.append((acct_id, res.base_url, res.reason))
            except endpoint_service.EndpointError as e:
                skipped.append((acct_id, e.reason))

        for acct_id, base, reason in seeded:
            self.stdout.write(f"  SEED acct={acct_id} -> {base} ({reason})")
        for acct_id, reason in skipped:
            self.stdout.write(f"  SKIP acct={acct_id} ({reason})")
        self.stdout.write(self.style.SUCCESS(
            f"{'APPLIED' if apply else 'DRY-RUN'}: seeded={len(seeded)} skipped={len(skipped)}"))
