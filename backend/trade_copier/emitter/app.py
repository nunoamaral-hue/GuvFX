"""trade_copier.emitter.app — the host-side emitter runner + CLI (ADR-0076). Django-free; host-runnable.

Wraps the ADR-0074 signing library (``bridge_emitter.BridgeEmitter``) with the read-only MT5 adapter, a durable
spool and the attestation envelope into three explicit, separately-gated modes:

* ``attest``       — one-shot identity attestation. Reads ONLY account identity (``/mt5/snapshots/account``),
                     never positions; builds + self-verifies a signed attestation envelope; emits NO lifecycle
                     state, ExecutionJob, alert or broker activity. Gated by ``attestation_allowed``.
* ``observe-once`` — one observation cycle: read positions → detect changes (``BridgeEmitter.observe``) → spool;
                     deliver only if ``delivery_allowed``. Gated by ``observation_allowed``.
* ``run``          — the bounded fast/reconcile loop the installed (disabled) service would run. Inert unless
                     ``observation_allowed`` AND a positive ``fast_interval_seconds`` are configured.

Every gate defaults OFF and a malformed config evaluates OFF. The emitter NEVER places/modifies/closes an order
(the adapter has no such method). Secrets are read from env by NAME at use and never logged. Diagnostics are
structured and secret-safe; an unavailable value is reported as its explicit state, never as zero.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from trade_copier import bridge_emitter as be
from trade_copier.emitter import attestation as att
from trade_copier.emitter.config import ConfigError, EmitterConfig, load_config_file
from trade_copier.emitter.mt5_adapter import BridgeHttpAdapter, PositionSnapshotAdapter

logger = logging.getLogger(__name__)
_MIN_SECRET_LEN = 32


def resolve_secret(env_name: str, env: Optional[dict] = None) -> str:
    """Read a secret from the environment by NAME, fail closed. A missing/short/placeholder value → '' (OFF).
    Never logs the value."""
    env = env if env is not None else os.environ
    val = (env.get(env_name) or "").strip()
    if len(val) < _MIN_SECRET_LEN or "xxx" in val.lower():
        return ""
    return val


def _http_ingest_transport(url: str, *, timeout: int = 10, opener=None) -> Callable[[dict], be.SendResult]:
    """Default delivery transport: POST the signed ingestion envelope to the backend. Injectable for tests."""
    import urllib.error
    import urllib.request

    def _default_opener(req, to):
        with urllib.request.urlopen(req, timeout=to) as r:  # noqa: S310 (fixed configured backend URL)
            return r.status, r.read()

    send_opener = opener or _default_opener

    def send(envelope: dict) -> be.SendResult:
        body = json.dumps(envelope).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            status, _raw = send_opener(req, timeout)
            return be.SendResult(ok=(int(status) == 200), status=int(status), reason=str(status))
        except urllib.error.HTTPError as e:   # 4xx/5xx — a classified, non-crashing delivery failure
            return be.SendResult(ok=False, status=int(e.code), reason=str(e.code))
        except Exception as e:  # noqa: BLE001 — any transport fault is a retryable send failure
            return be.SendResult(ok=False, status=0, reason=type(e).__name__)

    return send


class EmitterApp:
    """One account-scoped emitter instance. Adapter/transport/env/clock are injectable so tests never touch the
    network or the production adapter (Phase-10)."""

    def __init__(self, config: EmitterConfig, *, adapter: Optional[PositionSnapshotAdapter] = None,
                 transport: Optional[Callable[[dict], be.SendResult]] = None, env: Optional[dict] = None,
                 clock: Callable[[], float] = None, now_dt: Callable[[], datetime] = None, spool=None):
        config.require_complete_identity()   # fail closed: no start without a complete immutable identity
        self.config = config
        self.env = env if env is not None else os.environ
        self._clock = clock or (lambda: datetime.now(timezone.utc).timestamp())
        self._now_dt = now_dt or (lambda: datetime.now(timezone.utc))
        self.adapter = adapter or self._build_default_adapter()
        self._transport = transport or _http_ingest_transport(config.ingest_url)
        self._spool = spool or (be.FileSpool(config.spool_path) if config.spool_path else be.MemorySpool())
        self._emitter = be.BridgeEmitter(
            account_id=config.account_id, spool=self._spool,
            key_provider=lambda: resolve_secret(config.ingest_key_env, self.env),
            transport=self._transport, enabled=config.observation_allowed,
            broker_login=config.broker_login, broker_server=config.broker_server,
            max_pending=config.max_pending, max_attempts=config.max_attempts, key_id=config.key_id)
        self._diag = {"mode": None, "last_snapshot": None, "last_snapshot_reason": None,
                      "last_delivery": None, "stale_rejected": 0, "identity_mismatch": False,
                      "bridge_unavailable": False, "backend_unavailable": False, "spool_corruption": False}

    def _build_default_adapter(self) -> PositionSnapshotAdapter:
        token = resolve_secret(self.config.agent_token_env, self.env)
        return BridgeHttpAdapter(base_url=self.config.bridge_base_url, token=token)

    # ── attestation (reads identity only; NEVER positions) ───────────────────────────────────────────
    def attest(self, *, nonce: Optional[str] = None, probe_observed: bool = True) -> dict:
        if not self.config.attestation_allowed:
            return {"ok": False, "mode": "attest", "reason": "attestation_gate_off"}
        secret = resolve_secret(self.config.ingest_key_env, self.env)
        if not secret:
            return {"ok": False, "mode": "attest", "reason": "attestation_key_unavailable"}
        observed_login = None
        if probe_observed:
            ident = self.adapter.read_identity()   # /mt5/snapshots/account — identity only, NO positions
            if ident.ok:
                observed_login = ident.account_login
                server_bad = bool(self.config.broker_server and ident.account_server
                                  and ident.account_server.strip().casefold()
                                  != self.config.broker_server.strip().casefold())
                login_bad = bool(observed_login and self.config.broker_login
                                 and str(observed_login).strip() != str(self.config.broker_login).strip())
                if server_bad or login_bad:
                    # The live terminal we reached is NOT the bound account — fail closed; emit NO usable
                    # attestation (consistent with observe_once, and so a wrong-terminal envelope never exists).
                    self._diag["identity_mismatch"] = True
                    return {"ok": False, "mode": "attest", "reason": "identity_mismatch",
                            "diagnostics": self.diagnostics()}
            else:
                self._diag["bridge_unavailable"] = True
        env = att.build_attestation(
            secret=secret, account_id=self.config.account_id, broker_login=self.config.broker_login,
            broker_server=self.config.broker_server, host_id=self.config.host_id,
            terminal_fp=self.config.terminal_fingerprint, bridge_id=self.config.bridge_id,
            build_sha=self.config.build_sha, config_digest=self.config.config_digest(),
            key_id=self.config.key_id, nonce=(nonce or uuid.uuid4().hex), now=self._clock(),
            observed_login=observed_login)
        # Self-verify locally (the envelope round-trips with our own key) — a build sanity check, no DB, no send.
        seen: set = set()
        try:
            att.verify_attestation(env, keyring={self.config.key_id: secret}, now=self._clock(),
                                   nonce_burn=lambda n, e: (n not in seen) and (seen.add(n) or True))
            local_ok = True
        except att.AttestError as e:
            local_ok = False
            logger.error("attestation self-verify failed: %s", e.reason)
        return {"ok": local_ok, "mode": "attest", "envelope": env,
                "key_id": self.config.key_id,  # the key IDENTITY only; never the secret nor a secret-derived hash
                "observed_identity_probed": bool(observed_login), "diagnostics": self.diagnostics()}

    # ── observation (gated) ──────────────────────────────────────────────────────────────────────────
    def observe_once(self) -> dict:
        self._diag["mode"] = "observe-once"
        if not self.config.observation_allowed:
            return {"ok": False, "mode": "observe-once", "reason": "observation_gate_off",
                    "diagnostics": self.diagnostics()}
        snap = self.adapter.read_positions()
        self._diag["last_snapshot_reason"] = snap.reason
        if not snap.ok:
            self._diag["bridge_unavailable"] = True
            self._diag["last_snapshot"] = None
            return {"ok": False, "mode": "observe-once", "reason": snap.reason,
                    "diagnostics": self.diagnostics()}
        # Identity firewall at the emitter: the observed server must match the bound identity (fail closed).
        if (self.config.broker_server and snap.account_server
                and snap.account_server.strip().casefold() != self.config.broker_server.strip().casefold()):
            self._diag["identity_mismatch"] = True
            return {"ok": False, "mode": "observe-once", "reason": "identity_mismatch",
                    "diagnostics": self.diagnostics()}
        self._diag["bridge_unavailable"] = False
        self._diag["last_snapshot"] = {"n_positions": len(snap.positions), "trade_mode": snap.trade_mode}
        try:
            result = self._emitter.observe(snap.positions)   # diff → monotonic revisions → spool (enabled gate)
            delivered = None
            if self.config.delivery_allowed:
                flush = self._emitter.flush()
                delivered = flush
                self._diag["last_delivery"] = flush
                self._diag["backend_unavailable"] = (flush.get("sent", 0) == 0 and flush.get("pending", 0) > 0)
        except be.SpoolError:
            # Corrupt durable spool: fail closed (do NOT resume on a reset ledger), surfaced for the operator.
            self._diag["spool_corruption"] = True
            return {"ok": False, "mode": "observe-once", "reason": "spool_corruption",
                    "diagnostics": self.diagnostics()}
        return {"ok": True, "mode": "observe-once", "changed_tickets": list(result.changed_tickets),
                "revisions_emitted": result.revisions_emitted, "delivered": delivered,
                "diagnostics": self.diagnostics()}

    def run_loop(self, *, max_cycles: Optional[int] = None, sleep: Callable[[float], None] = time.sleep) -> dict:
        """The bounded fast/reconcile loop the (disabled) service would run. Inert unless observation is allowed
        AND a positive fast interval is configured. ``max_cycles`` bounds it for tests; None loops until
        interrupted (the service manages lifetime). Backoff + jitter on failure; never an unbounded tight loop."""
        self._diag["mode"] = "run"
        if not self.config.observation_allowed:
            return {"ok": False, "mode": "run", "reason": "observation_gate_off", "cycles": 0}
        interval = self.config.effective_fast_interval()
        if interval <= 0:
            return {"ok": False, "mode": "run", "reason": "fast_loop_not_configured", "cycles": 0}
        cycles = 0
        backoff = 0.0
        while max_cycles is None or cycles < max_cycles:
            cycles += 1
            out = self.observe_once()
            if out.get("ok"):
                backoff = 0.0
                sleep(interval)
            else:
                backoff = min(self.config.max_backoff_seconds,
                              (backoff or self.config.failure_backoff_seconds) * 2)
                sleep(backoff)
        return {"ok": True, "mode": "run", "cycles": cycles}

    def diagnostics(self) -> dict:
        """Secret-safe structured status. Unavailable values are explicit states, never zero."""
        try:
            state = self._spool.load()
            queue_depth = len(state.pending)
            dropped = int(state.dropped)
        except Exception:  # noqa: BLE001 — a corrupt spool is a reported failure, not a crash
            queue_depth = None
            dropped = None
            self._diag["spool_corruption"] = True
        d = dict(self._diag)
        d.update({
            "account_id": self.config.account_id,
            "identity": self.config.identity_fingerprints(),
            "queue_depth": queue_depth,
            "dead_lettered": dropped,
            "gates": {"attestation_allowed": self.config.attestation_allowed,
                      "observation_allowed": self.config.observation_allowed,
                      "delivery_allowed": self.config.delivery_allowed},
            "fast_interval_seconds": self.config.effective_fast_interval(),
        })
        return d


# ─────────────────────────────── CLI (the Windows service / operator entrypoint) ───────────────────────────────

def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="guvfx-copier-emitter", description="GuvFX Copy Trading host emitter")
    parser.add_argument("mode", choices=["attest", "observe-once", "run", "status"])
    parser.add_argument("--config", required=True, help="path to the account-scoped emitter config JSON")
    parser.add_argument("--max-cycles", type=int, default=None)
    args = parser.parse_args(argv)
    try:
        config = load_config_file(args.config)
        app = EmitterApp(config)
    except ConfigError as e:
        print(json.dumps({"ok": False, "reason": f"config_error:{e}"}))
        return 2
    if args.mode == "attest":
        out = app.attest()
    elif args.mode == "observe-once":
        out = app.observe_once()
    elif args.mode == "run":
        out = app.run_loop(max_cycles=args.max_cycles)
    else:
        out = {"ok": True, "mode": "status", "diagnostics": app.diagnostics()}
    print(json.dumps(out, default=str))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":   # pragma: no cover
    raise SystemExit(main())
