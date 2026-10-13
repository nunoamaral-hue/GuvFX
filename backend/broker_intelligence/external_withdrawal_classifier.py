"""§7 — External-withdrawal classifier ARCHITECTURE (disabled by default; NOT wired into the live parsers).

A message is ``EXTERNAL_WITHDRAWAL`` ONLY with POSITIVE evidence that money LEFT the broker environment — a named
external destination, an external settlement rail, or the broker explicitly typing it as an external/outgoing
withdrawal. It is NEVER inferred from the word "withdrawal", the subject, the sender, an amount, or an account number
alone. This module is the decision core; the per-broker EXTRACTION of these structured signals from a real email is
deferred until a GENUINE external-withdrawal sample exists and the broker's positive classifier is certified against
it (there are no such samples yet — the pilot parsers emit only DEPOSIT / INTERNAL_TRANSFER / UNKNOWN).

INVARIANTS (enforced regardless of certification; the whole point of this module):
  * internal transfer  → NEVER external   (money stayed inside the broker)
  * deposit            → NEVER a withdrawal
  * ambiguous / no positive external signal → UNKNOWN (never guessed external)
  * a broker whose positive classifier is NOT certified → UNKNOWN (no speculative positive classification in prod)

Fail-closed: the default ``CERTIFIED_BROKERS`` is EMPTY, so in production this classifier can only ever return
``not external`` until an operator certifies a specific broker against genuine evidence. Pure; no I/O, no DB, no flag.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import TransactionCategory

# Brokers whose POSITIVE external-withdrawal classifier has been CERTIFIED against genuine broker evidence. EMPTY by
# default — fail-closed. A broker is added here ONLY after its genuine external-withdrawal email has been captured and
# the extraction + classification verified against it (governance; §7/§8). Certification is a deliberate operator act,
# never a code default.
CERTIFIED_BROKERS: frozenset = frozenset()


@dataclass(frozen=True)
class ExternalWithdrawalSignals:
    """Structured POSITIVE evidence a broker parser extracted from ONE message. Only these fields can ever justify
    EXTERNAL_WITHDRAWAL — never the subject / sender / amount / account / the mere word "withdrawal". A field is True
    only when the parser found explicit, unambiguous evidence for it in the message body."""
    # Positive external-movement evidence (ANY one, when not overridden below, is sufficient for a certified broker):
    external_destination: bool = False      # a named EXTERNAL institution/bank/wallet (not an internal account)
    external_payment_rail: str = ""         # an external settlement rail, e.g. 'bank_wire' / 'card_refund' / 'crypto'
    explicit_external_type: bool = False    # the broker EXPLICITLY typed it as an external/outgoing withdrawal
    # Hard overrides (positive evidence it is NOT an external withdrawal; these win over any external signal above):
    internal_transfer: bool = False         # evidence the money stayed INSIDE the broker (between the user's accounts)
    deposit: bool = False                   # evidence money ENTERED the broker

    def has_positive_external_evidence(self) -> bool:
        return bool(self.external_destination or self.external_payment_rail or self.explicit_external_type)


def is_external_withdrawal(signals: ExternalWithdrawalSignals, *, broker: str = "",
                           certified_brokers: frozenset = CERTIFIED_BROKERS) -> bool:
    """True ONLY when: the broker's positive classifier is CERTIFIED, there is positive external evidence, and there
    is NO overriding internal-transfer/deposit evidence. Fail-closed in every other case. Pure."""
    # INVARIANTS first — independent of certification, they can never be overridden:
    if signals.internal_transfer:
        return False                         # internal transfer is NEVER external
    if signals.deposit:
        return False                         # a deposit is NEVER a withdrawal
    if not signals.has_positive_external_evidence():
        return False                         # ambiguous / no positive signal → UNKNOWN, never guessed
    # Only now does certification gate a POSITIVE result (no speculative positive classification in production):
    return (broker or "").strip() in certified_brokers


def classify_category(signals: ExternalWithdrawalSignals, *, broker: str = "",
                      certified_brokers: frozenset = CERTIFIED_BROKERS) -> str:
    """The TransactionCategory this classifier is willing to assert: EXTERNAL_WITHDRAWAL only on certified positive
    evidence, otherwise UNKNOWN (it never asserts DEPOSIT/INTERNAL_TRANSFER — that is the per-broker parser's job)."""
    if is_external_withdrawal(signals, broker=broker, certified_brokers=certified_brokers):
        return TransactionCategory.EXTERNAL_WITHDRAWAL
    return TransactionCategory.UNKNOWN
