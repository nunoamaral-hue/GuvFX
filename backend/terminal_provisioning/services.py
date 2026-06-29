"""
TX-1A / TX-1B — provisioning service (system of record + lifecycle controller).

Pure Django: computes the deterministic identity/runtime, persists the mapping,
emits auditable lifecycle events (reusing core.AuditEvent), and is idempotent.
Windows materialisation is performed separately by Provision-GuvfxAccount.ps1,
driven from the spec returned here.

Nothing here places trades, mutates execution/reliability, or touches the
legacy shared runtime.
"""
import secrets
import string

from django.db import IntegrityError, transaction
from django.utils import timezone

from core.models import AuditEvent
from trading.crypto import encrypt_password, decrypt_password

from .models import AccountProvisioning

# ── Deterministic conventions (TX1-R1 / TX1-R2) ──
RUNTIME_BASE = r"C:\GuvFX\accounts"
SUBDIRS = ["terminal", "profiles", "logs", "config", "audit"]


class ProvisioningError(Exception):
    """Controlled provisioning failure (duplicate / invalid mapping / bad input)."""


def username_for(account_id: int) -> str:
    return f"guvfx_u_{account_id}"


def runtime_root_for(account_id: int) -> str:
    return rf"{RUNTIME_BASE}\{account_id}"


def runtime_structure_for(account_id: int) -> dict:
    root = runtime_root_for(account_id)
    return {sub: rf"{root}\{sub}" for sub in SUBDIRS}


def generate_password(length: int = 20) -> str:
    """Strong, Windows-complexity-satisfying password using stdin-safe chars."""
    specials = "!@#$%^&*-_=+"
    alphabet = string.ascii_letters + string.digits + specials
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in specials for c in pw)):
            return pw


def _audit(event_type, account, prov=None, actor=None, severity=AuditEvent.Severity.INFO, **extra):
    """Append-only audit (reuses core.AuditEvent). Never logs secrets."""
    meta = {
        "account_id": account.id,
        "windows_username": prov.windows_username if prov else username_for(account.id),
        "runtime_root": prov.runtime_root if prov else runtime_root_for(account.id),
        "status": prov.status if prov else None,
        "is_admin": prov.is_admin if prov else False,
    }
    meta.update(extra)
    try:
        AuditEvent.objects.create(
            event_type=event_type,
            severity=severity,
            entity_type="terminal_provisioning",
            entity_id=str(account.id),
            user=actor if (actor and getattr(actor, "is_authenticated", False)) else None,
            metadata=meta,
        )
    except Exception:  # fail-open: audit must never block provisioning
        pass


def build_spec(prov: AccountProvisioning) -> dict:
    """Materialisation spec for the Windows executor. Includes the decrypted
    password — callers MUST pass it to the host over a secure channel and must
    never log/echo it."""
    return {
        "account_id": prov.trading_account_id,
        "windows_username": prov.windows_username,
        "password": decrypt_password(prov.password_enc) if prov.password_enc else "",
        "runtime_root": prov.runtime_root,
        "subdirs": list(SUBDIRS),
        "is_admin": prov.is_admin,
    }


@transaction.atomic
def provision(account, actor=None) -> AccountProvisioning:
    """Idempotent: ensure the isolation record + audit exist for an account.

    Re-running for the same account returns the existing record unchanged
    (no duplicate rows, no corruption). A pre-existing record whose mapping
    diverges from the deterministic convention is a controlled failure.
    """
    if account is None or account.id is None:
        raise ProvisioningError("account is required")

    want_user = username_for(account.id)
    want_root = runtime_root_for(account.id)

    existing = (AccountProvisioning.objects
                .select_for_update()
                .filter(trading_account=account)
                .first())
    if existing is not None:
        # Idempotent re-run — verify mapping integrity (TX1-R3).
        if existing.windows_username != want_user or existing.runtime_root != want_root:
            raise ProvisioningError(
                f"invalid mapping for account {account.id}: "
                f"have ({existing.windows_username}, {existing.runtime_root}), "
                f"expected ({want_user}, {want_root})"
            )
        return existing

    try:
        prov = AccountProvisioning.objects.create(
            trading_account=account,
            windows_username=want_user,
            password_enc=encrypt_password(generate_password()),
            is_admin=False,
            runtime_root=want_root,
            runtime_structure=runtime_structure_for(account.id),
            status=AccountProvisioning.Status.PENDING,
        )
    except IntegrityError as e:
        # Duplicate username/runtime owned by a different account → controlled failure.
        raise ProvisioningError(f"duplicate identity/runtime for account {account.id}: {e}")

    _audit(AuditEvent.EventType.TX_IDENTITY_CREATED, account, prov, actor)
    _audit(AuditEvent.EventType.TX_RUNTIME_CREATED, account, prov, actor)
    _audit(AuditEvent.EventType.TX_RUNTIME_BOUND, account, prov, actor)  # mapping created
    return prov


@transaction.atomic
def mark_materialized(account, *, identity=None, runtime=None, actor=None) -> AccountProvisioning:
    """Record that the Windows host now reflects the identity/runtime. When both
    are materialised the profile transitions to PROVISIONED."""
    prov = AccountProvisioning.objects.select_for_update().get(trading_account=account)
    changed = False
    if identity is not None and prov.identity_materialized != identity:
        prov.identity_materialized = identity
        changed = True
    if runtime is not None and prov.runtime_materialized != runtime:
        prov.runtime_materialized = runtime
        changed = True
    if (prov.identity_materialized and prov.runtime_materialized
            and prov.status == AccountProvisioning.Status.PENDING):
        prov.status = AccountProvisioning.Status.PROVISIONED
        prov.provisioned_at = timezone.now()
    if changed:
        prov.save()
    return prov


def _transition(account, new_status, event_type, ts_field, actor=None):
    prov = AccountProvisioning.objects.select_for_update().get(trading_account=account)
    prov.status = new_status
    setattr(prov, ts_field, timezone.now())
    prov.save(update_fields=["status", ts_field, "updated_at"])
    _audit(event_type, account, prov, actor)
    return prov


@transaction.atomic
def disable(account, actor=None):
    return _transition(account, AccountProvisioning.Status.DISABLED,
                       AuditEvent.EventType.TX_IDENTITY_DISABLED, "disabled_at", actor)


@transaction.atomic
def enable(account, actor=None):
    prov = AccountProvisioning.objects.select_for_update().get(trading_account=account)
    prov.status = AccountProvisioning.Status.PROVISIONED
    prov.disabled_at = None
    prov.save(update_fields=["status", "disabled_at", "updated_at"])
    _audit(AuditEvent.EventType.TX_IDENTITY_ENABLED, account, prov, actor)
    return prov


@transaction.atomic
def retire(account, actor=None):
    return _transition(account, AccountProvisioning.Status.RETIRED,
                       AuditEvent.EventType.TX_IDENTITY_RETIRED, "retired_at", actor)
