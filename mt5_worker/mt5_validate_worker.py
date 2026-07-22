import json
import time
import glob
import os
from pathlib import Path

import requests

# Backend writes validate requests here (mounted into both containers)
HANDOFF = Path("/app/.guvfx_handoff_validate")

DEFAULT_TIMEOUT = 20  # seconds
POLL_INTERVAL = 1     # seconds


def _write_json_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def _agent_base() -> str:
    return (os.getenv("WINDOWS_AGENT_BASE") or os.getenv("GUVFX_AGENT_URL") or "").rstrip("/")


def _agent_token() -> str:
    return (os.getenv("WINDOWS_AGENT_TOKEN") or os.getenv("GUVFX_AGENT_TOKEN") or "").strip()


# ─── RX-2C reliability heartbeat (best-effort; never affects validation) ───
class WorkerCredentialError(RuntimeError):
    """This worker's own credential is missing, placeholder, or inconsistent across its aliases."""


HEARTBEAT_SOURCE = "validate_worker"
HEARTBEAT_INTERVAL_S = int(os.getenv("RELIABILITY_HEARTBEAT_INTERVAL_S", "60"))


def _backend_base() -> str:
    # Public HTTPS base by default: the backend enforces SSL-redirect, so an
    # internal http:// call 301s to https on a plain port and fails. The public
    # endpoint terminates TLS at Traefik. Override with GUVFX_API_BASE if needed.
    return (os.getenv("GUVFX_API_BASE") or "https://api.guvfx.com").rstrip("/")


def _worker_token() -> str:
    """Resolve THIS worker's own credential (X-Worker-Token). No cross-credential fallback.

    Post-rotation hardening (WS1): this previously fell back to ``GUVFX_AGENT_TOKEN`` — the BRIDGE's
    credential — which only ever worked because the two secrets happened to hold the same value. The
    2026-07-22 rotation broke that conflation and produced silent heartbeat 401s. ``GUVFX_WORKER_TOKEN``
    remains an accepted ALIAS of the same worker secret; the bridge's agent token is not.
    """
    return _resolve_worker_token(os.environ)


def _resolve_worker_token(env) -> str:
    names = ("MT5_WORKER_TOKEN", "GUVFX_WORKER_TOKEN")   # aliases of the SAME worker secret
    found = {n: (env.get(n) or "").strip() for n in names}
    found = {n: v for n, v in found.items() if v}
    if not found:
        raise WorkerCredentialError(
            "MT5_WORKER_TOKEN (validate-worker credential) is not configured. Checked: "
            + ", ".join(names)
            + ". This worker will NOT fall back to the bridge's GUVFX_AGENT_TOKEN."
        )
    values = set(found.values())
    if len(values) > 1:
        raise WorkerCredentialError(
            "MT5_WORKER_TOKEN / GUVFX_WORKER_TOKEN hold different values — these must be the same "
            "secret. Refusing to guess."
        )
    value = values.pop()
    low = value.lower()
    if any(m in low for m in ("replace", "changeme", "example", "placeholder", "<", "${", "scrubbed")):
        raise WorkerCredentialError("MT5_WORKER_TOKEN looks like placeholder text, not a real secret.")
    return value


def emit_heartbeat() -> None:
    base = _backend_base()
    try:
        token = _worker_token()
    except WorkerCredentialError:
        # Startup validation (main) already refuses to run without it; if we somehow get here, stay
        # best-effort rather than killing the validation loop.
        return
    if not base or not token:
        return
    try:
        requests.post(
            f"{base}/api/reliability/heartbeat/",
            json={"source": HEARTBEAT_SOURCE, "expected_interval_s": HEARTBEAT_INTERVAL_S},
            headers={"X-Worker-Token": token},
            timeout=8,
        )
    except Exception:
        pass  # heartbeat is best-effort; validation must not be affected


def _infer_username(user_dir: Path, cred: dict) -> str:
    """
    Priority:
      1) explicit cred["username"]
      2) infer from folder name: /free/<user_id>/...
         -> guvfx_u_<user_id>
    """
    u = str(cred.get("username") or "").strip()
    if u:
        return u

    user_id = user_dir.name.strip()
    # folder is like ".../free/1"
    if user_id.isdigit():
        return f"guvfx_u_{user_id}"

    # fallback: still try something sane
    return f"guvfx_u_{user_id}"


def _validate_via_agent(username: str, login: str, server: str) -> tuple[bool, str]:
    """
    Calls Windows agent EA-based validator:
      POST /validate-mt5-ea
    Expects JSON: { ok, valid, reason, ea_path, ea }
    """
    base = _agent_base()
    token = _agent_token()

    if not base:
        return False, "WINDOWS_AGENT_BASE is not set"
    if not token:
        return False, "WINDOWS_AGENT_TOKEN is not set"

    url = f"{base}/validate-mt5-ea"
    headers = {
        "X-GuvFX-Agent-Token": token,
        "Content-Type": "application/json",
    }
    payload = {
        "username": username,
        "login": login,
        "server": server,
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=DEFAULT_TIMEOUT)
    except Exception as e:
        return False, f"windows_agent_request_failed:{type(e).__name__}:{e}"

    try:
        data = r.json()
    except Exception:
        return False, f"windows_agent_bad_json status={r.status_code} body={r.text[:200]}"

    if not bool(data.get("ok", False)):
        msg = str(data.get("message") or data.get("error") or "agent_error")
        return False, f"agent_error:{msg}"

    if bool(data.get("valid", False)):
        return True, ""

    # Not valid → return the reason (and optionally surface EA payload)
    reason = str(data.get("reason") or "invalid")
    ea_path = str(data.get("ea_path") or "")
    return False, f"{reason} ({ea_path})"


def handle_validate_once() -> None:
    base = HANDOFF / "free"
    if not base.exists():
        return

    for req_str in glob.glob(str(base / "*" / "validate_request.json")):
        req = Path(req_str)
        udir = req.parent  # /free/<user_id>
        cred_path = udir / "validate_cred.json"
        res_path = udir / "validate_result.json"

        print(f"[validate] found request={req}", flush=True)

        try:
            if not cred_path.exists():
                _write_json_atomic(res_path, {"ok": False, "error": "missing validate_cred.json"})
                print(f"[validate] missing cred -> wrote result={res_path}", flush=True)
                continue

            cred = json.loads(cred_path.read_text(encoding="utf-8"))
            login = str(cred.get("login", "")).strip()
            server = str(cred.get("server", "")).strip()

            # password not needed for EA-based check (login happens inside MT5 already)
            if not (login and server):
                _write_json_atomic(res_path, {"ok": False, "error": "missing login/server"})
                print(f"[validate] missing fields -> wrote result={res_path}", flush=True)
                continue

            username = _infer_username(udir, cred)
            ok, err = _validate_via_agent(username=username, login=login, server=server)

            _write_json_atomic(res_path, {"ok": ok, "error": err})
            print(f"[validate] wrote result={res_path} ok={ok}", flush=True)
            if not ok:
                print(f"[validate] error={err[:200]}", flush=True)

        except Exception as e:
            _write_json_atomic(res_path, {"ok": False, "error": f"validate_worker_error:{type(e).__name__}:{e}"})
            print(f"[validate] exception {type(e).__name__}: {e}", flush=True)

        finally:
            # EPHEMERAL cleanup
            for p in (req, cred_path):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass


def validate_startup_config() -> None:
    """WS3 startup self-validation: refuse to run without this worker's OWN credential.

    Fails at start with an operator-actionable message instead of discovering the problem later as
    silent heartbeat 401s (which is exactly how the 2026-07-22 coupling manifested).
    """
    _resolve_worker_token(os.environ)   # raises WorkerCredentialError with a clear diagnostic
    print("[validate] startup config OK: worker credential present (MT5_WORKER_TOKEN)", flush=True)


def main() -> None:
    print("[validate] mt5_validate_worker starting (WINDOWS / EA)", flush=True)
    try:
        validate_startup_config()
    except WorkerCredentialError as exc:
        print(f"[validate] FATAL: {exc}", flush=True)
        raise SystemExit(1)
    last_hb = 0.0
    while True:
        now = time.time()
        if now - last_hb >= HEARTBEAT_INTERVAL_S:
            emit_heartbeat()  # RX-2C: best-effort liveness, throttled
            last_hb = now
        handle_validate_once()
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
