"""ADR-0021 — the ONE canonical TradingAccount creation contract.

Both customer-facing entry points delegate here so there is exactly one creation behaviour:
  * ``TradingAccountViewSet.perform_create``           (``POST /api/trading/accounts/``)
  * ``AddAccountWithMt5LoginView``  (``POST /api/trading/accounts/add-with-mt5-login/`` — Accounts-page UI)

Behaviour (dedicated-runtime model):
  * Account creation records CUSTOMER INTENT ONLY. No legacy shared-instance binding — ``mt5_instance``
    is ``None``; the account's OWN runtime is provisioned asynchronously via
    ``_maybe_enqueue_beta_provisioning`` (idempotent, gated by ``BETA_RUNTIMES_ENABLED``).
  * Broker-login validation is DEFERRED to the runtime-provisioning stage (behind
    ``PROVISIONING_REQUIRE_BROKER_LOGIN``). This service performs NO immediate broker login and requires
    NO shared MT5 instance, so a customer with no leased instance is never ``409``'d here.
  * Idempotent at both layers WITHOUT depending on a row lock for correctness (fast-path existing-account
    lookup + DB partial-unique winner recovery); the per-user account CAP is enforced atomically under a
    row lock scoped solely to cap atomicity.
  * Password encryption, customer-credential intake audit, and demo/live classification are all preserved
    because creation goes through ``TradingAccountSerializer`` (never a bare ORM create).
  * Staff keep the unchanged admin create (no dedicated-runtime provisioning).

The helpers ``_find_existing_account`` and ``_maybe_enqueue_beta_provisioning`` deliberately remain in
``trading.views`` (existing call sites + a test that mocks them); they are imported lazily here to avoid a
circular import at module load.

Returns ``(account, created)`` — ``created`` is ``False`` when an idempotent resubmission returned an
already-existing account.
"""
from __future__ import annotations


def create_customer_account(request, serializer):
    """Create (or idempotently return) a customer's TradingAccount via the single ADR-0021 contract.

    ``serializer`` must be a *validated* ``TradingAccountSerializer`` (``is_valid()`` already called) — its
    ``validated_data`` supplies the create fields and its ``.save()`` performs the password-encrypting,
    audited model create. On return, ``serializer.instance`` is the resulting account. Raises
    ``rest_framework.exceptions.ValidationError`` on a missing broker identity or a breached cap.
    """
    from django.db import IntegrityError, transaction
    from rest_framework.exceptions import ValidationError

    from billing.entitlements import resolve_entitlements
    from billing.models import UserSubscriptionState
    from trading.models import TradingAccount
    from trading.views import _find_existing_account, _maybe_enqueue_beta_provisioning

    user = request.user

    # Staff: unchanged legacy admin create (no dedicated-runtime provisioning).
    if user.is_staff:
        serializer.save(user=user)
        return serializer.instance, True

    # Canonical identity normalisation (mirrors the DB partial-unique constraints / CheckConstraint).
    acct_no = str((request.data or {}).get("account_number") or "").strip()
    broker_name = str((request.data or {}).get("broker_name") or "").strip()
    broker_server = serializer.validated_data.get("broker_server")

    # Friendly 400 — a customer account needs a usable broker identity (mirrors the DB CheckConstraint so
    # the customer gets a clear message instead of a 500 from a constraint violation).
    if broker_server is None and not broker_name:
        raise ValidationError({"broker": "Select a broker server or enter a broker name."})

    # Fast path (no lock): an identical prior submission returns the SAME account (idempotent).
    existing = _find_existing_account(user, acct_no, broker_server, broker_name)
    if existing is not None:
        serializer.instance = existing
        _maybe_enqueue_beta_provisioning(user, existing)   # idempotent re-drive of provisioning
        return existing, False

    # New account. Serialise THIS user's creates on their row so the per-user account CAP is enforced
    # ATOMICALLY (a non-atomic count() would let concurrent creates exceed the plan limit). Idempotency
    # does NOT depend on this lock — the DB partial-unique constraints + the IntegrityError winner recovery
    # below are the idempotency mechanism.
    created = True
    try:
        with transaction.atomic():
            type(user).objects.select_for_update().get(pk=user.pk)   # cap serialisation only
            locked = _find_existing_account(user, acct_no, broker_server, broker_name)
            if locked is not None:
                serializer.instance = locked   # a concurrent identical submission just won — reuse it
                created = False
            else:
                ent = resolve_entitlements(UserSubscriptionState.objects.filter(user=user).first())
                limit = min(10, ent.max_trading_accounts)
                if TradingAccount.objects.filter(user=user).count() >= limit:
                    raise ValidationError({"detail": f"Broker-account limit reached (maximum {limit})."})
                serializer.save(user=user, mt5_instance=None, is_active=False)
    except IntegrityError:
        # Winner recovery (belt-and-suspenders backstop to the DB unique constraint): a concurrent
        # identical submission created the row first — recover it, return idempotently, never a 500.
        winner = _find_existing_account(user, acct_no, broker_server, broker_name)
        if winner is None:
            raise   # a different integrity error (not the account-identity race) — surface it
        serializer.instance = winner
        _maybe_enqueue_beta_provisioning(user, winner)
        return winner, False

    _maybe_enqueue_beta_provisioning(user, serializer.instance)
    return serializer.instance, created
