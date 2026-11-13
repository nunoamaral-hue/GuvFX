"""copier_verifier_window — operator command for the bounded verifier window (ADR-0101).

Opens / closes / inspects the bounded window that makes the backend's ``COPIER_INGEST_HMAC_KEY`` readable to
the gated verifier endpoints from an EPHEMERAL tmpfs file, for one verification session.

SECURITY CONTRACT
-----------------
* ``--open`` reads the key from **STDIN ONLY** — never a command argument, never an env var, never a prompt
  echoed to a terminal (the operator wrapper supplies it through a hidden prompt + pipe). The key is written
  0600 to the tmpfs path and NEVER printed, logged, or returned.
* Output is always redacted JSON (window metadata only). The key value never appears anywhere in this command's
  arguments, output, or logs.
* ``--close`` is idempotent: it shreds the tmpfs key file and closes any open window. Safe to run in a
  guaranteed-cleanup ``finally`` on every path (success / failure / timeout / interruption).
* This command grants NO execution/arm/admission authority. It only governs WHEN two no-side-effect identity
  endpoints can resolve their key.
"""
from __future__ import annotations

import json
import sys

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Open / close / inspect the bounded COPIER_INGEST_HMAC_KEY verifier window (ADR-0101)."

    def add_arguments(self, parser):
        g = parser.add_mutually_exclusive_group(required=True)
        g.add_argument("--open", action="store_true", help="Open a window; read the key from STDIN (never argv).")
        g.add_argument("--close", action="store_true", help="Idempotently shred the key + close any open window.")
        g.add_argument("--status", action="store_true", help="Print redacted window status (no secret).")
        parser.add_argument("--purpose", choices=["ingest", "attest", "both"], default="ingest",
                            help="Which verifier(s) the window serves (default: ingest).")
        parser.add_argument("--ttl", type=int, default=300, help="Window lifetime in seconds (capped at 900).")
        parser.add_argument("--max-uses", type=int, default=1,
                            help="Hard cap on verifications before auto-close (default 1; 0 = bounded only by expiry).")
        parser.add_argument("--key-path", default="", help="Override the tmpfs key path (default /dev/shm/...).")
        parser.add_argument("--opened-by-email", default="", help="Staff operator email for the audit row.")
        parser.add_argument("--note", default="", help="Short audit note (never a secret).")
        parser.add_argument("--correlation-id", default="", help="Opaque audit correlation id.")

    def handle(self, *args, **opts):
        from trade_copier import verifier_window as vw

        if opts["close"]:
            closed = vw.close_all(reason="operator_close")
            return self._emit({"action": "close", "closed_windows": closed, "status": vw.window_status()})

        if opts["status"]:
            return self._emit({"action": "status", "status": vw.window_status()})

        # --open: the key comes from STDIN only. Read, validate, write the ephemeral file, open the window.
        if sys.stdin is None or sys.stdin.isatty():
            raise CommandError("--open requires the key on STDIN (piped); refusing an interactive TTY read.")
        key = sys.stdin.read()
        purpose = opts["purpose"].upper()
        path = (opts["key_path"] or vw.verifier_key_path()).strip()
        max_uses = None if int(opts["max_uses"]) == 0 else int(opts["max_uses"])
        opened_by = self._resolve_operator(opts["opened_by_email"])

        # Any failure on the open path shreds the ephemeral file so no key is ever left behind.
        try:
            vw.write_key_file(path, key)
            key = None   # scrub the key reference as soon as it is on the tmpfs file
            window = vw.open_window(
                opened_by=opened_by, purpose=purpose, ttl_seconds=int(opts["ttl"]), key_path=path,
                max_uses=max_uses, correlation_id=opts["correlation_id"], note=opts["note"])
        except vw.VerifierWindowError as e:
            vw._shred_file(path)
            raise CommandError(f"refused to open window: {e.reason}")
        except Exception:
            vw._shred_file(path)
            raise
        finally:
            key = None   # never retain the key in this frame on any path

        self._emit({
            "action": "open",
            "window_id": window.id,
            "purpose": window.purpose,
            "expires_at": window.expires_at.isoformat(),
            "max_uses": window.max_uses,
            "key_file_written": True,     # NEVER the key value
            "status": vw.window_status(),
        })

    def _resolve_operator(self, email: str):
        email = (email or "").strip()
        if not email:
            return None
        from django.contrib.auth import get_user_model
        return get_user_model().objects.filter(email__iexact=email).first()

    def _emit(self, obj: dict):
        self.stdout.write(json.dumps(obj, default=str, indent=1))
