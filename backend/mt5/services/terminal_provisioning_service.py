"""
Terminal provisioning service — auto-creates TerminalBinding and
UserToTerminalAuthorization when a TradingAccount is connected.

Single-node deployment: maps user's Mt5Instance to a TerminalNode
by hostname match, then creates/reuses a TerminalBinding for the
user's account login.
"""
import logging

from django.utils import timezone

from execution.models import TerminalNode
from mt5.models import TerminalBinding, UserToTerminalAuthorization
from trading.models import TradingAccount

logger = logging.getLogger(__name__)


def provision_terminal_for_account(user, account: TradingAccount) -> TerminalBinding | None:
    """
    Ensure a TerminalBinding and UserToTerminalAuthorization exist
    for the given user + account.

    Returns the TerminalBinding, or None if provisioning is not possible
    (e.g., no Mt5Instance, no matching TerminalNode).
    """
    if not account.mt5_instance_id:
        logger.info("provision_terminal: account %d has no mt5_instance", account.id)
        return None

    inst = account.mt5_instance
    hostname = inst.hostname

    # Find matching TerminalNode by hostname
    node = TerminalNode.objects.filter(
        hostname=hostname,
        status=TerminalNode.Status.ACTIVE,
    ).first()

    if not node:
        logger.info("provision_terminal: no active TerminalNode for hostname=%s", hostname)
        return None

    # Terminal identifier: unique per node + account login
    terminal_id = f"mt5-{account.account_number}"
    env_type = "demo" if account.is_demo else "live"

    # Create or reuse binding
    binding, b_created = TerminalBinding.objects.get_or_create(
        terminal_node=node,
        terminal_identifier=terminal_id,
        defaults={
            "mt5_account_login": account.account_number,
            "environment_type": env_type,
            "terminal_label": f"{account.name} ({account.account_number})",
            "status": TerminalBinding.Status.AVAILABLE,
        },
    )

    if b_created:
        logger.info("provision_terminal: created binding id=%d for account %s", binding.id, account.account_number)
    else:
        # Update label if account name changed
        if binding.terminal_label != f"{account.name} ({account.account_number})":
            binding.terminal_label = f"{account.name} ({account.account_number})"
            binding.save(update_fields=["terminal_label"])

    # Create or reuse authorization
    auth, a_created = UserToTerminalAuthorization.objects.get_or_create(
        user=user,
        terminal_binding=binding,
        defaults={
            "access_mode": "full" if account.is_demo else "view_only",
            "can_launch": True,
            "can_resume": True,
            "can_manual_trade": account.is_demo,
            "can_chart_interact": True,
            "granted_at": timezone.now(),
        },
    )

    if a_created:
        logger.info("provision_terminal: created auth id=%d for user %s", auth.id, user.email)

    return binding
