"""host_gateway.gateway_http — thin reference HTTP transport for the monitoring gateway (ADR-0075, DARK).

Stdlib ``http.server`` only. This is the DEPLOY-TIME transport skeleton; ALL security logic lives in
``gateway_core.MonitoringGateway.handle`` (tested in-process without sockets). It is NOT run by this
packet: ``serve`` must be called explicitly with a configured bind host/port — it never auto-starts on
import, and it refuses to start when the gateway has no credential (fail-closed, security.md) or when no
bind interface is configured (no accidental public exposure). Kept intentionally small; a production
deployment would harden logging/TLS/termination separately.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _reject_non_finite(token):
    # CPython's json.loads accepts the bare tokens NaN/Infinity/-Infinity by default; reject them so a
    # non-finite timestamp can never reach the handler (defense-in-depth with the core's finite check).
    raise ValueError(f"non-finite JSON constant: {token}")


def make_handler(gateway):
    """Return a BaseHTTPRequestHandler bound to ``gateway``. POST a JSON request body; the handler replies
    with the JSON ``gateway.handle(...)`` result at its ``_status``. No route/path is taken from the URL —
    the operation is a field in the body and is allow-listed in the core."""

    class _Handler(BaseHTTPRequestHandler):
        server_version = "GuvFXMonitoringGateway"

        def _reply(self, payload: dict):
            status = int(payload.pop("_status", 200))
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802 - stdlib handler contract
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length > 0 else b""
                request = json.loads(raw.decode("utf-8"), parse_constant=_reject_non_finite) if raw else {}
                # The agent token rides a header; the core treats it as request["token"].
                if isinstance(request, dict) and "token" not in request:
                    request["token"] = self.headers.get("X-GuvFX-Agent-Token", "")
            except Exception:  # noqa: BLE001
                return self._reply({"ok": False, "error": "malformed_request", "_status": 400})
            return self._reply(gateway.handle(request))

        def log_message(self, *args, **kwargs):  # silence default stderr logging (no request detail leak)
            return

    return _Handler


def serve(gateway, *, bind_host: str, bind_port: int):  # pragma: no cover - deploy-time entrypoint
    """Bind the gateway to a CONFIGURED interface and serve. Fail-closed: refuses startup without a
    credential or a bind interface. NOT invoked by the repository packet."""
    gateway.assert_startup()
    if not bind_host or not bind_port:
        raise RuntimeError("monitoring gateway refuses to start without a configured bind host/port")
    httpd = ThreadingHTTPServer((bind_host, int(bind_port)), make_handler(gateway))
    httpd.serve_forever()
