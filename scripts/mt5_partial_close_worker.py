#!/usr/bin/env python3
"""GuvFX dedicated DEMO partial-close host worker (ADR-0056 F2).

The narrowly-scoped host-side consumer that turns a server-authorized partial-close dispatch into exactly one
``partial_close_position`` bridge call. It is deliberately the LEAST-privileged execution component:

* MODERN worker credentials only (``X-Worker-Id`` / ``X-Worker-Secret``); never a shared legacy token.
* Polls ONLY the dedicated endpoints (``partial-close/claim|authorize-send|complete``). It has no knowledge of
  the generic ``next_job`` endpoint and cannot claim or act on OPEN / CLOSE / MODIFY / any other job.
* Obtains a SINGLE-USE pre-send authorization (``authorize-send``) immediately before the mutation; the
  mutation instruction (ticket / volume / symbol / identity pin) is server-issued and used verbatim.
* Calls ONLY ``partial_close_position`` — never any other bridge primitive.
* Attempts the broker call EXACTLY ONCE. On ANY timeout / disconnect / lost ACK / uncertain result it does
  NOT retry ``order_send`` — it reports the result (``need_reconcile``) and moves on; the server resolves the
  outcome by authoritative read, never a re-dispatch. A worker restart never re-sends a prior job (the server
  never re-serves a claimed job).
* Redacts credentials and account-sensitive values (login / server / secret) from all logs.
* Stops safely whenever a flag or authorization gate is unavailable (claim 204/409, authorize refused,
  backend unreachable) — it never improvises a mutation.

UNPROVISIONED + INACTIVE by default: ``COPIER_PCLOSE_WORKER_ENABLED`` defaults OFF and the modern credentials
must be supplied by the operator. This file creates NO credentials and grants NO permission; a Sponsor must
separately mint a ``partial_close_worker`` identity and arm ``COPIER_EXECUTE_DEMO_ARMED`` (ADR-0056).

Run on the host (only after the Sponsor gates above):  ``python scripts/mt5_partial_close_worker.py``
"""
from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass

logger = logging.getLogger("guvfx.copier.pclose_worker")

_TRUTHY = ("1", "true", "yes", "on")
_CLAIM_PATH = "/api/copy-trading/partial-close/claim/"
_AUTHORIZE_PATH = "/api/copy-trading/partial-close/authorize-send/"
_COMPLETE_PATH = "/api/copy-trading/partial-close/complete/"
# A bridge call that failed on the worker side (exception / timeout) — reported so the server reconciles
# authoritatively. It is NEVER retried from here (a retry could double-reduce a position).
_WORKER_FAILURE_RESULT = {"ok": False, "error": "worker_exception_no_retry", "need_reconcile": True}


class WorkerDisabled(Exception):
    """The worker is not enabled / not provisioned — exit cleanly (0), nothing to do."""


class WorkerMisconfigured(Exception):
    """The worker is enabled but required configuration is missing — exit non-zero without acting."""


@dataclass(frozen=True)
class WorkerConfig:
    base_url: str
    worker_id: str
    worker_secret: str
    poll_interval: float = 5.0
    http_timeout: float = 20.0
    verify_tls: bool = True


def load_config(env: dict | None = None) -> WorkerConfig:
    """Build the worker configuration from the environment. Raises ``WorkerDisabled`` when the kill-flag is
    off (the default) and ``WorkerMisconfigured`` when enabled but credentials/base URL are missing. No secret
    value is ever logged."""
    env = os.environ if env is None else env
    if str(env.get("COPIER_PCLOSE_WORKER_ENABLED", "")).strip().lower() not in _TRUTHY:
        raise WorkerDisabled("COPIER_PCLOSE_WORKER_ENABLED is off (default)")
    base_url = str(env.get("GUVFX_BACKEND_BASE_URL", "")).strip().rstrip("/")
    worker_id = str(env.get("GUVFX_PCLOSE_WORKER_ID", "")).strip()
    worker_secret = str(env.get("GUVFX_PCLOSE_WORKER_SECRET", "")).strip()
    missing = [n for n, v in (("GUVFX_BACKEND_BASE_URL", base_url),
                              ("GUVFX_PCLOSE_WORKER_ID", worker_id),
                              ("GUVFX_PCLOSE_WORKER_SECRET", worker_secret)) if not v]
    if missing:
        raise WorkerMisconfigured("missing required config: " + ", ".join(missing))
    try:
        poll = float(env.get("COPIER_PCLOSE_WORKER_POLL_SECONDS", "5") or 5)
    except (TypeError, ValueError):
        poll = 5.0
    verify = str(env.get("GUVFX_BACKEND_VERIFY_TLS", "1")).strip().lower() in _TRUTHY
    return WorkerConfig(base_url=base_url, worker_id=worker_id, worker_secret=worker_secret,
                        poll_interval=max(poll, 1.0), verify_tls=verify)


def _redact_identity(identity: dict | None) -> dict:
    """Return a log-safe view of an identity pin — login/server are account-sensitive and are masked."""
    ident = identity or {}
    out = {}
    if ident.get("expected_login"):
        out["expected_login"] = "***"
    if ident.get("expected_server"):
        out["expected_server"] = "***"
    if "require_identity_pin" in ident:
        out["require_identity_pin"] = bool(ident.get("require_identity_pin"))
    return out


class BackendClient:
    """Minimal HTTP client bound to the THREE dedicated endpoints only. Modern worker auth headers on every
    request; no method reaches the generic claim endpoint. ``http`` is injectable for tests (anything with a
    ``.post(url, json=, headers=, timeout=, verify=)`` returning ``.status_code`` + ``.json()``)."""

    def __init__(self, config: WorkerConfig, http=None):
        self._c = config
        self._http = http if http is not None else _requests_session()

    def _headers(self) -> dict:
        return {"X-Worker-Id": self._c.worker_id, "X-Worker-Secret": self._c.worker_secret}

    def _post(self, path: str, body: dict):
        resp = self._http.post(self._c.base_url + path, json=body, headers=self._headers(),
                               timeout=self._c.http_timeout, verify=self._c.verify_tls)
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001 — a non-JSON body is treated as empty
            data = {}
        return resp.status_code, (data or {})

    def claim(self):
        return self._post(_CLAIM_PATH, {})

    def authorize_send(self, job_id, dispatch_token):
        return self._post(_AUTHORIZE_PATH, {"job_id": job_id, "dispatch_token": dispatch_token})

    def complete(self, job_id, dispatch_token, send_nonce, bridge_result):
        return self._post(_COMPLETE_PATH, {"job_id": job_id, "dispatch_token": dispatch_token,
                                           "send_nonce": send_nonce, "bridge_result": bridge_result})


def _requests_session():
    import requests  # lazy — only needed for a real run, not for unit tests
    return requests.Session()


def _default_bridge_call(*, ticket, volume, identity, expected_symbol):
    """Invoke the real bridge ``partial_close_position`` (imported lazily from the sibling bridge script).
    The ONLY bridge primitive this worker may call."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from mt5_signal_bridge import partial_close_position
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "bridge_import_failed", "need_reconcile": False, "detail": str(e)}
    return partial_close_position(int(ticket), float(volume), identity=identity, expected_symbol=expected_symbol)


def run_once(client: BackendClient, *, bridge_call=_default_bridge_call) -> dict:
    """Process AT MOST one partial close end-to-end: claim -> authorize-send -> single bridge call -> complete.

    Returns a small action dict for the loop/tests. The broker mutation is attempted EXACTLY ONCE and NEVER
    retried; any flag/authorization gate being unavailable stops safely BEFORE the bridge call."""
    status, data = client.claim()
    if status == 204 or data.get("reason") == "no_jobs":
        return {"action": "idle"}
    if status != 200 or not data.get("ok"):
        # Flags off / wrong route / auth problem — stop safely, do not act.
        logger.info("pclose worker: claim unavailable status=%s reason=%s", status, data.get("reason"))
        return {"action": "claim_unavailable", "status": status, "reason": data.get("reason")}

    job_id = data["job_id"]
    dispatch_token = data["dispatch_token"]

    # PRE-SEND authorization — the mutation instruction is released ONLY here, after the backend re-reads the
    # feature flags in-lock. If refused, the bridge is NEVER called; the job stays server-side for the sweep.
    a_status, auth = client.authorize_send(job_id, dispatch_token)
    if a_status != 200 or not auth.get("ok"):
        logger.info("pclose worker: authorize-send refused job=%s status=%s reason=%s",
                    job_id, a_status, auth.get("reason"))
        return {"action": "authorize_refused", "job_id": job_id, "reason": auth.get("reason")}

    send_nonce = auth.get("send_nonce")
    ticket, volume = auth.get("ticket"), auth.get("volume")
    expected_symbol, identity = auth.get("expected_symbol"), auth.get("identity")
    logger.info("pclose worker: authorized job=%s ticket=%s symbol=%s identity=%s",
                job_id, ticket, expected_symbol, _redact_identity(identity))

    # EXACTLY ONE broker call — never retried on timeout/disconnect/uncertain result.
    try:
        bridge_result = bridge_call(ticket=ticket, volume=volume, identity=identity,
                                    expected_symbol=expected_symbol)
    except Exception as e:  # noqa: BLE001 — a worker-side failure is reported, NOT retried
        logger.warning("pclose worker: bridge call failed job=%s (no retry): %s", job_id, type(e).__name__)
        bridge_result = dict(_WORKER_FAILURE_RESULT)

    c_status, c_out = client.complete(job_id, dispatch_token, send_nonce, bridge_result or {})
    logger.info("pclose worker: completed job=%s status=%s recon=%s", job_id, c_status, c_out.get("status"))
    return {"action": "completed", "job_id": job_id, "complete_status": c_status,
            "reconciliation_status": c_out.get("status")}


def run_forever(client: BackendClient, *, bridge_call=_default_bridge_call, stop=None) -> None:
    """Poll loop. ``stop`` is an optional callable returning True to exit (tests inject one)."""
    poll = client._c.poll_interval
    consecutive_errors = 0
    while not (stop and stop()):
        try:
            out = run_once(client, bridge_call=bridge_call)
            consecutive_errors = 0
            # Back off a touch while idle; process the next job promptly after a completion.
            time.sleep(0 if out.get("action") == "completed" else poll)
        except Exception as e:  # noqa: BLE001 — backend unreachable etc.: back off, never act blindly
            consecutive_errors += 1
            logger.warning("pclose worker: poll error (%s) — backing off", type(e).__name__)
            time.sleep(min(poll * consecutive_errors, 60))


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = load_config()
    except WorkerDisabled as e:
        logger.info("pclose worker not enabled: %s — exiting", e)
        return 0
    except WorkerMisconfigured as e:
        logger.error("pclose worker misconfigured: %s — refusing to start", e)
        return 2
    logger.info("pclose worker starting (worker_id=%s, base=%s) — DEDICATED partial-close only",
                config.worker_id, config.base_url)
    run_forever(BackendClient(config))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
