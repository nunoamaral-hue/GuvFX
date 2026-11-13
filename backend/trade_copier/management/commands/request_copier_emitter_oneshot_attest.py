"""request_copier_emitter_oneshot_attest - operator command for the INDEPENDENT host-emitter one-shot
identity-attestation TRIGGER (ADR-0104). DARK.

Asks the signed host executor to run account #46's pre-installed, DISABLED one-shot emitter attestation task
EXACTLY ONCE (enable -> trigger -> ALWAYS re-disable, host-side), so an authorized operator can bootstrap the
#46 emitter identity attestation WITHOUT an interactive PowerShell desktop (the #46 web workspace is MT5-only).

SECURITY CONTRACT
-----------------
* Exact-account-46 pin: any other account is refused (policy edge; the signed op + host dispatcher stay
  account-generic + Customer-Zero-refused, and the host .ps1 re-pins #46 as defence in depth).
* DARK flag: with ``copier_emitter_oneshot_enabled()`` OFF the command refuses BEFORE any host contact.
* Dry-run by default: without ``--apply`` it only prints a PLAN and contacts NO host. ``--apply`` is required to
  make the single host invocation (no auto-retry).
* The signed host executor is separately gated (host-executor flag + keyring/base_url); when it is DARK/unconfigured
  the command refuses with ``host_executor_unavailable`` and contacts NO host.
* It is attestation-only: it arms NO observation/replication/execution/event-ingestion, contacts NO follower
  account, and places/modifies/closes NO order. The ingest secret stays HOST-LOCAL and never reaches the backend;
  the host result is SANITISED (ok + http_status/verified/reason) and this command re-sanitises it defensively -
  no envelope, signature, broker login or secret ever appears in its output.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

_EMITTER_ONESHOT_ACCOUNT_ID = 46
_SECRET_KEYS = {"envelope", "signature", "secret", "key", "signed", "payload", "pw", "password"}


class Command(BaseCommand):
    help = ("Trigger the pre-installed DISABLED one-shot emitter identity-attestation task for account 46 once "
            "via the signed host executor (ADR-0104, DARK). Dry-run unless --apply.")

    def add_arguments(self, parser):
        parser.add_argument("--account", type=int, default=_EMITTER_ONESHOT_ACCOUNT_ID,
                            help=f"Account id (pinned to {_EMITTER_ONESHOT_ACCOUNT_ID}; any other is refused).")
        parser.add_argument("--apply", action="store_true",
                            help="Make the single host invocation. Default: dry-run PLAN, no host contact.")

    def handle(self, *args, **opts):
        from trade_copier.flags import copier_emitter_oneshot_enabled

        account_id = int(opts["account"])
        apply = bool(opts["apply"])

        # Gate 1 - exact-account-46 pin (policy edge).
        if account_id != _EMITTER_ONESHOT_ACCOUNT_ID:
            raise CommandError(
                f"refused: this bootstrap is pinned to account {_EMITTER_ONESHOT_ACCOUNT_ID} (got {account_id}).")

        # Gate 2 - DARK flag. OFF => refuse BEFORE any host contact.
        if not copier_emitter_oneshot_enabled():
            return self._emit({"ok": False, "action": "request_oneshot_attest", "account_id": account_id,
                               "reason": "copier_emitter_oneshot_disabled", "host_contacted": False,
                               "applied": apply})

        # Gate 3 - dry-run default => PLAN only, NO host contact.
        if not apply:
            return self._emit({"ok": True, "action": "request_oneshot_attest", "plan": True,
                               "account_id": account_id, "host_contacted": False, "applied": False,
                               "would": "enable + trigger + always re-disable the pre-installed DISABLED one-shot "
                                        "attest task once",
                               "note": "re-run with --apply to make the single host invocation (no auto-retry)."})

        # Gate 4 - resolve the signed host executor. DARK/unconfigured => None => refuse, NO host contact.
        from hosted_workspace.host_executor import resolve_signed_host_executor
        try:
            executor = resolve_signed_host_executor(account_id=account_id, rdp_host="")
        except Exception:  # noqa: BLE001 - any resolver failure is ambiguous => fail closed, no host contact
            executor = None
        if executor is None:
            return self._emit({"ok": False, "action": "request_oneshot_attest", "account_id": account_id,
                               "reason": "host_executor_unavailable", "host_contacted": False, "applied": True})

        # Apply - EXACTLY ONE invocation, no retry. The result is already sanitised host-side; re-sanitise here.
        result = executor.trigger_emitter_oneshot_attest()
        safe = self._sanitise(result)
        self._emit({"ok": bool(safe.get("ok")), "action": "request_oneshot_attest", "account_id": account_id,
                    "host_contacted": True, "applied": True, "result": safe})

    def _sanitise(self, result) -> dict:
        if not isinstance(result, dict):
            return {"ok": False, "reason": "malformed_result"}
        return {k: v for k, v in result.items() if str(k).lower() not in _SECRET_KEYS}

    def _emit(self, obj: dict):
        self.stdout.write(json.dumps(obj, default=str, indent=1))
