"""broker_catalogue.preseed — the governed provisioning stage that CONSUMES the approved catalogue (Objective B).

DARK unless ``HOSTED_BROKER_CATALOGUE_ENABLED``. When armed it resolves the account's broker from its
authoritative server, and — only for a SUPPORTED + human-APPROVED + SHA-verified artefact — asks the host to
copy that exact ``servers.dat`` into the fresh runtime and read-back-verify its SHA, then records provenance.

Safety invariants:
  * DARK default -> byte-identical to before (no lookup, no host call); a fresh runtime uses native MT5 discovery.
  * UNSUPPORTED broker -> NEVER fails provisioning; leaves the runtime broker-neutral (native discovery) and
    records ``catalogue_fallback=native_discovery``.
  * SUPPORTED but corrupt/unapproved -> fail closed for THAT artefact (never copy unverified bytes); still falls
    back to native discovery (does not brick onboarding) and emits operator telemetry.
  * NEVER writes into the golden image; only into the tenant's own fresh runtime (host primitive is confined +
    Customer-Zero-refused). Records catalogue version / broker / artefact SHA / MT5 build / timestamp.
"""
from __future__ import annotations

import logging

from broker_catalogue.flags import hosted_broker_catalogue_enabled
from broker_catalogue import service as S

logger = logging.getLogger("guvfx.broker_catalogue")
SOURCE = "broker_catalogue.preseed"


def _telemetry(account_id, payload: dict) -> None:
    """Best-effort operator telemetry (DARK behind OPERATIONS_EVENTS_ENABLED). Never raises; no secrets."""
    try:
        from operational_events.events import record_event
        from operational_events.constants import CATEGORY_SYSTEM
        record_event(category=CATEGORY_SYSTEM, event_type="broker_catalogue_preseed", severity="INFO",
                     source=SOURCE, summary=payload.get("reason_code", ""), status=payload.get("reason_code", ""),
                     metadata={k: v for k, v in payload.items() if k != "sha256"})
    except Exception:  # pragma: no cover — telemetry is best-effort
        logger.debug("broker_catalogue preseed telemetry failed acct=%s", account_id)


def run_catalogue_preseed(account, *, executor=None, rdp_host: str = "") -> dict:
    """One governed preseed pass for ``account``. Returns a secret-free result dict. Never raises into the
    provisioning caller (fail-open to native discovery). Copies nothing unless SUPPORTED+APPROVED+SHA-verified."""
    if not hosted_broker_catalogue_enabled():
        return {"enabled": False, "reason_code": S.PRESEED_DISABLED, "preseeded": False, "fallback_native": True}

    plan = S.resolve_broker_preseed(account)
    result = {"enabled": True, "reason_code": plan.reason_code, "broker_id": plan.broker_id,
              "catalogue_version": plan.catalogue_version, "preseeded": False,
              "fallback_native": plan.fallback_native}

    if not plan.preseed:
        # UNSUPPORTED / no-active / unapproved -> native discovery preserved; telemetry for the unapproved case.
        if plan.reason_code == S.PRESEED_UNAPPROVED:
            logger.warning("broker_catalogue: supported broker %s NOT approved -> native fallback (acct=%s)",
                           plan.broker_id, getattr(account, "id", "?"))
            _telemetry(getattr(account, "id", None), result)
        return result

    # SUPPORTED + APPROVED + SHA-verified -> copy the exact bytes via the confined host primitive, read-back verify.
    if executor is None:
        result["reason_code"] = "catalogue_executor_unavailable"
        result["fallback_native"] = True
        _telemetry(getattr(account, "id", None), result)
        return result
    from hosted_workspace.host_agent_dispatch import derive_slot
    slot = derive_slot(account.id)
    fn = getattr(executor, "preseed_broker_artefact", None)
    if fn is None:
        result["reason_code"] = "catalogue_executor_incomplete"
        result["fallback_native"] = True
        _telemetry(getattr(account, "id", None), result)
        return result
    try:
        res = fn(slot["runtime_root"], plan.broker_id, plan.sha256, plan.host_relpath, rdp_host=rdp_host)
    except Exception:  # noqa: BLE001 — host errors are ambiguous -> fail closed, sanitised
        logger.warning("broker_catalogue: host preseed errored acct=%s broker=%s", getattr(account, "id", "?"),
                       plan.broker_id)
        res = {"ok": False, "reason": "host_error"}
    ok = bool(res) and bool(res.get("ok")) and str(res.get("verified_sha256", "")).lower() == plan.sha256
    result["preseeded"] = ok
    result["host_reason"] = (res or {}).get("reason", "")
    if ok:
        # Record provenance (catalogue version / broker / SHA / build / timestamp) — non-secret.
        from django.utils import timezone
        prov = {"catalogue_version": plan.catalogue_version, "broker_id": plan.broker_id,
                "artefact_sha256": plan.sha256, "server_name": plan.server_name,
                "preseeded_at": timezone.now().isoformat()}
        result["provenance"] = prov
        logger.info("broker_catalogue: preseeded %s for acct=%s (version=%s sha=%s)", plan.broker_id,
                    getattr(account, "id", "?"), plan.catalogue_version, plan.sha256[:12])
    else:
        # Copy attempted but read-back SHA mismatch / host failure -> do NOT trust it; native fallback.
        result["fallback_native"] = True
        result["reason_code"] = "catalogue_preseed_verify_failed"
        logger.warning("broker_catalogue: preseed verify FAILED acct=%s broker=%s -> native fallback",
                       getattr(account, "id", "?"), plan.broker_id)
    _telemetry(getattr(account, "id", None), result)
    return result
