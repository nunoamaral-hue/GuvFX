import json
import time
import glob
from pathlib import Path

HANDOFF = Path("/srv/guvfx/mt5_handoff")

def _write_json_atomic(path: Path, payload: dict) -> None:
    """
    Write JSON atomically-ish: write to .tmp then replace.
    Prevents partial writes if container restarts mid-write.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)

def handle_validate_once() -> None:
    base = HANDOFF / "free"
    if not base.exists():
        # Uncomment if you want noisy logs:
        # print(f"[validate] base missing: {base}", flush=True)
        return

    # Expect: /srv/guvfx/mt5_handoff/free/<user_id>/validate_request.json
    for req_str in glob.glob(str(base / "*" / "validate_request.json")):
        req = Path(req_str)
        udir = req.parent
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
            password = str(cred.get("password", "")).strip()
            server = str(cred.get("server", "")).strip()

            # MVP: presence check ONLY (next step will trigger real MT5 login attempt)
            ok = bool(login and password and server)
            err = "" if ok else "missing login/password/server"

            print(f"[validate] creds parsed login={login!r} server={server!r} -> ok={ok}", flush=True)

            _write_json_atomic(res_path, {"ok": ok, "error": err})
            print(f"[validate] wrote result={res_path}", flush=True)

        except Exception as e:
            _write_json_atomic(res_path, {"ok": False, "error": f"validate_worker_error: {type(e).__name__}: {e}"})
            print(f"[validate] exception {type(e).__name__}: {e}", flush=True)

        finally:
            # EPHEMERAL cleanup: always delete request + cred after writing result
            try:
                req.unlink()
            except FileNotFoundError:
                pass
            try:
                cred_path.unlink()
            except FileNotFoundError:
                pass

def main() -> None:
    print("[validate] mt5_validate_worker starting", flush=True)
    while True:
        handle_validate_once()
        time.sleep(1)

if __name__ == "__main__":
    main()
