"""CVM-Inc-3 B2 — beta provisioning agent service (private-network HTTP wrapper around the agent core).

Exposes exactly ONE endpoint (POST /provision) that hands a fixed-schema signed request to the boundary-
enforcing agent core. Binds only to the configured private/Tailscale address (startup fails otherwise).
No other route exists — there is no way to submit a command, path, script or argument.

Run:  python agent.py     (config from the environment; see config.example.json + RUNBOOK.md)
This file is a DARK artefact in B2 — it is never started, firewalled or scheduled until B3.
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)   # so the bundled ``lib`` package + agent modules import cleanly

from lib.mgmt_agent_core import BetaProvisioningAgent   # noqa: E402  bundled Django-free agent core
import config as agent_config                            # noqa: E402
import manifest as agent_manifest                        # noqa: E402
from op_impls import OpImplementations                   # noqa: E402
from stores import RuntimeLockManager, SqliteStore       # noqa: E402
from win_ops import RealWindowsOps                        # noqa: E402

AGENT_VERSION = "beta-agent-1.0.0"


def build_agent(cfg: dict, *, win=None, store=None, locks=None, manifest_path: str = "") -> BetaProvisioningAgent:
    """Assemble the boundary-enforcing agent from config + the approved manifest. Injectable (win/store/
    locks) for tests; defaults to the real Windows ops + the SQLite state store."""
    manifest_path = manifest_path or cfg.get("manifest_path") or os.path.join(_HERE, "manifest.json")
    approved = agent_manifest.load_manifest(manifest_path)
    actual = agent_manifest.compute_checksums(_HERE)
    script_manifest = agent_manifest.build_script_manifest(
        approved.get("checksums", {}), actual, approved.get("supported_operations", []))

    win = win if win is not None else RealWindowsOps()
    store = store if store is not None else SqliteStore(cfg["state_db"])
    locks = locks if locks is not None else RuntimeLockManager()
    impls = OpImplementations(win, tombstone_base=cfg["tombstone_base"]).as_dict()

    return BetaProvisioningAgent(
        keyring=cfg["keyring"], nonce_store=store, idempotency_store=store, op_impls=impls,
        agent_version=AGENT_VERSION,
        script_manifest=script_manifest,
        script_versions={f"op_{op.lower()}": approved.get("manifest_version", "")
                         for op in approved.get("supported_operations", [])},
        resolve_real_path=win.real_path, runtime_locks=locks,
        base=cfg["beta_root"], manifest_version=approved.get("manifest_version", ""))


def make_handler(agent: BetaProvisioningAgent):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # no request logging (never log request bodies / paths)
            return

        def _send(self, obj, code=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path.rstrip("/") != "/provision":
                return self._send({"outcome": "denied", "reason_code": "unknown_route"}, 404)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                req = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, TypeError):
                return self._send({"outcome": "denied", "reason_code": "malformed_request"}, 400)
            resp = agent.handle(req)     # the agent core NEVER raises; always a sanitised dict
            self._send(resp, 200)

        def do_GET(self):                # no read routes — negotiation is a signed POST
            return self._send({"outcome": "denied", "reason_code": "unknown_route"}, 404)

    return Handler


def main():
    cfg = agent_config.load_config()             # raises if the bind host is wildcard/public
    agent_config.assert_private_bind(cfg["bind_host"])
    agent = build_agent(cfg)
    httpd = ThreadingHTTPServer((cfg["bind_host"], cfg["bind_port"]), make_handler(agent))
    httpd.serve_forever()


if __name__ == "__main__":
    main()
