"""trade_copier.mutation_transport — the REAL identity-pinned, route-pinned per-tenant follower MUTATION
transport for the ADR-0086 dispatcher (ADR-0087). DARK / default-OFF.

The ADR-0086 dispatcher ships with an INERT default transport; this is the real one it uses once armed. It:

* resolves the GOVERNED follower's OWN per-tenant bridge endpoint via the audited identity firewall
  (``snapshot_transport.resolve_account_snapshot_base`` — never a global / sibling bridge),
* authenticates with the env-resolved agent token (``monitoring_gateway_client._agent_token`` — the token NEVER
  enters the repository, command arguments, logs, Notion or chat),
* ROUTE-PINS to the sole governed master→follower copier route and IDENTITY-BINDS the command's expected
  login/server to the resolved follower account (a caller supplies NO url / account / symbol; there is NO global
  "allow live accounts" bypass),
* POSTs the typed ``MODIFY_PROTECTION`` / ``FULL_CLOSE`` command (the ADR-0083 envelope) with a TIGHT deadline,
  attempts the broker call AT MOST ONCE, and
* returns a STRUCTURED outcome: a definite bridge response is passed through (carrying the bridge guard's
  ``lifecycle_outcome``); a timeout / transport error / non-2xx is ``UNKNOWN_OUTCOME`` with ``need_reconcile`` —
  NEVER inferred as success, NEVER retried.

It OPENS no position and never recovers a late/failed OPEN — it only sends an existing MODIFY/CLOSE on a bound
follower ticket, or fails closed. Default OFF: with ``copier_mutation_transport_enabled`` off it is INERT and
contacts NO broker. PARTIAL_CLOSE keeps its dedicated least-privilege worker (ADR-0056); OPEN keeps the
PLACE_ORDER path.
"""
from __future__ import annotations

import json as _json
import logging

logger = logging.getLogger(__name__)

# Guard outcome vocabulary (kept in sync with scripts/mt5_copier_lifecycle_guard.py; this backend module never
# imports the host guard script).
UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"

INERT_RESULT = {"ok": False, "error": "mutation_transport_disabled", "need_reconcile": False}

# copier_operation -> the bridge HTTP mutation route. PARTIAL_CLOSE is intentionally absent (worker path).
_OP_ROUTE = {"FULL_CLOSE": "/mt5/close-position", "MODIFY_PROTECTION": "/mt5/modify-position"}

_DEFAULT_TIMEOUT = 20.0   # tight per-request deadline (seconds); a cold MT5 attach can approach this


def _unknown(reason: str) -> dict:
    """An UNKNOWN, reconcile-required outcome — NEVER success, NEVER retried."""
    return {"ok": False, "error": "transport_" + reason, "need_reconcile": True,
            "lifecycle_outcome": UNKNOWN_OUTCOME, "lifecycle_reason": reason}


def _blocked(reason: str) -> dict:
    """A pre-send transport refusal — nothing was sent (``guard_blocked`` so the backend records 'nothing sent')."""
    return {"ok": False, "error": "transport_" + reason, "guard_blocked": True, "need_reconcile": False,
            "lifecycle_reason": reason}


def resolve_governed_follower_route(replication) -> tuple[bool, str, str]:
    """Resolve + ROUTE-PIN the follower's per-tenant bridge base URL. ``(ok, reason, base_url)``. Fail-closed:
    the replication's follower must be an ENABLED governed copier follower, and its endpoint must resolve
    per-tenant READY (never a global / sibling bridge). No caller-supplied URL is honoured."""
    try:
        follower = replication.follower
    except Exception:  # noqa: BLE001
        return False, "no_follower", ""
    if follower is None or not getattr(follower, "enabled", False):
        return False, "follower_not_enabled", ""
    acct = getattr(follower, "follower_account", None)
    if acct is None:
        return False, "no_follower_account", ""
    # Per-tenant endpoint resolution with the audited identity firewall (fails closed; never global/sibling).
    from execution.snapshot_transport import resolve_account_snapshot_base, ST_PER_TENANT_OK
    from execution.models import HostedExecutionEndpoint  # noqa: F401 (import side-effect parity with resolver)
    import os
    global_base = (os.getenv("GUVFX_WINDOWS_AGENT_BASE_URL") or os.getenv("GUVFX_AGENT_URL") or "").strip()
    st = resolve_account_snapshot_base(acct, global_base_url=global_base)
    if not st.ok or not st.per_tenant or st.reason_code != ST_PER_TENANT_OK:
        # A LIVE follower mutation REQUIRES its own per-tenant endpoint — never the shared global agent.
        return False, "endpoint_" + st.reason_code, ""
    return True, "ok", st.base_url


def _identity_matches(command, follower) -> bool:
    """The command's server-derived identity pin must match the resolved follower account (defence in depth —
    the command already carries the authorized follower identity; this refuses a cross-account command)."""
    acct = getattr(follower, "follower_account", None)
    exp_login = str(command.get("expected_login") or "").strip()
    exp_server = str(command.get("expected_server") or "").strip()
    acct_login = str(getattr(acct, "account_number", "") or "").strip()
    acct_server = str(getattr(getattr(acct, "broker_server", None), "server_name", "") or "").strip()
    if not (exp_login and exp_server and acct_login and acct_server):
        return False
    return exp_login == acct_login and exp_server.lower() == acct_server.lower()


def send_mutation(command: dict, *, now_epoch: float, http=None) -> dict:
    """Send ONE governed follower MODIFY/CLOSE command through the per-tenant transport. At most one broker call;
    never retried; UNKNOWN on any indeterminate result (never success). Returns a bridge-shaped result."""
    from trade_copier.flags import copier_mutation_transport_enabled
    if not copier_mutation_transport_enabled():
        return dict(INERT_RESULT)

    op = str(command.get("copier_operation") or "")
    route = _OP_ROUTE.get(op)
    if route is None:
        return _blocked("unsupported_operation")   # PARTIAL_CLOSE/OPEN are not this transport's job
    ticket = command.get("ticket")
    if ticket is None:
        return _blocked("missing_ticket")

    # Resolve the replication + route-pin to the governed follower.
    from trade_copier.models import CopierReplication
    rep_id = command.get("copier_replication_id")
    replication = CopierReplication.objects.select_related("follower", "follower__follower_account").filter(
        id=rep_id).first() if rep_id else None
    if replication is None:
        return _blocked("replication_missing")
    ok, reason, base_url = resolve_governed_follower_route(replication)
    if not ok:
        return _blocked(reason)
    if not _identity_matches(command, replication.follower):
        return _blocked("identity_mismatch")

    # Hard deadline (defence in depth — the bridge guard also checks). A past-deadline command is never sent.
    exp = command.get("authorization_expires_at")
    deadline = _parse_epoch(exp)
    if deadline is None or float(now_epoch) >= float(deadline):
        return _blocked("past_deadline")

    # Agent token from the env chain — never in repo / args / logs.
    from execution.monitoring_gateway_client import _agent_token
    token = _agent_token()
    if not token:
        return _blocked("no_agent_token")

    url = base_url.rstrip("/") + route
    body = _json.dumps(command).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-GuvFX-Agent-Token": token}
    try:
        status, resp = (http or _default_http)(url, body=body, headers=headers, timeout=_DEFAULT_TIMEOUT)
    except Exception as e:  # noqa: BLE001 — a transport error is an INDETERMINATE send, never success
        logger.warning("copier mutation transport error ticket=%s op=%s: %s", ticket, op, type(e).__name__)
        return _unknown("exception")
    if status is None:
        return _unknown("no_response")
    if int(status) != 200:
        # A non-2xx bridge response is indeterminate for the POSITION state unless the body proves otherwise;
        # a 400 pre-send refusal from the bridge carries guard_blocked/error -> pass it through (nothing sent).
        if isinstance(resp, dict) and (resp.get("guard_blocked") or resp.get("lifecycle_outcome")):
            return resp
        return _unknown("http_%s" % int(status))
    if not isinstance(resp, dict):
        return _unknown("bad_response")
    return resp


def _parse_epoch(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime, timezone
        s = str(v).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


def _default_http(url: str, *, body: bytes, headers: dict, timeout: float):
    """The real one-shot HTTP POST (urllib). Returns ``(status, parsed_json_or_None)``. Never retried by the
    caller. Injectable for tests so no real broker is contacted."""
    import urllib.request
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (internal per-tenant endpoint)
        raw = r.read() or b"{}"
        try:
            parsed = _json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            parsed = None
        return r.status, parsed


def mutation_transport(*, http=None):
    """A ``transport(command) -> bridge_result`` callable for ``mutation_dispatch.run_mutation_dispatch_pass``.
    INERT when ``copier_mutation_transport_enabled`` is off."""
    import time

    def _transport(command):
        return send_mutation(command, now_epoch=time.time(), http=http)
    return _transport
