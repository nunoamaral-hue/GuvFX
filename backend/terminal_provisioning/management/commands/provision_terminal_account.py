"""
TX-1A / TX-1B operations command.

Django-side provisioning + lifecycle for an account's isolation profile.
NEVER prints the generated Windows password (the secret stays Fernet-encrypted
in the DB; the Windows executor receives it over a separate secure channel).

Examples:
  manage.py provision_terminal_account --account-id 14 --action provision
  manage.py provision_terminal_account --account-id 14 --action status
  manage.py provision_terminal_account --account-id 14 --action disable
"""
import json

from django.core.management.base import BaseCommand, CommandError

from trading.models import TradingAccount
from terminal_provisioning import services
from terminal_provisioning.models import AccountProvisioning


class Command(BaseCommand):
    help = "TX-1 terminal isolation: provision / lifecycle for an account (no secrets printed)."

    def add_arguments(self, parser):
        parser.add_argument("--account-id", type=int, required=True)
        parser.add_argument(
            "--action",
            choices=["provision", "status", "disable", "enable", "retire", "mark-materialized"],
            default="provision",
        )
        parser.add_argument("--identity-materialized", action="store_true")
        parser.add_argument("--runtime-materialized", action="store_true")

    def handle(self, *args, **opts):
        try:
            account = TradingAccount.objects.get(pk=opts["account_id"])
        except TradingAccount.DoesNotExist:
            raise CommandError(f"TradingAccount {opts['account_id']} not found")

        action = opts["action"]
        try:
            if action == "provision":
                prov = services.provision(account)
            elif action == "disable":
                prov = services.disable(account)
            elif action == "enable":
                prov = services.enable(account)
            elif action == "retire":
                prov = services.retire(account)
            elif action == "mark-materialized":
                prov = services.mark_materialized(
                    account,
                    identity=True if opts["identity_materialized"] else None,
                    runtime=True if opts["runtime_materialized"] else None,
                )
            else:  # status
                prov = AccountProvisioning.objects.filter(trading_account=account).first()
                if prov is None:
                    self.stdout.write(json.dumps({"account_id": account.id, "provisioned": False}))
                    return
        except services.ProvisioningError as e:
            raise CommandError(f"provisioning failed (controlled): {e}")

        self.stdout.write(json.dumps({
            "account_id": prov.trading_account_id,
            "windows_username": prov.windows_username,
            "runtime_root": prov.runtime_root,
            "subdirs": list(prov.runtime_structure.keys()),
            "is_admin": prov.is_admin,
            "status": prov.status,
            "identity_materialized": prov.identity_materialized,
            "runtime_materialized": prov.runtime_materialized,
            "password_exposed": False,
        }, indent=2))
