"""
EXEC-E1a — approval → ProposedSignalOrder bridge + execution control.

HARD BOUNDARY — this module creates ``ProposedSignalOrder`` rows ONLY. It
NEVER:

  * creates an ``ExecutionJob`` (or any worker-claimable PENDING job),
  * calls ``create_open_trade_job`` / ``order_send`` / any broker call,
  * contacts MT5 or the Windows agent,
  * uses broker credentials,
  * activates a Telegram listener.

A ``ProposedSignalOrder`` is structurally invisible to the worker claim path
(``ExecutionJob.objects.filter(status=PENDING)`` in ``execution.views``
``next_job``), so no order can ever result from one. Promotion of a proposal to
an executable job is a separate, sponsor-gated packet (E2+).

Boundary direction: ``execution`` may read ``signal_intake`` (one-way). The
content path (``wims`` / ``intelligence``) and ``signal_intake`` itself never
import ``execution`` — enforced by the ADR-009 guard tests.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional

from django.db import IntegrityError, transaction

from execution.broker_symbols import can_account_trade_symbol
from execution.models import (
    DEMO_FIXED_LOT_SIZE,
    SIGNAL_MAX_CONCURRENT_POSITIONS,
    SIGNAL_MAX_LOT_SIZE,
    SIGNAL_MAX_TRADES_PER_DAY,
    ExecutionControl,
    ProposalAuditEvent,
    ProposedSignalOrder,
    order_creation_kill_reason,
)
from signal_intake.models import PendingSignalApproval


class ProposalRejected(Exception):
    """Raised when a proposal cannot be created. Carries a stable ``code``.

    A PROPOSAL_REJECTED audit row is always written *before* this is raised, and
    persists even though no ``ProposedSignalOrder`` is created (the rejection
    path performs no enclosing transaction that could roll the audit back).
    """

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


# ---------------------------------------------------------------------------
# Audit helpers
# ---------------------------------------------------------------------------


def _audit(event, *, proposal=None, approval=None, actor=None, **detail) -> None:
    ProposalAuditEvent.objects.create(
        event=event,
        proposal=proposal,
        approval=approval,
        actor=actor,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Execution control (functional kill switch + signal-specific disable)
# ---------------------------------------------------------------------------


def control_block_reason() -> Optional[str]:
    """Return a stable reason code if proposals are currently blocked, else None.

    Reuses the shared kill-switch source (env flag + DB ``kill_switch_engaged``)
    and adds the proposal-specific ``signal_proposals_enabled`` disable.
    """
    reason = order_creation_kill_reason()
    if reason:
        return reason
    if not ExecutionControl.get_solo().signal_proposals_enabled:
        return "signal_proposals_disabled"
    return None


def engage_kill_switch(*, actor=None, reason: str = "") -> ExecutionControl:
    control = ExecutionControl.get_solo()
    control.kill_switch_engaged = True
    control.reason = reason
    control.updated_by = actor
    control.save()
    _audit(ProposalAuditEvent.Event.KILL_SWITCH_ENGAGED, actor=actor, reason=reason)
    return control


def release_kill_switch(*, actor=None, reason: str = "") -> ExecutionControl:
    control = ExecutionControl.get_solo()
    control.kill_switch_engaged = False
    control.reason = reason
    control.updated_by = actor
    control.save()
    _audit(ProposalAuditEvent.Event.KILL_SWITCH_RELEASED, actor=actor, reason=reason)
    return control


def set_signal_proposals_enabled(enabled: bool, *, actor=None, reason: str = "") -> ExecutionControl:
    control = ExecutionControl.get_solo()
    control.signal_proposals_enabled = bool(enabled)
    control.reason = reason
    control.updated_by = actor
    control.save()
    event = (
        ProposalAuditEvent.Event.PROPOSALS_ENABLED
        if enabled
        else ProposalAuditEvent.Event.PROPOSALS_DISABLED
    )
    _audit(event, actor=actor, reason=reason)
    return control


# ---------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------


def _resolve_lot(lot_size) -> Decimal:
    if lot_size is None:
        return Decimal(str(DEMO_FIXED_LOT_SIZE))
    try:
        return Decimal(str(lot_size))
    except (InvalidOperation, ValueError, TypeError):
        raise ProposalRejected("invalid_lot", f"lot_size {lot_size!r} is not a number")


def _validate(approval: PendingSignalApproval, account, lot: Decimal) -> dict:
    """Pure read-only validation. Raises ProposalRejected (no audit) on failure.

    Returns the normalised proposal fields on success. Audit-on-reject is the
    caller's responsibility so the rejection persists outside any transaction.
    """
    if approval.status != PendingSignalApproval.Status.APPROVED:
        raise ProposalRejected(
            "approval_not_approved",
            f"approval #{approval.id} is {approval.status}, not APPROVED",
        )

    blocked = control_block_reason()
    if blocked:
        raise ProposalRejected(blocked, "execution control blocks proposals")

    if not account.is_demo:
        raise ProposalRejected(
            "account_not_demo", "proposals are demo-only in E1a (account is not demo)"
        )

    env = ""
    if account.broker_server_id:
        env = account.broker_server.environment or ""
        if env.lower() == "live":
            raise ProposalRejected(
                "account_live", "live accounts are not permitted in E1a"
            )

    if ProposedSignalOrder.objects.filter(approval=approval).exists():
        existing = ProposedSignalOrder.objects.get(approval=approval)
        raise ProposalRejected(
            "duplicate_proposal",
            f"approval #{approval.id} already has proposal #{existing.id}",
        )

    symbol = (approval.symbol or "").upper()
    direction = (approval.direction or "").upper()
    if not symbol or direction not in (
        ProposedSignalOrder.Direction.BUY,
        ProposedSignalOrder.Direction.SELL,
    ):
        raise ProposalRejected(
            "not_tradeable", "approval lacks a usable symbol/direction"
        )

    # Broker/account-aware symbol gate (fail-closed with a specific reason).
    _sym_res = can_account_trade_symbol(account, symbol)
    if not _sym_res.accepted:
        raise ProposalRejected(_sym_res.reason, f"{symbol}: {_sym_res.reason}")

    if lot <= 0:
        raise ProposalRejected("invalid_lot", f"lot {lot} must be positive")
    if lot > Decimal(str(SIGNAL_MAX_LOT_SIZE)):
        raise ProposalRejected(
            "lot_exceeds_cap", f"lot {lot} exceeds cap {SIGNAL_MAX_LOT_SIZE}"
        )

    if ProposedSignalOrder.count_today(account.id, symbol) >= SIGNAL_MAX_TRADES_PER_DAY:
        raise ProposalRejected(
            "daily_limit_exceeded",
            f"daily proposal limit reached ({SIGNAL_MAX_TRADES_PER_DAY})",
        )

    if (
        ProposedSignalOrder.count_active(account.id, symbol)
        >= SIGNAL_MAX_CONCURRENT_POSITIONS
    ):
        raise ProposalRejected(
            "concurrent_limit_exceeded",
            f"concurrent proposal limit reached ({SIGNAL_MAX_CONCURRENT_POSITIONS})",
        )

    return {"symbol": symbol, "direction": direction, "environment": env}


def propose_order_from_approval(
    approval: PendingSignalApproval,
    *,
    account,
    actor=None,
    lot_size=None,
    notes: str = "",
) -> ProposedSignalOrder:
    """Create a ``ProposedSignalOrder`` from an APPROVED approval onto a demo account.

    Creates NO ``ExecutionJob`` and places NO order. On any safety failure a
    PROPOSAL_REJECTED audit is written and ``ProposalRejected`` is raised.
    """
    lot = _resolve_lot(lot_size)
    try:
        fields = _validate(approval, account, lot)
    except ProposalRejected as exc:
        # Persist the rejection in its own (autocommit) write so it survives even
        # if the caller aborts. No order, no job — only an audit row.
        _audit(
            ProposalAuditEvent.Event.PROPOSAL_REJECTED,
            approval=approval,
            actor=actor,
            code=exc.code,
            message=exc.message,
            account_id=getattr(account, "id", None),
        )
        raise

    try:
        with transaction.atomic():
            proposal = ProposedSignalOrder.objects.create(
                approval=approval,
                account=account,
                symbol=fields["symbol"],
                direction=fields["direction"],
                entry=approval.entry or "",
                stop_loss=approval.stop_loss or "",
                take_profit=approval.take_profit or "",
                lot_size=lot,
                is_demo=account.is_demo,
                account_environment=fields["environment"],
                proposed_by=actor,
                notes=notes,
            )
            _audit(
                ProposalAuditEvent.Event.PROPOSAL_CREATED,
                proposal=proposal,
                approval=approval,
                actor=actor,
                symbol=proposal.symbol,
                direction=proposal.direction,
                lot_size=str(lot),
                account_id=account.id,
            )
    except IntegrityError:
        # Lost a race on the OneToOne(approval) — treat as duplicate.
        _audit(
            ProposalAuditEvent.Event.PROPOSAL_REJECTED,
            approval=approval,
            actor=actor,
            code="duplicate_proposal",
            message=f"approval #{approval.id} already has a proposal",
        )
        raise ProposalRejected(
            "duplicate_proposal", f"approval #{approval.id} already has a proposal"
        )

    return proposal
