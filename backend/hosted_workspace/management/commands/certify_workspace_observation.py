"""ADR-0034 / M3b-2 Integration — operator-only disposable-host certification command (DARK).

Composes the certified chain against a DISPOSABLE, already-broker-connected Hosted Workspace and prints a
SECRET-FREE certification result:

    M1 Guarded Attach  ->  M3b-2 host adapter  ->  RawWorkspaceSnapshot  ->  M3b-1 producer
      ->  WorkspaceObservation  ->  M3a Workspace Manager  ->  WorkspaceDecision

Operator-only. Not a daemon, not a service, not a loop, not wired into any request path. It NEVER launches
MT5, NEVER logs in (it accepts NO password — only the non-secret expected login/server + the terminal path),
NEVER places an order, NEVER persists, NEVER emits telemetry. It REFUSES any target path that is not on the
operator-supplied disposable allow-list — production/Customer-Zero paths are rejected before the host is
touched.

Usage (run ON the disposable host, AFTER Nuno has manually logged the disposable MT5 in):

    python manage.py certify_workspace_observation \
        --workspace-id disposable-1 \
        --expected-login <DEMO_LOGIN> --expected-server <DEMO_SERVER> \
        --target-path "C:\\path\\to\\disposable\\terminal64.exe" \
        --disposable-prefix "C:\\path\\to\\disposable" \
        --tick-symbol EURUSD --previous-state CONNECTED

`MT5_GUARDED_ATTACH=1` must be set so the M1 never-launch guard is enforced.
"""
import json
import os
import time

from django.core.management.base import BaseCommand, CommandError

from hosted_workspace.agent import WorkspaceSpec
from hosted_workspace.certification import classify_target_path, run_certification


class Command(BaseCommand):
    help = ("Disposable-host certification of the ADR-0034 observation chain (M1 -> M3b-2 -> M3b-1 -> M3a). "
            "Operator-only, read-only, secret-free; refuses non-disposable paths.")

    def add_arguments(self, parser):
        parser.add_argument("--workspace-id", default="disposable")
        parser.add_argument("--expected-login", default=None, help="Non-secret expected login (no password).")
        parser.add_argument("--expected-server", default=None, help="Non-secret expected broker server.")
        parser.add_argument("--target-path", required=True, help="Fixed disposable terminal64.exe path.")
        parser.add_argument("--disposable-prefix", action="append", default=None,
                            help="Allow-listed disposable path prefix (repeatable). Falls back to "
                                 "$GUVFX_DISPOSABLE_WORKSPACE_PREFIXES (os.pathsep-separated).")
        parser.add_argument("--freshness-limit", type=float, default=60.0)
        parser.add_argument("--tick-symbol", default=None)
        parser.add_argument("--previous-state", default="CONNECTED")

    def handle(self, *args, **opts):
        # Reject any credential smuggled in as an option name — this command must never see a secret.
        for forbidden in ("password", "password_enc", "token", "keyring", "secret"):
            if opts.get(forbidden):
                raise CommandError("credential arguments are forbidden on the certification command")

        prefixes = opts.get("disposable_prefix")
        if not prefixes:
            env = os.getenv("GUVFX_DISPOSABLE_WORKSPACE_PREFIXES", "")
            prefixes = [p for p in env.split(os.pathsep) if p.strip()]
        classification = classify_target_path(opts["target_path"], allowed_prefixes=prefixes)
        # Fail-closed BEFORE touching the host: only an allow-listed disposable path may be observed.
        if classification != "disposable_authorised":
            raise CommandError(
                f"refusing to certify: target path classified '{classification}', not 'disposable_authorised'. "
                f"Supply --disposable-prefix (or $GUVFX_DISPOSABLE_WORKSPACE_PREFIXES) covering the disposable "
                f"workspace. Never certify against production / Customer Zero.")

        host = self._build_host()  # binds to M1 + a live mt5 (host-only; imported lazily)

        spec = WorkspaceSpec(
            workspace_id=opts["workspace_id"], expected_login=opts.get("expected_login"),
            expected_server=opts.get("expected_server"), target_path=opts["target_path"],
            freshness_limit_seconds=opts.get("freshness_limit"), tick_symbol=opts.get("tick_symbol"))

        result = run_certification(
            host, spec, clock=time.time, previous_state=opts["previous_state"],
            target_path_classification=classification)
        self.stdout.write(json.dumps(result, indent=2, sort_keys=True))
        return None

    def _build_host(self):
        """Construct the reference host adapter bound to the M1 Guarded-Attach primitive + a live mt5 handle.
        Host-only: MetaTrader5 and the M1 bridge are imported lazily so this command module stays importable
        (and unit-testable) off-host. The pipeline logic itself is exercised by tests via ``run_certification``
        with an injected mock host — this method is the thin, host-specific wiring."""
        # Code-enforce the M1 never-launch precondition (Part 7): without MT5_GUARDED_ATTACH the injected
        # guarded_initialize degrades to raw mt5.initialize (which may launch). Refuse to build the host.
        if os.getenv("MT5_GUARDED_ATTACH", "").strip().lower() not in ("1", "true", "yes", "on"):
            raise CommandError(
                "MT5_GUARDED_ATTACH must be enabled (1/true/yes/on) so the M1 never-launch guard is enforced")
        try:
            import MetaTrader5 as mt5  # noqa: N813 — host-only, Windows
        except Exception as exc:  # pragma: no cover - host-only path
            raise CommandError(f"MetaTrader5 is unavailable (run on the disposable host): {exc}")
        try:
            from scripts import mt5_signal_bridge as bridge  # M1 Guarded Attach (#305)
        except Exception as exc:  # pragma: no cover - host-only path
            raise CommandError(f"M1 guarded-attach bridge is unavailable (scripts/ not importable): {exc}")
        from hosted_workspace.agent_host import Mt5WorkspaceHost
        return Mt5WorkspaceHost(
            mt5, guarded_initialize=bridge.guarded_initialize,
            terminal_process_running=bridge._terminal_process_running)
