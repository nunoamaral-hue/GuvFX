"""ADR-0034 Execution Engine capstone — DARK provisioning of the hosted execution route (operator tool).

Sets up the DURABLE, server-side records the hosted execution loop resolves against — WITHOUT executing,
attaching, logging in, launching MT5, or placing any order. Everything here is inert config: the loop only
runs when the certified bridge is started in HOSTED mode on the bound node by a node-aware worker, and every
live order-time gate still applies. Idempotent; audited; reversible while DARK.

Steps (each optional / gated by args):
  --account-id N              the Provider-B TradingAccount to route for
  --node-hostname H           bind the account + workspace to this execution TerminalNode (created if absent)
  --grant-worker WID          add H to an EXISTING WorkerIdentity's authorized_nodes (make it node-aware)
  --arm                       run the explicit, fully-preconditioned arm (still fails closed if not ready)
  --unbind / --disarm         reverse while DARK

This command sets NO broker credential and NO worker secret (register a WorkerIdentity + its secret with the
existing tooling first). It never places an order — arming only flips a durable boolean that the live gates
still sit in front of.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from execution import hosted_provisioning as P
from execution.models import TerminalNode, WorkerIdentity
from trading.models import TradingAccount


class Command(BaseCommand):
    help = "DARK-provision the hosted execution route (bind node, grant worker, arm). Places no order."

    def add_arguments(self, parser):
        parser.add_argument("--account-id", type=int, required=True)
        parser.add_argument("--node-hostname", type=str, default="")
        parser.add_argument("--grant-worker", type=str, default="")
        parser.add_argument("--arm", action="store_true")
        parser.add_argument("--disarm", action="store_true")
        parser.add_argument("--unbind", action="store_true")

    def handle(self, *args, **opts):
        acct = TradingAccount.objects.filter(pk=opts["account_id"]).first()
        if acct is None:
            raise CommandError(f"account {opts['account_id']} not found")

        if opts["node_hostname"]:
            node, _ = TerminalNode.objects.get_or_create(hostname=opts["node_hostname"])
            if acct.terminal_node_id != node.pk:
                acct.terminal_node = node
                acct.save(update_fields=["terminal_node"])
            P.provision_hosted_workspace(acct, actor="provision_hosted_execution")
            P.assign_workspace_execution_node(acct, node, actor="provision_hosted_execution")
            self.stdout.write(self.style.SUCCESS(
                f"bound account {acct.pk} + workspace -> node {node.hostname} "
                f"(gen={acct.hosted_workspace.execution_binding_generation})"))

        if opts["grant_worker"] and opts["node_hostname"]:
            wi = WorkerIdentity.objects.filter(worker_id=opts["grant_worker"]).first()
            if wi is None:
                raise CommandError(f"worker {opts['grant_worker']} not found (register it + its secret first)")
            perms = dict(wi.worker_permissions or {})
            nodes = list(perms.get("authorized_nodes", []))
            if opts["node_hostname"] not in nodes:
                nodes.append(opts["node_hostname"])
                perms["authorized_nodes"] = nodes
                wi.worker_permissions = perms
                wi.save(update_fields=["worker_permissions"])
            self.stdout.write(self.style.SUCCESS(
                f"worker {wi.worker_id} authorized_nodes -> {nodes}"))

        if opts["unbind"]:
            P.clear_workspace_execution_node(acct, actor="provision_hosted_execution")
            self.stdout.write(self.style.WARNING(f"unbound account {acct.pk} workspace execution node"))

        if opts["disarm"]:
            r = P.disarm_hosted_workspace_execution(acct, actor="provision_hosted_execution")
            self.stdout.write(self.style.WARNING(f"disarm: {r.reason_code}"))

        if opts["arm"]:
            r = P.arm_hosted_workspace_execution(acct, actor="provision_hosted_execution")
            style = self.style.SUCCESS if r.ok else self.style.ERROR
            self.stdout.write(style(f"arm: ok={r.ok} reason={r.reason_code}"))
            if not r.ok:
                self.stdout.write("  (arm fails closed until every precondition — incl. the durable "
                                  "workspace->node binding, connected+matched+fresh observation, demo-only "
                                  "— holds; the live bridge gate still re-proves them before every order.)")
