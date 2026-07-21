"""MetaTrader 5 broker-validation provider — the first concrete integration of the abstraction.

Consumes the ``BrokerValidator`` seam like any other provider (not a special case). ``validate_account_record``
is BROKER-INDEPENDENT: it checks that the stored MT5 record is well-formed and complete (a numeric login
and a non-empty server), performing NO broker connectivity. Live MT5 login verification is the later
broker-login stage and will extend this provider then.
"""
from .base import BrokerValidationResult
from .registry import register_broker_validator


class Mt5BrokerValidator:
    key = "mt5"

    def validate_account_record(self, account) -> BrokerValidationResult:
        # MT5 login is the account number: a positive integer. Reject anything non-numeric fail-closed.
        login = str(getattr(account, "account_number", "") or "").strip()
        if not login.isdigit() or int(login) <= 0:
            return BrokerValidationResult(False, "invalid_login",
                                          detail="MT5 login must be a positive integer")
        # Server: prefer the normalised BrokerServer.server_name (the true MT5 server string); fall back
        # to free-text broker_name only to confirm the record is not empty. A record with neither is
        # incomplete and cannot be provisioned.
        if getattr(account, "broker_server_id", None):
            server = (account.broker_server.server_name or "").strip()
        else:
            server = (getattr(account, "broker_name", "") or "").strip()
        if not server:
            return BrokerValidationResult(False, "missing_server",
                                          detail="broker server / broker name is required")
        return BrokerValidationResult(True, "ok", normalized_login=login, normalized_server=server)


register_broker_validator(Mt5BrokerValidator())
