"""GuvFX Copy Trading INDEPENDENT one-shot emitter identity-attestation entrypoint (ADR-0104) - repository
artifact, DARK. ASCII-only.

This is the action of the pre-installed, DISABLED one-shot task (oneshot_attest_task_template.xml). It is run
non-interactively AS the tenant (guvfx_u_46) exactly once, when the signed host-executor op
TRIGGER_EMITTER_ONESHOT_ATTEST enables + triggers that task; the host primitive re-disables the task afterwards.
It exists so an authorized operator can bootstrap the #46 emitter identity attestation WITHOUT an interactive
PowerShell desktop (the #46 web workspace is MT5-only), replacing the human-operated
operator/Invoke-GuvfxCopierOneShotAttestation.ps1 for the headless path.

Contract (fail-closed, secret-blind to the backend and to this script's output):
  * Pin account 46; a config whose account_id is not 46 is refused.
  * The ingest secret is read from a HOST-LOCAL EPHEMERAL key file (--key-file) - the host-local secret
    mechanism. This script NEVER invents/provisions a secret store: if that file is absent/empty/too short the
    result is SECRET_UNAVAILABLE and NOTHING is attested or posted. The key is injected into a PROCESS-LOCAL env
    only (never Machine/User env, registry, task XML, config, disk, stdout or a log) and the ephemeral key file
    is always shredded after this single attempt (one-use).
  * It runs the emitter's one-shot ``attest`` mode (identity only; NEVER positions), then POSTs the signed
    attestation envelope EXACTLY ONCE (no retry) to the backend ATTEST verifier (--verifier-url, the
    attest-verify route - never the event-ingest route).
  * It writes a SANITISED result (--result-file): ok + http_status/verified/reason only - never the envelope,
    signature, broker login or the secret. The host primitive reads that file.

It creates NO lifecycle state / MasterEvent / ExecutionJob / alert, contacts NO follower account, places NO
order, and enables NO observation/replication/execution/event-ingestion. Packaging mirrors run_emitter.py (the
pinned emitter library subset is vendored into ``lib/`` at bundle-build time). This file carries no secret.
"""
import argparse
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB = os.path.join(_HERE, "lib")
if os.path.isdir(_LIB) and _LIB not in sys.path:
    sys.path.insert(0, _LIB)

# The sole account this bootstrap exists for (defence in depth; the backend command + the host .ps1 also pin it).
EMITTER_ONESHOT_ACCOUNT_ID = 46
# Minimum usable ingest-secret length (matches trade_copier.emitter.app.resolve_secret). A shorter value is
# treated as an absent host-local secret so a truncated/placeholder key never produces a bogus attestation.
_MIN_SECRET_LEN = 32


def _result(ok, account_id, *, reason, posted, now, http_status=None, verified=False, observed_probed=False):
    return {"ok": bool(ok), "account_id": int(account_id), "mode": "oneshot-attest",
            "http_status": http_status, "verified": bool(verified), "posted": bool(posted),
            "observed_identity_probed": bool(observed_probed), "reason": str(reason), "at": float(now())}


def _read_key_file(path):
    """Return the stripped secret from the ephemeral host-local key file, or None if it is absent/unreadable.
    Never logs the value. A short/empty value is handled by the caller as SECRET_UNAVAILABLE."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _shred_key_file(path):
    """One-use: best-effort overwrite-then-remove of the ephemeral key file so the secret never persists past this
    single attempt. Silent on any error (the file may legitimately be absent - SECRET_UNAVAILABLE path)."""
    try:
        if path and os.path.isfile(path):
            try:
                size = os.path.getsize(path)
                with open(path, "r+b") as fh:
                    fh.seek(0)
                    fh.write(b"\x00" * max(size, 1))
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError:
                pass
            os.remove(path)
    except OSError:
        pass


def _post_once(url, envelope, transport):
    """POST the signed attestation envelope EXACTLY ONCE (no retry). Returns (http_status, verified, reason).
    ``transport`` is injectable for tests; the default uses urllib. Only the signed envelope is sent - no secret."""
    body = json.dumps(envelope).encode("utf-8")
    if transport is not None:
        return transport(url, body)
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    raw = b""
    try:
        with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 (fixed configured verifier URL)
            status = int(getattr(r, "status", 0) or r.getcode())
            raw = r.read()
    except urllib.error.HTTPError as e:
        status = int(e.code)
        try:
            raw = e.read()
        except Exception:  # noqa: BLE001
            raw = b""
    except Exception as e:  # noqa: BLE001 - any transport fault is a non-crashing, fail-closed post failure
        return 0, False, "post_error:" + type(e).__name__
    verified = False
    reason = "http_%d" % status
    try:
        j = json.loads(raw.decode("utf-8")) if raw else {}
        if isinstance(j, dict) and j.get("status"):
            reason = str(j.get("status"))
            verified = (j.get("status") == "verified")
    except Exception:  # noqa: BLE001 - a non-JSON body leaves the http_<code> reason, verified False
        pass
    return status, verified, reason


def _run(args, *, env, transport, app_factory, now):
    from trade_copier.emitter.app import EmitterApp
    from trade_copier.emitter.config import ConfigError, load_config_file

    account_id = EMITTER_ONESHOT_ACCOUNT_ID
    env = dict(os.environ if env is None else env)
    key_env_name = None
    try:
        try:
            config = load_config_file(args.config)
        except ConfigError as e:
            return _result(False, account_id, reason="config_error:" + str(e)[:48], posted=False, now=now)
        if int(getattr(config, "account_id", 0) or 0) != account_id:
            return _result(False, account_id, reason="account_not_emitter_pinned", posted=False, now=now)
        key_env_name = config.ingest_key_env

        # Host-local secret mechanism: the ephemeral key file. Absent/short => SECRET_UNAVAILABLE (fail closed).
        key = _read_key_file(args.key_file)
        if not key or len(key) < _MIN_SECRET_LEN:
            return _result(False, account_id, reason="SECRET_UNAVAILABLE", posted=False, now=now)

        env[key_env_name] = key   # PROCESS-LOCAL only; scrubbed in the finally; never persisted
        key = None
        app = app_factory(config, env) if app_factory else EmitterApp(config, env=env)

        att_out = app.attest()   # identity only (NO positions); reads the key from env by name
        probed = bool(att_out.get("observed_identity_probed"))
        if not att_out.get("ok") or not isinstance(att_out.get("envelope"), dict):
            reason = str(att_out.get("reason") or "attest_failed")
            if reason == "attestation_key_unavailable":
                reason = "SECRET_UNAVAILABLE"   # normalise the key-absent reason to the one contract code
            return _result(False, account_id, reason=reason, posted=False, now=now, observed_probed=probed)

        http_status, verified, post_reason = _post_once(args.verifier_url, att_out["envelope"], transport)
        return _result(bool(verified), account_id, reason=post_reason, posted=True, now=now,
                       http_status=http_status, verified=verified, observed_probed=probed)
    finally:
        try:
            if key_env_name:
                env.pop(key_env_name, None)   # drop the process-local secret reference
        except Exception:  # noqa: BLE001
            pass
        _shred_key_file(args.key_file)   # one-use: never leave the ephemeral secret after an attempt


def _write_result(path, result):
    """Write the SANITISED result JSON atomically so the host primitive reads a complete object. Never contains
    the envelope or the secret."""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    except OSError:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(result, default=str))
    os.replace(tmp, path)


def main(argv=None, *, env=None, transport=None, app_factory=None, now=None):
    parser = argparse.ArgumentParser(prog="guvfx-copier-oneshot-attest",
                                     description="GuvFX Copy Trading one-shot emitter identity attestation")
    parser.add_argument("--config", required=True, help="path to the account-scoped emitter config JSON")
    parser.add_argument("--verifier-url", required=True, help="the backend attest-verify URL (never event-ingest)")
    parser.add_argument("--key-file", required=True, help="host-local ephemeral ingest-secret file (one-use)")
    parser.add_argument("--result-file", required=True, help="sanitised result output path the primitive reads")
    args = parser.parse_args(argv)
    now = now or time.time
    result = _run(args, env=env, transport=transport, app_factory=app_factory, now=now)
    try:
        _write_result(args.result_file, result)
    except OSError:
        result = {**result, "ok": False, "reason": "result_write_failed"}
    print(json.dumps(result, default=str))   # sanitised; carries no envelope/secret
    return 0 if result.get("ok") else 1


if __name__ == "__main__":   # pragma: no cover
    raise SystemExit(main())
