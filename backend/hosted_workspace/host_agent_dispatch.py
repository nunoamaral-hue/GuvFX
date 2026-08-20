"""hosted_workspace.host_agent_dispatch — Beta Readiness Stream 5: the HOST-SIDE narrow provisioning dispatcher.

This runs on the Windows host (deployed as a supported service — RULE 1). It is the ONLY thing that turns a
signed request into a host action, and it is deliberately incapable of arbitrary execution:

  1. verify the signed request (auth + integrity + replay + time-bound)                → host_protocol
  2. refuse Customer Zero / reserved identities                                          → fail closed
  3. DERIVE the Windows identity and every path from ``account_id`` SERVER-SIDE          → no caller path
  4. reject any request params that are not an explicit per-op scalar allow-list         → no smuggling
  5. map the allow-listed ``operation`` to EXACTLY ONE reviewed primitive + derived args → no command/script
  6. run that primitive (the injected runner ParseFile-gates + executes the fixed .ps1)  → no free-form string
  7. sign the deterministic response                                                     → host_protocol

There is no path on this wire to a PowerShell string, a command line, an executable/filesystem path, a
username, or a task definition — the request cannot express any of them, and the dispatcher never reads a path
or identity from the caller. ``run_primitive`` / ``nonce_burn`` / ``envelope_open`` are injected so the whole
dispatcher is exercised in CI without a host.

Kept dependency-free (stdlib + host_protocol) so it can run on the host as a standalone service (RULE 3
corollary): it does not import Django/backend.
"""
from __future__ import annotations

import re

from hosted_workspace.host_protocol import (
    HostProtocolError, HOSTED_OPERATIONS, sign_hosted_response, verify_hosted_request,
)

ACCOUNTS_BASE = r"C:\GuvFX\accounts"
_USERNAME_FMT = "guvfx_u_{}"

# Customer Zero keeps its already-published legacy RemoteApp alias for compatibility; every other account gets a
# deterministic per-account alias. This is the SINGLE SOURCE OF TRUTH for the alias — used both host-side (to
# publish) and Django-side (the delivery descriptor imports it), so they can never drift. Server-derived only.
_LEGACY_REMOTEAPP_ACCOUNTS = frozenset({1})
_REMOTEAPP_ALIAS_RE = re.compile(r"^(terminal64|guvfx_mt5_[1-9][0-9]*)$")


def remoteapp_alias(account_id) -> str:
    """The deterministic, server-derived RemoteApp alias for an account. account #1 (Customer Zero) → the legacy
    ``terminal64`` (compat with its currently-published RemoteApp); every other account → ``guvfx_mt5_<id>``.
    Safe charset + bounded by construction; never caller-controlled."""
    acct = int(account_id)
    if acct <= 0:
        raise HostProtocolError("account_id_out_of_range")
    alias = "terminal64" if acct in _LEGACY_REMOTEAPP_ACCOUNTS else f"guvfx_mt5_{acct}"
    if not _REMOTEAPP_ALIAS_RE.match(alias):   # defence: the derivation can only ever produce a safe alias
        raise HostProtocolError("remoteapp_alias_malformed")
    return alias

# The reserved identities the host must NEVER provision — the SECOND enforcement layer (Django refuses too).
# Default protects Customer Zero (account #1 / guvfx_u_1 / C:\GuvFX\accounts\1). A garbled host override falls
# back SAFE to {1}. An explicit empty override ("") disables it (operator choice; used only in tests).
DEFAULT_RESERVED_ACCOUNT_IDS = frozenset({1})

# Each allow-listed operation maps to EXACTLY ONE reviewed primitive name. The host runner resolves the
# primitive name to its fixed, version-controlled .ps1 and passes ONLY the derived args below — never anything
# from the request. `params_allow` is the (currently empty) set of scalar param keys a primitive may accept.
OP_PRIMITIVES = {
    "PROVISION_IDENTITY":       {"primitive": "provision_identity",       "params_allow": ()},
    "APPLY_WORKSPACE_ACL":      {"primitive": "apply_workspace_acl",      "params_allow": ()},
    "ROLLBACK_WORKSPACE_ACL":   {"primitive": "rollback_workspace_acl",   "params_allow": ()},
    "MATERIALISE_RUNTIME":      {"primitive": "materialise_runtime",      "params_allow": ()},
    # P0 proactive LiveUpdate containment: ensure the tenant profile exists (CreateProfile — no session, no MT5
    # launch) then apply the certified Variant-A deny-write on the tenant's OWN roaming LiveUpdate staging.
    # Server-derived identity/paths only (username + accounts\<id>\terminal + account_id); Customer Zero refused.
    "APPLY_LIVEUPDATE_CONTAINMENT": {"primitive": "apply_liveupdate_containment", "params_allow": ()},
    "APPLY_AUTOTRADING_CONFIG": {"primitive": "apply_autotrading_config", "params_allow": ()},
    "ENSURE_RDP_MEMBERSHIP":    {"primitive": "ensure_rdp_membership",    "params_allow": ()},
    "ENSURE_SINGLE_SESSION":    {"primitive": "ensure_single_session",    "params_allow": ()},
    "ENSURE_REMOTEAPP":         {"primitive": "ensure_remoteapp",         "params_allow": ()},
    "REMOVE_REMOTEAPP":         {"primitive": "remove_remoteapp",         "params_allow": ()},
    "PREPARE_OBSERVER":         {"primitive": "prepare_observer",         "params_allow": ()},
    "OBSERVE_WORKSPACE":        {"primitive": "observe_workspace",        "params_allow": ()},
    "APPLY_APPLOCKER_AUDIT":    {"primitive": "applocker_tenant_merge",   "params_allow": ()},
    "REMOVE_APPLOCKER_TENANT":  {"primitive": "applocker_tenant_remove",  "params_allow": ()},
    "VERIFY_SLOT":              {"primitive": "verify_slot",              "params_allow": ()},
    # FINAL Closed-Beta stream: activate this node's dedicated pin-enforcing order bridge. Server-derived
    # terminal_root (falls through to _build_args' base) + injected -AccountId; no caller params.
    "ACTIVATE_ORDER_BRIDGE":    {"primitive": "activate_order_bridge",    "params_allow": ()},
    # P0-B1.1 multi-tenant: start THIS tenant's OWN pin-enforcing bridge on its per-tenant PORT. The port is the
    # ONLY caller-influenced value (server-derived on the backend from HostedExecutionEndpoint, bound in the
    # signature via params_digest, range-validated below); everything else (slot/terminal/account) is derived.
    "ACTIVATE_TENANT_BRIDGE":   {"primitive": "activate_tenant_bridge",   "params_allow": ("port",)},
    # AJ#6.3: graceful in-session close+relaunch of THIS tenant's OWN MT5 (post-login AutoTrading capability
    # recovery). Server-derived identity/paths only; Customer Zero is refused (reserved) before this maps. It
    # relaunches a terminal — it NEVER logs in, changes accounts, arms a strategy, or places an order.
    "RELAUNCH_TERMINAL":        {"primitive": "relaunch_terminal",        "params_allow": ()},
}
assert set(OP_PRIMITIVES) == set(HOSTED_OPERATIONS), "OP_PRIMITIVES must cover exactly HOSTED_OPERATIONS"

_SCALAR = (str, int, bool)


def reserved_ids_from(raw) -> frozenset:
    """Parse a host reserved-id override, failing SAFE. None/garbled → {1} (protect Customer Zero); an explicit
    empty string → empty (operator opt-out, tests only); a list of ints → that set (always keeps at least the
    parsed ints, never silently widening access)."""
    if raw is None:
        return DEFAULT_RESERVED_ACCOUNT_IDS
    s = str(raw).strip()
    if s == "":
        return frozenset()
    out = set()
    for tok in s.replace(",", " ").split():
        try:
            out.add(int(tok))
        except ValueError:
            continue
    # Customer Zero (account #1) is a HARD FLOOR: any non-empty override ADDS to {1}, it never removes it. So a
    # typo like "1o 2" -> {1,2} still protects CZ, and a fully garbled value -> {1}. Only an explicit empty
    # string (handled above) disables the guard entirely.
    return frozenset(out | DEFAULT_RESERVED_ACCOUNT_IDS)


def derive_slot(account_id: int) -> dict:
    """Derive the fixed slot identity + paths from ``account_id`` — the ONLY source of these values (never the
    caller). Refuses a non-positive id."""
    acct = int(account_id)
    if acct <= 0:
        raise HostProtocolError("account_id_out_of_range")
    username = _USERNAME_FMT.format(acct)
    runtime_root = rf"{ACCOUNTS_BASE}\{acct}"
    return {
        "account_id": acct,
        "username": username,
        "runtime_root": runtime_root,
        "terminal_root": rf"{runtime_root}\terminal",
        "acl_snapshot_path": rf"{runtime_root}\audit\acl_snapshot.sddl",
        "remoteapp_alias": remoteapp_alias(acct),
    }


def _validate_params(op: str, params: dict) -> dict:
    allow = OP_PRIMITIVES[op]["params_allow"]
    if not isinstance(params, dict):
        raise HostProtocolError("params_malformed")
    for k, v in params.items():
        if k not in allow:
            raise HostProtocolError("params_not_allowed")          # no smuggling of extra keys
        if not isinstance(v, _SCALAR):                             # only str/int/bool — no dict/list/bytes path
            raise HostProtocolError("params_not_scalar")
    return dict(params)


def dispatch(request: dict, *, keyring: dict, now: int, nonce_burn, run_primitive,
             reserved_ids=None, envelope_open=None, max_skew_seconds=None) -> dict:
    """Verify → confine → map → run → sign. Returns a signed response dict. Raises ``HostProtocolError`` on any
    validation/confinement failure (the caller returns it as a sanitised failure — never a secret, never a
    traceback). ``run_primitive(primitive_name, args) -> {"ok": bool, ...}`` is the injected host runner.
    ``max_skew_seconds`` (Stream 7C) is threaded to ``verify_hosted_request`` so an operator's configured skew
    is actually enforced; ``None`` (the default) preserves the protocol default — no behaviour change for
    existing callers."""
    skew_kw = {} if max_skew_seconds is None else {"max_skew_seconds": int(max_skew_seconds)}
    fields = verify_hosted_request(request, keyring=keyring, now=now, nonce_burn=nonce_burn, **skew_kw)
    op = fields["operation"]
    account_id = fields["account_id"]
    correlation_id = fields["correlation_id"]
    nonce = fields["nonce"]

    reserved = reserved_ids_from(reserved_ids) if not isinstance(reserved_ids, frozenset) else reserved_ids
    if account_id in reserved:
        raise HostProtocolError("reserved_identity")               # Customer Zero refused, host-side layer

    slot = derive_slot(account_id)
    _validate_params(op, fields.get("params") or {})

    args = _build_args(op, slot, fields, envelope_open=envelope_open)
    primitive = OP_PRIMITIVES[op]["primitive"]
    result = run_primitive(primitive, args)
    if not isinstance(result, dict):
        raise HostProtocolError("primitive_bad_result")

    # A signed response binds the (possibly security-relevant, e.g. ACL read-back) result to this request.
    return sign_hosted_response(result=_sanitise_result(result), correlation_id=correlation_id, nonce=nonce,
                                keyring=keyring, key_id=fields["key_id"])


def _build_args(op: str, slot: dict, fields: dict, *, envelope_open) -> dict:
    """Assemble the fixed, server-derived args for the mapped primitive. The ONLY caller-influenced value is the
    sealed password for PROVISION_IDENTITY, which is OPENED here (never logged) — everything else is derived."""
    base = {"username": slot["username"], "runtime_root": slot["runtime_root"],
            "terminal_root": slot["terminal_root"]}
    if op == "PROVISION_IDENTITY":
        if envelope_open is None:
            raise HostProtocolError("envelope_opener_unavailable")
        password = envelope_open(fields.get("payload"), account_id=slot["account_id"],
                                 correlation_id=fields["correlation_id"], nonce=fields["nonce"])
        if not isinstance(password, (str, bytes)) or not password:
            raise HostProtocolError("password_unavailable")
        return {**base, "password": password}                      # password used, never returned/logged
    if op in ("APPLY_WORKSPACE_ACL", "ROLLBACK_WORKSPACE_ACL"):
        return {"username": slot["username"], "runtime_root": slot["runtime_root"],
                "snapshot_path": slot["acl_snapshot_path"],
                "mode": "Apply" if op == "APPLY_WORKSPACE_ACL" else "Rollback"}
    if op == "APPLY_AUTOTRADING_CONFIG":
        return {"terminal_root": slot["terminal_root"]}
    if op == "ACTIVATE_TENANT_BRIDGE":
        # The per-tenant bridge PORT is the sole caller-influenced value (signed + params_allow-validated).
        # Re-validate its RANGE here (defence in depth): must be a per-tenant port, never a reserved GuvFX port
        # (8787 backtest / 8788 CZ / 8789 legacy node / 8791 validation). Everything else is server-derived from
        # the account slot, and the host script re-asserts the CZ refusal.
        try:
            port = int((fields.get("params") or {})["port"])
        except (KeyError, TypeError, ValueError):
            raise HostProtocolError("params_malformed")
        if not (8800 <= port <= 8899):
            raise HostProtocolError("params_not_allowed")
        return {"username": slot["username"], "terminal_root": slot["terminal_root"],
                "account_id": slot["account_id"], "port": port}
    if op == "APPLY_LIVEUPDATE_CONTAINMENT":
        # The host script confines to guvfx_u_<id> + accounts\<id>\terminal, refuses Customer Zero, ensures the
        # tenant profile exists (CreateProfile) and deny-writes ONLY that tenant's roaming LiveUpdate staging.
        # account_id is passed so the .ps1 re-asserts the CZ refusal + validates the identity as defence in depth.
        return {"username": slot["username"], "terminal_root": slot["terminal_root"],
                "account_id": slot["account_id"]}
    if op == "RELAUNCH_TERMINAL":
        # The host script confines to guvfx_u_<id> + accounts\<id>\terminal and closes/relaunches ONLY the
        # tenant's own terminal64; account_id is passed so the .ps1 can derive its per-account task names +
        # re-assert the CZ refusal as defence in depth.
        return {"username": slot["username"], "terminal_root": slot["terminal_root"],
                "account_id": slot["account_id"]}
    if op in ("ENSURE_REMOTEAPP", "REMOVE_REMOTEAPP"):
        # The alias is DERIVED server-side (never the caller's) — per-account for isolation, legacy for CZ.
        return {"username": slot["username"], "terminal_root": slot["terminal_root"],
                "alias": slot["remoteapp_alias"], "account_id": slot["account_id"]}
    if op in ("APPLY_APPLOCKER_AUDIT", "REMOVE_APPLOCKER_TENANT"):
        # Tenant-scoped AppLocker: the host resolves the SID from the username and tags rules with account_id, so
        # a merge/remove touches ONLY this account's contribution (never the base or another tenant / CZ).
        return {"username": slot["username"], "account_id": slot["account_id"]}
    if op == "ENSURE_RDP_MEMBERSHIP":
        return {"username": slot["username"]}
    if op == "ENSURE_SINGLE_SESSION":
        return {}
    return base                                     # MATERIALISE / PREPARE_OBSERVER / OBSERVE_WORKSPACE / VERIFY_SLOT


_SECRET_KEYS = {"password", "pw", "secret", "payload"}


def _sanitise_result(result: dict) -> dict:
    """Never let a primitive's result echo a secret back over the wire (defence in depth)."""
    return {k: v for k, v in result.items() if k.lower() not in _SECRET_KEYS}


# A conservative guard the real host runner uses before executing any mapped primitive (belt for RULE 9/no-RCE):
_PRIMITIVE_NAME_RE = re.compile(r"^[a-z_]+$")


def is_known_primitive(primitive: str) -> bool:
    return bool(_PRIMITIVE_NAME_RE.match(str(primitive or ""))) and \
        primitive in {v["primitive"] for v in OP_PRIMITIVES.values()}
