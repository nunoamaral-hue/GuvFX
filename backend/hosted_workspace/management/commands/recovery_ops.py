"""recovery_ops — single source of truth for the governed operational lease + safe recovery-config metadata (pkt-127).

Used by the in-container tooling and (via ``docker compose exec``) by the host acceptance runner. The Django-free
deploy gate ``scripts/deploy_recovery_gate.py`` reads the lease FILE directly so it still works when the backend is
down; this command and that gate share the one lease state file.

Subcommands (all emit no secret):
  lease-status                      — print the current lease status as JSON.
  lease-acquire --kind --purpose --owner --correlation [--ttl]
                                    — acquire the single lease FAIL-CLOSED; exit 1 if refused.
  lease-release --correlation       — release the lease matching the correlation id.
  lease-reclaim                     — safely clean up an EXPIRED or MALFORMED (stale) lease.
  check-deploy [--emergency-override REASON]
                                    — exit non-zero if a live RECOVERY lease (or malformed lease) blocks a deploy;
                                      an override proceeds (exit 0) and is AUDITED.
  check-arm                         — exit non-zero if a live DEPLOY lease (or malformed lease) blocks arming.
  config                            — print the safe recovery operating-model metadata (flags, allow-list ids,
                                      breaker, lease, warnings) as JSON. No environment dump.
"""
import json

from django.core.management.base import BaseCommand
from django.utils import timezone

from hosted_workspace import recovery_lease
from hosted_workspace.liveness_recovery import recovery_config_metadata


class Command(BaseCommand):
    help = "Governed recovery operational-lease control + safe recovery-config metadata (packet-127)."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="subcommand", required=True)
        sub.add_parser("lease-status")
        p_acq = sub.add_parser("lease-acquire")
        p_acq.add_argument("--kind", required=True, choices=[recovery_lease.KIND_RECOVERY, recovery_lease.KIND_DEPLOY])
        p_acq.add_argument("--purpose", required=True)
        p_acq.add_argument("--owner", required=True)
        p_acq.add_argument("--correlation", required=True)
        p_acq.add_argument("--ttl", type=int, default=recovery_lease.TTL_DEFAULT_S)
        p_rel = sub.add_parser("lease-release")
        p_rel.add_argument("--correlation", required=True)
        sub.add_parser("lease-reclaim")
        p_cd = sub.add_parser("check-deploy")
        p_cd.add_argument("--emergency-override", dest="override", default=None,
                          help="Audited reason to proceed with a deploy despite a live recovery lease.")
        sub.add_parser("check-arm")
        sub.add_parser("config")

    def _emit(self, obj):
        self.stdout.write(json.dumps(obj, sort_keys=True, default=str))

    def handle(self, *args, **opts):
        now = timezone.now()
        cmd = opts["subcommand"]

        if cmd == "lease-status":
            self._emit(recovery_lease.status_metadata(now))
            return

        if cmd == "lease-acquire":
            ok, res = recovery_lease.acquire(opts["kind"], opts["purpose"], opts["owner"], opts["correlation"],
                                             opts["ttl"], now)
            if ok:
                self._emit({"acquired": True, "lease": res.to_dict()})
            else:
                self._emit({"acquired": False, "reason": res})
                raise SystemExit(1)
            return

        if cmd == "lease-release":
            ok, reason = recovery_lease.release(opts["correlation"], now)
            self._emit({"released": ok, "reason": reason})
            if not ok:
                raise SystemExit(1)
            return

        if cmd == "lease-reclaim":
            ok, reason = recovery_lease.reclaim(now)
            self._emit({"reclaimed": ok, "reason": reason})
            if not ok:
                raise SystemExit(1)
            return

        if cmd in ("check-deploy", "check-arm"):
            action = recovery_lease.KIND_DEPLOY if cmd == "check-deploy" else recovery_lease.KIND_RECOVERY
            blocked, reason = recovery_lease.blocks(action, now)
            if cmd == "check-deploy" and blocked and opts.get("override"):
                recovery_lease._audit("emergency_override", now, None, kind=action,
                                      reason="deploy_override:" + str(opts["override"])[:120])
                self._emit({"action": action, "blocked": False, "override": True, "original_reason": reason})
                return
            self._emit({"action": action, "blocked": blocked, "reason": reason})
            if blocked:
                raise SystemExit(2)
            return

        if cmd == "config":
            self._emit(recovery_config_metadata())
            return
