"""trade_copier.live_host_actuators — ADR-0117 PRODUCTION adapters for the injected lifecycle-arming / teardown
actuator interfaces, backed by the REAL DARK-gated signed host executor.

WHY THIS EXISTS
---------------
`arm_copier_live_lifecycle` passed `emitter_enabler=None`, so `lifecycle_arming.arm_live_lifecycle` always
returned `_teardown("emitter_enabler_unavailable")` on `--commit` — the governed arm could never complete, and no
production code supplied a signed-host emitter actuator (only test fakes). This module is the missing production
wire-up: factory functions that return the four injected callables
(`admission_provisioner` / `admission_revoker` / `emitter_enabler` / `emitter_disabler` / `emitter_key_remover`),
each resolving `hosted_workspace.host_executor.resolve_signed_host_executor` and verifying the signed attestation.

DARK + FAIL-CLOSED
------------------
`resolve_signed_host_executor` returns `None` unless `HOSTED_HOST_EXECUTOR_ENABLED` is on AND the
keyring/key_id/base_url are configured. So in the repository-only / DARK phase the executor is absent and every
adapter fails closed with the TRUTHFUL reason `host_executor_unavailable`: a real `--commit` arm now fails at the
emitter step (not "emitter_enabler is None") and rolls the whole stack back to DARK — never a partial arm. The
risk-INCREASING emitter ENABLE is additionally gated by `COPIER_EMITTER_STANDING_ENABLED`; the risk-REDUCING
DISABLE / key-remove / admission-restore need no flag (safe to run any time, mirroring `restore_off`).

SCOPE / SECURITY
----------------
The standing-emitter actuators are PINNED to account #46 (the executor method re-pins it before any host contact);
admission provision/revoke act on follower #48 via the ADR-0049 integrator (`provision_live` / `restore_off`). No
secret is read or printed; the emitter key never rides this path (REMOVE shreds host-side and returns only a
sanitised verdict). The `executor_resolver` argument is a TEST-ONLY injection seam; production always passes None
and uses the real DARK factory (test fakes stay test-only — the factory is never faked in production code).
"""
from __future__ import annotations

from typing import Callable, Optional

# The sole master in the bounded #46->#48 demo. The signed executor method re-pins this before any host contact;
# this module is the backend policy edge that only ever asks for the #46 standing emitter.
STANDING_EMITTER_ACCOUNT_ID = 46


def _resolve_emitter_executor(executor_resolver: Optional[Callable]):
    """Resolve the #46-bound signed host executor (DARK -> None). ``executor_resolver`` (called with the int
    account id) is a TEST-ONLY seam; in production it is None and the real DARK factory is used."""
    if executor_resolver is not None:
        return executor_resolver(STANDING_EMITTER_ACCOUNT_ID)
    from hosted_workspace.host_executor import resolve_signed_host_executor
    return resolve_signed_host_executor(account_id=STANDING_EMITTER_ACCOUNT_ID)


def _verify_host_ok(result, *, expect_account: int, expect_op: str) -> tuple[bool, str]:
    """Semantic echo check on a signed host-op result. The signature was already verified by
    ``verify_hosted_response``; this confirms the host attested ``ok`` and (when present) echoed the expected
    account id + operation, so a truncated/duplicated/wrong-op response fails closed."""
    if not isinstance(result, dict):
        return (False, "no_result")
    if not result.get("ok"):
        return (False, str(result.get("reason", "host_not_ok")))
    try:
        if int(result.get("account_id", expect_account)) != int(expect_account):
            return (False, "account_mismatch")
    except (TypeError, ValueError):
        return (False, "malformed_attestation")   # a non-int field from a (compromised) host -> fail closed
    if str(result.get("operation", expect_op) or expect_op) != expect_op:
        return (False, "operation_mismatch")
    return (True, "ok")


# ── emitter actuators (ADR-0117 signed ops, #46-pinned) ─────────────────────────────────────────────────────
def make_emitter_enabler(*, executor_resolver: Optional[Callable] = None) -> Callable[[], dict]:
    """Return the arm's ``emitter_enabler`` adapter: enable ``GuvFX_CopierEmitter_46`` via the signed executor and
    verify the attestation. Risk-INCREASING, so it is gated by BOTH ``COPIER_EMITTER_STANDING_ENABLED`` and the
    DARK executor factory; either absent => fail closed (the arm then rolls the whole stack back, never a partial
    arm). On an unverified attestation it issues a best-effort compensating DISABLE so the task is never left
    enabled on a failed verification. Returns ``{"ok", "enabled", ...}``."""
    def _enable() -> dict:
        from trade_copier.flags import copier_emitter_standing_enabled
        if not copier_emitter_standing_enabled():
            return {"ok": False, "enabled": False, "reason": "copier_emitter_standing_disabled"}
        executor = _resolve_emitter_executor(executor_resolver)
        if executor is None:
            return {"ok": False, "enabled": False, "reason": "host_executor_unavailable"}
        result = executor.enable_copier_emitter()
        ok, reason = _verify_host_ok(result, expect_account=STANDING_EMITTER_ACCOUNT_ID,
                                     expect_op="ENABLE_COPIER_EMITTER")
        if not ok:
            # Never leave the emitter enabled on a failed/ambiguous verification. Best-effort; the arm rolls the
            # whole stack back to DARK regardless (and teardown's disabler is idempotent).
            try:
                executor.disable_copier_emitter()
            except Exception:  # noqa: BLE001 — compensating disable is best-effort
                pass
            return {"ok": False, "enabled": False, "reason": f"enable_unverified:{reason}", "attestation": result}
        return {"ok": True, "enabled": True, "attestation": result}
    return _enable


def make_emitter_disabler(*, executor_resolver: Optional[Callable] = None) -> Callable[[], dict]:
    """Return the teardown's ``emitter_disabler`` adapter (re-disable ``GuvFX_CopierEmitter_46``). Risk-REDUCING +
    idempotent => NOT flag-gated (safe any time, like ``restore_off``); only the DARK executor is required. Returns
    ``{"ok", "disabled", ...}``."""
    def _disable() -> dict:
        executor = _resolve_emitter_executor(executor_resolver)
        if executor is None:
            return {"ok": False, "disabled": False, "reason": "host_executor_unavailable"}
        result = executor.disable_copier_emitter()
        ok, reason = _verify_host_ok(result, expect_account=STANDING_EMITTER_ACCOUNT_ID,
                                     expect_op="DISABLE_COPIER_EMITTER")
        return {"ok": ok, "disabled": ok, "reason": (None if ok else reason), "attestation": result}
    return _disable


def make_emitter_key_remover(*, executor_resolver: Optional[Callable] = None) -> Callable[[], dict]:
    """Return the teardown's ``emitter_key_remover`` adapter (signed ``REMOVE_COPIER_EMITTER_KEY`` -> the reviewed
    provisioner ``-Remove``: shred + delete the host-local ``...\\46\\ingest.key``). Risk-REDUCING + idempotent =>
    NOT flag-gated. No key material rides this path. Returns ``{"ok", "removed", ...}``."""
    def _remove() -> dict:
        executor = _resolve_emitter_executor(executor_resolver)
        if executor is None:
            return {"ok": False, "removed": False, "reason": "host_executor_unavailable"}
        result = executor.remove_copier_emitter_key()
        ok, reason = _verify_host_ok(result, expect_account=STANDING_EMITTER_ACCOUNT_ID,
                                     expect_op="REMOVE_COPIER_EMITTER_KEY")
        return {"ok": ok, "removed": ok, "reason": (None if ok else reason), "attestation": result}
    return _remove


def read_emitter_host_state(*, executor_resolver: Optional[Callable] = None) -> dict:
    """VALUE-BLIND read-back of the #46 standing-emitter host posture (task enabled/disabled + ``ingest.key``
    presence/ACL — metadata ONLY, never the key or any key-derived value). DARK executor => fail closed. Used by
    the arm read-back and operator preflight; never gates on or returns key material."""
    executor = _resolve_emitter_executor(executor_resolver)
    if executor is None:
        return {"ok": False, "reason": "host_executor_unavailable"}
    result = executor.read_copier_host_state()
    ok, reason = _verify_host_ok(result, expect_account=STANDING_EMITTER_ACCOUNT_ID,
                                 expect_op="READ_COPIER_HOST_STATE")
    if not ok:
        return {"ok": False, "reason": reason, "attestation": result}
    return {"ok": True, "attestation": result,
            # value-blind host-attested facts; never a key or key-derived value
            "emitter_task_enabled": bool(result.get("emitter_task_enabled")),
            "emitter_task_disabled": bool(result.get("emitter_task_disabled")),
            "ingest_key_present": bool(result.get("ingest_key_present")),
            "ingest_key_acl_ok": bool(result.get("ingest_key_acl_ok"))}


# ── admission adapters (reuse the ADR-0049 signed integrator) ───────────────────────────────────────────────
def make_admission_provisioner(follower_account_id: int, *,
                               executor_resolver: Optional[Callable] = None) -> Callable:
    """Return the arm's ``admission_provisioner(authz)`` adapter: provision follower #48 host admission
    (``MT5_ALLOW_LIVE``) via the signed ADR-0049 integrator, bound to the exact authorization. DARK executor =>
    ``provision_live`` returns ``host_executor_unavailable`` (fail closed). Returns ``{"ok", "armed", ...}``."""
    def _provision(authz) -> dict:
        from trade_copier.live_bridge_provision import provision_live
        return provision_live(int(follower_account_id), int(authz.id), commit=True,
                              executor_resolver=executor_resolver)
    return _provision


def make_admission_revoker(follower_account_id: int, *,
                           executor_resolver: Optional[Callable] = None) -> Callable[[], dict]:
    """Return the teardown's ``admission_restore`` adapter: signed ``RESTORE_TENANT_BRIDGE_EXECUTION_OFF`` for
    follower #48. Idempotent + needs no flags/authorization (safe any time). Returns ``{"ok", ...}``."""
    def _restore() -> dict:
        from trade_copier.live_bridge_provision import restore_off
        return restore_off(int(follower_account_id), commit=True, executor_resolver=executor_resolver)
    return _restore
