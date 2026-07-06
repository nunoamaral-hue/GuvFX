"""Signal-intake Django signals.

``signal_acquired`` is fired once per NEWLY-acquired message at the end of
``acquire_message`` (never on a dedup/catch-up replay). It is the one-way, decoupled
hook the downstream ``execution.auto_router`` connects to — ``signal_intake`` defines and
fires the signal but NEVER imports ``execution`` (the boundary stays one-directional).

Providers to the receiver (kwargs): ``provider`` (SignalProvider), ``acquired``
(AcquiredMessage), ``approval`` (PendingSignalApproval | None), ``outcome`` (str). It is
sent with ``send_robust`` so a receiver error can NEVER break acquisition (fail-open on the
acquisition side; the receiver itself is fail-closed).
"""
from django.dispatch import Signal

signal_acquired = Signal()
