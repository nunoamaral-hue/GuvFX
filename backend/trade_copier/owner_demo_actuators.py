"""trade_copier.owner_demo_actuators — the narrow, fail-closed PROVIDER that wires the owner-demo control's
``DemoActuators`` to the EXISTING production host-actuator factories (GFX-COPIER-OWNER-DEMO-EXECUTION-WIRING-171,
ADR-0128).

WHY THIS EXISTS
---------------
``owner_demo_control.confirm`` / ``stop`` previously passed ``actuators=None``, so execution could never proceed:
the orchestration layer had no production wiring. This module is that wiring — and NOTHING more. It reuses the
already-reviewed ``trade_copier.live_host_actuators`` factories (the signed ADR-0117 host adapters) and the
``DemoActuators`` injection contract unchanged; it adds NO new authority, NO new host channel and NO migration.

FAIL-CLOSED, DARK BY DEFAULT
----------------------------
* The risk-INCREASING arm actuators (``admission_provisioner`` / ``emitter_enabler``) are wired ONLY when the new
  default-OFF ``copier_owner_demo_execution_enabled()`` flag is on AND the signed host executor is actually
  available. Off by default ⇒ they are ``None`` ⇒ ``DemoActuators.provisioned()`` is False ⇒ confirm fails closed.
* The host-side ``verifier_window_opener`` is DELIBERATELY left ``None`` here: it is the one component that would
  need a standing-key transfer channel, which this repository-only wiring does NOT add. With it absent
  ``provisioned()`` is False, so confirm stays DARK in production even with the execution flag on — until a
  separate, Sponsor-gated host rollout provisions the opener (ADR-0128 §Production integration).
* ``host_admission_probe`` is also left ``None``: there is no value-blind #48 ``MT5_ALLOW_LIVE`` read-back yet (a
  documented REMAINING integration), and its absence likewise keeps ``provisioned()`` False.
* The risk-REDUCING teardown actuators (``emitter_disabler`` / ``emitter_key_remover`` / ``admission_restore``) are
  wired whenever the signed executor is available, INDEPENDENT of the execution flag, so a ``stop`` can still disarm
  the host after the flag is toggled off mid-session. When the executor is absent (the repository-only / DARK phase,
  and all tests) they are ``None`` and the teardown reports the host steps UNPROVEN — never a false "done".

No secret is read, held, printed or transferred: the factory callables obtain any key host-side and return only
sanitised verdicts; this module holds references only.
"""
from __future__ import annotations

from trade_copier.owner_demo_control import DemoActuators


def _signed_host_executor_available() -> bool:
    """True iff the DARK-gated signed host executor resolves (``HOSTED_HOST_EXECUTOR_ENABLED`` + keyring/base_url).
    Value-blind and side-effect-free: it resolves the adapter object only, never contacts the host. Any error ⇒
    unavailable (fail closed)."""
    try:
        from hosted_workspace.host_executor import resolve_signed_host_executor
        from trade_copier.live_host_actuators import STANDING_EMITTER_ACCOUNT_ID
        return resolve_signed_host_executor(account_id=STANDING_EMITTER_ACCOUNT_ID) is not None
    except Exception:  # noqa: BLE001 — any resolution error is fail-closed
        return False


def build_demo_actuators(rc, *, commit: bool) -> DemoActuators:
    """Build the production ``DemoActuators`` for the governed route ``rc``, fail-closed.

    ``commit`` is advisory (the arm/teardown callables are the same references either way; the arm decides when to
    invoke them). Returns a ``DemoActuators`` whose ``provisioned()`` is True ONLY if the execution flag is on, the
    signed host executor is available, AND the host-side verifier-window opener is provisioned — the last of which
    this repository-only wiring never provisions, so in every current environment ``provisioned()`` is False and
    confirm fails closed (DARK)."""
    from trade_copier import flags as cf
    from trade_copier import live_host_actuators as lha

    fid = int(rc.follower.id)
    exec_on = bool(cf.copier_owner_demo_execution_enabled())
    host_live = _signed_host_executor_available()

    def _emitter_key_probe() -> dict:
        try:
            st = lha.read_emitter_host_state()
        except Exception:  # noqa: BLE001 — a probe error is fail-closed (treated as not-ready)
            return {"ready": False, "identity_ok": False, "reason": "probe_error"}
        if not st.get("ok"):
            return {"ready": False, "identity_ok": False, "reason": st.get("reason")}
        return {"ready": bool(st.get("ingest_key_present")), "identity_ok": bool(st.get("ingest_key_acl_ok"))}

    def _emitter_task_probe() -> dict:
        try:
            st = lha.read_emitter_host_state()
        except Exception:  # noqa: BLE001
            return {"disabled": False, "reason": "probe_error"}
        if not st.get("ok"):
            return {"disabled": False, "reason": st.get("reason")}
        return {"disabled": bool(st.get("emitter_task_disabled"))}

    return DemoActuators(
        # Value-blind read-back probes (no key or key-derived value; fail-closed when the executor is DARK).
        emitter_key_probe=_emitter_key_probe,
        emitter_task_probe=_emitter_task_probe,
        # REMAINING integration: no value-blind #48 MT5_ALLOW_LIVE read-back exists yet (ADR-0128) ⇒ left None, which
        # also keeps provisioned() False.
        host_admission_probe=None,
        # Risk-INCREASING: only with the execution flag ON and a live signed executor (off by default ⇒ None ⇒ DARK).
        admission_provisioner=(lha.make_admission_provisioner(fid) if (exec_on and host_live) else None),
        emitter_enabler=(lha.make_emitter_enabler() if (exec_on and host_live) else None),
        # Risk-REDUCING teardown actuators: flag-INDEPENDENT (available whenever the executor is live) so a stop can
        # still disarm the host after the execution flag toggles off mid-session.
        emitter_disabler=(lha.make_emitter_disabler() if host_live else None),
        emitter_key_remover=(lha.make_emitter_key_remover() if host_live else None),
        admission_restore=(lha.make_admission_revoker(fid) if host_live else None),
        # DELIBERATELY UNPROVISIONED in this repository-only wiring: no standing-key transfer channel is added here.
        # provisioned() is therefore False ⇒ confirm fails closed even with the execution flag on.
        verifier_window_opener=None,
    )
