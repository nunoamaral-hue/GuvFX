"""ADR-0034 Execution Engine — server-derived per-job identity pin for Hosted Workspace (Provider B).

The MT5 bridge already enforces a mandatory per-job identity pin immediately before every mutation
(``verify_execution_binding`` / ``verify_mutation_identity`` in ``scripts/mt5_signal_bridge.py``): when a job
payload carries ``require_identity_pin=True`` it re-reads live ``account_info()``/``terminal_info()`` and
refuses the order unless the live login/server/demo-classification match the payload's expected values — and
it never falls back to the process-global ``MT5_EXPECTED_LOGIN`` env pin when the per-job pin is required.

What was missing (ADR-0034 Execution Engine gap G3) is the BACKEND populating that pin for the hosted path.
``identity_pin_for`` derives it SERVER-SIDE from the account's durable bindings — never from the client, the
bridge, or the workspace's self-report — so a Hosted Workspace job is bound to exactly the broker identity
GuvFX intends, regardless of which of the two worker consumers executes it.

DARK + fail-closed:
- Returns ``{}`` for a NON-Provider-B account, or while the master flag is OFF → the legacy env-pin path is
  byte-for-byte unchanged (Provider A / Customer Zero see no payload change).
- For a Provider-B account returns ``require_identity_pin=True`` + ``expected_login`` + ``expected_server`` +
  ``is_demo``. If the durable binding is missing a login/server, the pin is STILL required with an empty
  expected value → the bridge fails closed (refuses), never silently trades an unpinned hosted order.

Contains NO credential — ``expected_login``/``expected_server`` are broker IDENTIFIERS (the account number
and the broker server name), exactly as the bridge's own pin fields already are.
"""
from __future__ import annotations


def _provider_b_pin_enabled() -> bool:
    """DARK master gate (import-local, fail-closed). While OFF, no account gets a per-job pin injected."""
    try:
        from hosted_workspace.flags import hosted_persistent_mt5_enabled
        return hosted_persistent_mt5_enabled()
    except Exception:  # noqa: BLE001 — absence of the subsystem means it is disabled
        return False


def pin_subsystem_enabled() -> bool:
    """Public, cheap flag check (no account access) — the DARK master gate. Callers use this to short-circuit
    BEFORE dereferencing ``job.account``, so a dark subsystem adds zero queries/overhead."""
    return _provider_b_pin_enabled()


def is_hosted_workspace_account(account) -> bool:
    """True iff this account executes via the Hosted Workspace (Provider B) path AND the subsystem is on."""
    if account is None or not _provider_b_pin_enabled():
        return False
    from execution.readiness import PERSISTENT_WORKSPACE
    return str(getattr(account, "readiness_provider", "") or "") == PERSISTENT_WORKSPACE


def identity_pin_for(account) -> dict:
    """Return the per-job identity-pin payload fragment for ``account`` (merge into the job payload).

    ``{}`` for a non-Provider-B account or a dark subsystem (legacy env-pin path unchanged). For a Provider-B
    account, the mandatory pin the bridge enforces: ``require_identity_pin`` + server-derived
    ``expected_login``/``expected_server`` + ``is_demo``. Fail-closed — a missing binding value yields an
    empty expected with the pin STILL required, so the bridge refuses rather than trading unpinned.
    """
    if not is_hosted_workspace_account(account):
        return {}
    login = str(getattr(account, "account_number", "") or "").strip()
    server = ""
    if getattr(account, "broker_server_id", None):
        server = str(getattr(getattr(account, "broker_server", None), "server_name", "") or "").strip()
    return {
        "require_identity_pin": True,
        "expected_login": login,       # broker login (account number) — an identifier, never a secret
        "expected_server": server,     # broker server name — an identifier, never a secret
        "is_demo": bool(getattr(account, "is_demo", False)),
    }


def inject_identity_pin(job) -> bool:
    """Merge the server-derived per-job pin into ``job.payload`` IN PLACE — the single central injection
    called from ``ExecutionJob.save()`` so every mutation-creating seam (PLACE / OPEN / CLOSE / MODIFY)
    inherits it without per-seam edits. Idempotent: it never overwrites a value a caller explicitly set
    (``setdefault``). Returns whether a pin was injected.

    The flag is checked FIRST (cheap), so while the subsystem is dark this touches neither ``job.account``
    (no extra query) nor the payload — the legacy path is byte-for-byte unchanged.
    """
    if not _provider_b_pin_enabled():
        return False
    pin = identity_pin_for(getattr(job, "account", None))
    if not pin:
        return False
    payload = dict(getattr(job, "payload", None) or {})
    for key, value in pin.items():
        # The EXPECTED identity values (login/server/is_demo) respect an explicit caller-supplied value
        # (setdefault); but ``require_identity_pin`` is the safety-critical ENABLE flag — it is FORCED on for
        # a Provider-B mutation and can never be disabled by a payload, so a hosted order can never go
        # unpinned even if a future caller mistakenly set it False.
        if key == "require_identity_pin":
            payload[key] = True
        else:
            payload.setdefault(key, value)
    job.payload = payload
    return True
