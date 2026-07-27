"""Phase 3 (P3-E) — demo/live classification consistency (threat T7).

A single, shared CONFIG-level check used by every customer-facing account write path (the
`TradingAccountSerializer` and the `AddAccountWithMt5LoginView` create endpoint), so a demo/live
mismatch cannot slip through one path while the other is guarded. Broker-TRUTH verification
(``account_info().trade_mode``) stays at execution (the bridge exact-binding gate); this is only the
configuration cross-check that does not need a broker connection.
"""
from .models import BrokerServer


def classification_error(is_demo, broker_server):
    """Return a fail-closed error string if the account's demo/live flag is inconsistent with the
    configured ``BrokerServer`` environment, else ``None``.

    - No ``BrokerServer`` (free-text ``broker_name``) → ``None`` (nothing to cross-check against).
    - Unrecognised environment (not demo/live) → error (fail closed; never silently treat as live).
    - ``is_demo`` must equal ``environment == demo``, else error.
    """
    if broker_server is None:
        return None
    env = getattr(broker_server, "environment", None)
    if env not in (BrokerServer.DEMO, BrokerServer.LIVE):
        return (f"Broker server has an unrecognised environment '{env}'; cannot classify the account. "
                f"Fix the broker server before adding an account.")
    server_is_demo = (env == BrokerServer.DEMO)
    if bool(is_demo) != server_is_demo:
        return (f"Classification mismatch: is_demo={bool(is_demo)} but broker server environment is "
                f"'{env}'. A demo account must use a demo server and a live account a live server.")
    return None
