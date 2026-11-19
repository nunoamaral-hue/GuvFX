"""GuvFX Copy Trading NON-TRADING one-shot INGEST-signing probe (GFX-160, ADR-0119) - repository artifact, DARK.
ASCII-only.

Reviewed operator command for proving, end to end, that the STANDING account-46 ingest key can sign a request the
backend accepts via the bounded INGEST verifier window - i.e. host-key signing + transport + backend HMAC
authentication + single-use nonce/replay protection - WITHOUT any trading semantics. It is run once, by hand, by an
authorized operator during a Sponsor-gated INGEST window; it is NOT a scheduled task and NOT a persistent worker.

Contract (fail-closed, secret-blind):
  * Pins account 46, the fixed probe operation, and the fixed backend endpoint (derived from the account config's
    own ``ingest_url`` ORIGIN + the fixed ``/api/copy-trading/ingest-probe/`` path). It takes NO arbitrary
    URL / account / operation argument.
  * Reads the EXISTING standing key from the host-local file ``<config dir>/ingest.key`` (or ``--key-file``) - the
    SAME file the standing emitter auto-loads. The key is NEVER accepted through argv, environment, a prompt, stdin
    or chat, and is NEVER printed, hashed, length-measured or otherwise exposed. The standing key file is RETAINED
    (never shredded/rotated/removed) - this is a non-destructive probe.
  * Builds and signs EXACTLY ONE structurally non-trading probe envelope (``trade_copier.emitter.ingest_probe`` -
    no symbol/side/volume/price/ticket/order field exists) and POSTs it EXACTLY ONCE (no retry) to the fixed
    ingest-probe route (never event-ingest, never attest-verify).
  * With ``--replay`` it POSTs the SAME already-generated envelope a SECOND time to prove the backend rejects a
    replay (expected ``replayed``). The envelope is never re-signed and never exposed.
  * Writes only a SANITISED, value-blind result (stdout and optional ``--result-file``): verified / sanitised
    reason / correlation id / timestamp / attempt count - never the envelope, signature, secret or any trade data.

It creates NO lifecycle state / MasterEvent / ExecutionJob / authorization / arm, enables NO task/worker/flag,
contacts NO follower account (#48) and NO broker, places/modifies/closes NO order, and reads NO MT5 state. The in
-memory key and envelope are scrubbed in a finally. Packaging mirrors run_emitter.py (the pinned emitter library
subset is vendored into ``lib/`` at bundle-build time). This file carries no secret.
"""
import argparse
import json
import os
import secrets
import sys
import time
from urllib.parse import urlsplit, urlunsplit

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB = os.path.join(_HERE, "lib")
if os.path.isdir(_LIB) and _LIB not in sys.path:
    sys.path.insert(0, _LIB)

# The sole account this probe exists for (defence in depth; the backend verifier and the pure signer also pin it).
PROBE_ACCOUNT_ID = 46
# Minimum usable ingest-secret length (matches run_emitter.py / resolve_secret). A shorter value is treated as an
# absent host-local secret so a truncated/placeholder key never produces a bogus probe.
_MIN_SECRET_LEN = 32
# The fixed backend route this probe targets (never event-ingest, never attest-verify).
_PROBE_PATH = "/api/copy-trading/ingest-probe/"

# Allow-list of the backend ingest-probe's SANITISED rejection reason codes (mirrors
# trade_copier.ingest_probe_api._AUTH_FAIL / _MALFORMED plus the view-level reasons). These are non-secret
# identifiers retained ONLY so the operator can see WHY a probe was rejected without echoing a raw body.
_SANITISED_PROBE_REASONS = frozenset({
    # 401 signature / account / operation / timestamp (ingest_probe_api._AUTH_FAIL)
    "unknown_key_id", "bad_signature", "probe_stale", "probe_expired", "timestamp_in_future", "expiry_too_far",
    "account_mismatch", "account_not_probe_pinned", "nonce_too_long",
    # 400 malformed (ingest_probe_api._MALFORMED) + view-level sanitised reasons
    "malformed_envelope", "missing_signed_field", "bad_protocol_version", "bad_operation", "bad_timestamp",
    "bad_account_id", "malformed_json", "body_too_large", "verifier_window_unavailable",
})
# Safe top-level response statuses the backend returns; used when a status carries no sub-reason.
_SAFE_PROBE_STATUS = frozenset({"verified", "rejected", "replayed", "disabled", "unavailable"})
_REJECTED_UNKNOWN = "rejected_unknown"


def _result(ok, *, reason, verified, posted, now, http_status=None, attempts=0, correlation_id="",
            transport_ambiguous=False, replay_reason=None):
    return {"ok": bool(ok), "account_id": PROBE_ACCOUNT_ID, "mode": "ingest-probe",
            "verified": bool(verified), "posted": bool(posted), "http_status": http_status,
            "reason": str(reason), "attempts": int(attempts), "correlation_id": str(correlation_id or ""),
            "transport_ambiguous": bool(transport_ambiguous),
            "replay_reason": (str(replay_reason) if replay_reason is not None else None),
            "at": float(now())}


def _read_key_file(path):
    """Return the stripped secret from the standing key file, or None if absent/unreadable. Never logs the value.
    A short/empty value is handled by the caller as SECRET_UNAVAILABLE. The file is NOT modified or removed."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _probe_url(ingest_url):
    """Derive the fixed ingest-probe URL from the account config's ingest_url ORIGIN + the fixed probe path. Never
    an arbitrary caller-supplied URL. Returns None if the origin is unusable (fail closed)."""
    try:
        parts = urlsplit(str(ingest_url or ""))
        if not parts.scheme or not parts.netloc:
            return None
        return urlunsplit((parts.scheme, parts.netloc, _PROBE_PATH, "", ""))
    except Exception:  # noqa: BLE001 - a malformed config URL fails closed
        return None


def _classify_post_response(status, raw):
    """Map the backend ingest-probe HTTP status + raw body to (verified, SANITISED reason, transport_ambiguous).
    Secret-blind: retains only an allow-listed top-level reason; a rejected response with a missing/unknown reason
    collapses to rejected_unknown; an unrecognised/empty body keeps the http_<code> fallback and ambiguous=True."""
    verified = False
    reason = "http_%d" % status
    transport_ambiguous = True
    try:
        j = json.loads(raw.decode("utf-8")) if raw else None
    except Exception:  # noqa: BLE001 - a non-JSON body leaves the http_<code> reason, verified False, ambiguous
        j = None
    if isinstance(j, dict) and isinstance(j.get("status"), str):
        st = j["status"]
        if st == "verified":
            verified = True
            reason = "verified"
            transport_ambiguous = False
        elif st in _SAFE_PROBE_STATUS:
            transport_ambiguous = False
            if st == "rejected":
                sub = j.get("reason")
                reason = sub if (isinstance(sub, str) and sub in _SANITISED_PROBE_REASONS) else _REJECTED_UNKNOWN
            else:
                reason = st     # replayed / disabled / unavailable carry no sub-reason by contract
    return verified, reason, transport_ambiguous


def _post_once(url, envelope, transport):
    """POST the signed probe envelope EXACTLY ONCE (no retry). Returns
    (http_status, verified, reason, transport_ambiguous). ``transport`` is injectable for tests. Only the signed
    envelope is sent - no secret."""
    body = json.dumps(envelope).encode("utf-8")
    if transport is not None:
        r = transport(url, body)
        if isinstance(r, tuple) and len(r) == 3:
            s, v, rs = r
            return s, v, rs, False
        return r
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    raw = b""
    try:
        with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 (fixed, config-derived probe URL)
            status = int(getattr(r, "status", 0) or r.getcode())
            raw = r.read()
    except urllib.error.HTTPError as e:
        status = int(e.code)
        try:
            raw = e.read()
        except Exception:  # noqa: BLE001
            raw = b""
    except Exception as e:  # noqa: BLE001 - any transport fault is a non-crashing, AMBIGUOUS post failure
        return 0, False, "post_error:" + type(e).__name__, True
    verified, reason, transport_ambiguous = _classify_post_response(status, raw)
    return status, verified, reason, transport_ambiguous


def _run(args, *, transport, now, build_envelope):
    from trade_copier.emitter.config import ConfigError, load_config_file

    correlation_id = (args.correlation_id or secrets.token_hex(8))
    key = None
    envelope = None
    try:
        try:
            config = load_config_file(args.config)
        except ConfigError as e:
            return _result(False, reason="config_error:" + str(e)[:64], verified=False, posted=False, now=now,
                           correlation_id=correlation_id)
        if int(getattr(config, "account_id", 0) or 0) != PROBE_ACCOUNT_ID:
            return _result(False, reason="account_not_probe_pinned", verified=False, posted=False, now=now,
                           correlation_id=correlation_id)
        url = _probe_url(getattr(config, "ingest_url", ""))
        if not url:
            return _result(False, reason="probe_url_unresolved", verified=False, posted=False, now=now,
                           correlation_id=correlation_id)

        key_file = args.key_file or os.path.join(os.path.dirname(os.path.abspath(args.config)), "ingest.key")
        key = _read_key_file(key_file)
        if not key or len(key) < _MIN_SECRET_LEN:
            return _result(False, reason="SECRET_UNAVAILABLE", verified=False, posted=False, now=now,
                           correlation_id=correlation_id)

        nonce = secrets.token_hex(16)
        key_id = str(getattr(config, "key_id", "") or "copier-ingest")
        envelope = build_envelope(secret=key, account_id=PROBE_ACCOUNT_ID, nonce=nonce, now=now(), key_id=key_id)
        key = None   # scrub the secret as soon as the envelope is signed; it is never needed again

        http_status, verified, reason, ambiguous = _post_once(url, envelope, transport)
        attempts = 1
        replay_reason = None
        if args.replay and not ambiguous:
            # Prove replay protection: the SAME envelope must now be rejected (expected "replayed"). Never re-sign.
            _s2, _v2, replay_reason, _amb2 = _post_once(url, envelope, transport)
            attempts = 2
        return _result(bool(verified), reason=reason, verified=verified, posted=True, now=now,
                       http_status=http_status, attempts=attempts, correlation_id=correlation_id,
                       transport_ambiguous=ambiguous, replay_reason=replay_reason)
    finally:
        key = None         # never retain the secret on any path
        envelope = None     # drop the signed envelope reference


def _write_result(path, result):
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    except OSError:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(result, default=str))
    os.replace(tmp, path)


def main(argv=None, *, transport=None, now=None, build_envelope=None):
    parser = argparse.ArgumentParser(prog="guvfx-copier-ingest-probe",
                                     description="GuvFX Copy Trading NON-TRADING one-shot ingest-signing probe")
    parser.add_argument("--config", required=True, help="path to the account-scoped emitter config JSON (pins 46)")
    parser.add_argument("--key-file", default="",
                        help="optional standing key path (default <config dir>/ingest.key); never a secret value")
    parser.add_argument("--replay", action="store_true",
                        help="after the probe, re-POST the SAME envelope once to prove replay is rejected")
    parser.add_argument("--correlation-id", default="", help="operator correlation id (non-secret) for the result")
    parser.add_argument("--result-file", default="", help="optional sanitised result output path")
    args = parser.parse_args(argv)
    now = now or time.time
    if build_envelope is None:
        from trade_copier.emitter.ingest_probe import build_ingest_probe as build_envelope
    result = _run(args, transport=transport, now=now, build_envelope=build_envelope)
    if args.result_file:
        try:
            _write_result(args.result_file, result)
        except OSError:
            result = {**result, "ok": False, "reason": "result_write_failed"}
    print(json.dumps(result, default=str))   # sanitised; carries no envelope/secret
    return 0 if result.get("ok") else 1


if __name__ == "__main__":   # pragma: no cover
    raise SystemExit(main())
