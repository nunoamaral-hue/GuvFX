#!/usr/bin/env python3
"""GuvFX Copy Trading - account-46 STANDING ingest-key SINGLE-ENTRY operator orchestration
(GFX-COPIER-INGEST-SINGLE-ENTRY-WRAPPER-156; ADR-0117 section 3 / ADR-0101 / ADR-0114). Repository tooling:
NOT installed, NOT scheduled, NOT run by any agent. A human operator runs it in their own console during one
Sponsor-gated arming session, immediately before the atomic lifecycle arm.

WHY THIS EXISTS
---------------
The standing sub-1s emitter (GuvFX_CopierEmitter_46 -> run_emitter.py) signs its event-ingest emissions with
the account-46 ingest key, and the backend event-ingest verifier resolves that key from a BOUNDED ADR-0101
verifier window (purpose INGEST). The SAME key must sit in TWO places: the host-local standing key file
(C:\\GuvFX\\copier-emitter\\46\\ingest.key, read by the emitter) and the backend verifier window (which checks
the signature). Entering the key separately into two prompts risks a single-character divergence -> every
signed event fails bad_signature. This wrapper takes the key ONCE (hidden, confirmed) and delivers the SAME
bytes to both over ssh STDIN, so they can never diverge.

Distinct from the one-shot ATTEST wrapper (provision_and_open_oneshot.py):
  * it uses the STANDING key provisioner (Provision-GuvfxCopierEmitterKey.ps1), not the one-shot key;
  * it opens an UNCAPPED window (--max-uses 0). The arm REFUSES a finite cap: every ingested OPEN/MODIFY/CLOSE
    and heartbeat burns one use, so a use-capped window would lapse mid-lifecycle and 503 the follower CLOSE,
    stranding a live position (verifier_window.covers_live_authorization). The live window is bounded ONLY by
    its TTL (<= 900s);
  * on SUCCESS it deliberately LEAVES both the host key and the OPEN window in place for the later,
    separately-gated arm sequence (the one-shot wrapper tears its ATTEST window down after one verification).

SECURITY CONTRACT (fail closed)
-------------------------------
* The key is read ONCE from a hidden prompt (getpass), re-entered to confirm, and held only in this process'
  memory for the bounded operation. It is delivered to each destination over ssh STDIN only - never argv, env,
  a file, a log line, shell history, an exception, the JSON output, Git or Notion.
* Host side: the key is piped to the REVIEWED provisioner Provision-GuvfxCopierEmitterKey.ps1 -KeyFromStdin
  (its own fixed confirm phrase precedes the key), which performs the full account-46 ACL-restricted atomic
  write + task-contract validation (it refuses unless GuvFX_CopierEmitter_46 exists, is DISABLED + triggerless,
  points at run_emitter.py + the account-46 config, and the principal SID == guvfx_u_46). Its read-only -Verify
  must then report ok + acl_verified (value-blind) before the window is opened.
* Backend side: the SAME key bytes are piped to
  copier_verifier_window --open --purpose ingest --ttl <bounded> --max-uses 0.
* PREFLIGHT before the key is ever prompted: refuse if a verifier window is already open, if a present-and-valid
  standing host key already exists, or if either state is unreadable (ambiguous -> refuse). The target is the
  fixed account 46 and the follower #48 is never referenced; both are structurally pinned by the reviewed
  provisioner and this wrapper.
* GUARANTEED CLEANUP on every non-success path (provision/verify/window failure, ssh error, exception, user
  interrupt): close the backend window (idempotent) and shred the host key (provisioner -Remove, idempotent),
  then INDEPENDENTLY verify zero open windows AND host key absent. If that independent check cannot confirm a
  clean state, it fails LOUDLY with a CLEANUP_INCOMPLETE result + exact operator remediation - it never claims
  rollback succeeded without verification.
* Output is redacted JSON only - never the key. It enables/triggers NO task, NO flag, NO authorization, NO arm,
  NO execution worker, and NEVER contacts account #48 or any broker.
"""
from __future__ import annotations

import argparse
import getpass
import json
import subprocess

PINNED_ACCOUNT = 46
# The STANDING provisioner's fixed confirmation phrase (non-secret); it precedes the key on STDIN.
CONFIRM_PHRASE = "provision-emitter-key-account-46"
MIN_LEN = 32                      # matches verifier_window._MIN_KEY_LEN / the provisioner's minimum
MIN_TTL = 30                      # verifier_window.MIN_WINDOW_TTL_SECONDS
MAX_TTL = 900                     # verifier_window.MAX_WINDOW_TTL_SECONDS (the hard cap)
DEFAULT_TTL = 900                 # a standing INGEST window should cover the whole bounded live session
WIN_PROVISIONER = r"C:\GuvFX\copier-emitter\operator\Provision-GuvfxCopierEmitterKey.ps1"


def _ssh(target, remote_argv, *, key_stdin=None, timeout=90):
    """Run `ssh <target> <remote_argv...>`; feed key_stdin (str) over stdin only. Returns (stdout, stderr, rc)."""
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", target] + remote_argv
    inp = key_stdin.encode("utf-8") if key_stdin is not None else None
    p = subprocess.run(cmd, input=inp, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    return (p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace"), p.returncode)


def _last_json(text):
    """Parse the single authoritative JSON result from a backend/host command's stdout. Fail closed (None).

    Accepts compact one-line JSON AND pretty-printed multi-line JSON; tolerates harmless non-JSON banner text
    that PRECEDES the single value; rejects empty output, non-JSON garbage, partial/truncated JSON, two-or-more
    concatenated payloads, and any non-whitespace TRAILING content. Returns the parsed object, else None.
    Callers still apply their own gate (open-check / ok / acl_verified); a None result fails closed.

    Copied verbatim from provision_and_open_oneshot.py to keep the two operator toolchains fully isolated (the
    standing-ingest and one-shot-attest key lifecycles share no code path); its behaviour is pinned by the
    LastJsonParserTests below."""
    s = text.strip()
    if not s:
        return None
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        pass
    dec = json.JSONDecoder()
    i, n = 0, len(text)
    while i < n:
        if text[i] in "{[":
            try:
                obj, end = dec.raw_decode(text, i)
            except ValueError:
                i += 1
                continue
            if text[end:].strip() != "":
                return None                               # trailing payload / garbage -> ambiguous
            return obj if isinstance(obj, dict) else None  # object only; scalar/array fails closed
        i += 1
    return None


def _win_provision_cmd(provisioner_path, *args):
    return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", provisioner_path, *args]


def _backend_window_cmd(container, python_exe, *args):
    return ["docker", "exec", "-i", container, python_exe, "manage.py", "copier_verifier_window", *args]


def _backend_window_cmd_nostdin(container, python_exe, *args):
    return ["docker", "exec", container, python_exe, "manage.py", "copier_verifier_window", *args]


def _host_key_state(a):
    """Read-only, value-blind classification of the host standing-key file, via the provisioner -Verify:
      * 'absent'        -> -Verify explicitly reports key_file_absent (the ONLY state safe to provision into);
      * 'present_valid' -> -Verify reports ok + acl_verified (a usable standing key already exists);
      * 'ambiguous'     -> anything else: present-but-unverifiable, emitter-task-contract drift, ACL/tenant
                           fault, or an unreadable/garbled result. The caller MUST refuse on 'ambiguous'
                           (fail closed). Note the provisioner sets key_file_present only AFTER Assert-TaskContract,
                           so a drifted task can hide a real file -> we never treat a non-'absent' result as clean.
    Never reads or returns the key value."""
    vout, _verr, _vrc = _ssh(a.windows_ssh, _win_provision_cmd(a.provisioner_path, "-Verify"))
    ver = _last_json(vout)
    if ver is None:
        return "ambiguous"
    if ver.get("ok") and ver.get("acl_verified"):
        return "present_valid"
    if ver.get("reason") == "key_file_absent":
        return "absent"
    return "ambiguous"


def _provision_host_key(a, key):
    """Provision the host-local STANDING ingest key from the in-memory bytes via the reviewed provisioner, then
    verify its ACL (read-only). Removes any stale key first. Returns (ok: bool, acl_verified: bool). Never prints
    the key. On a verify failure the unverified key is shredded before returning."""
    _ssh(a.windows_ssh, _win_provision_cmd(a.provisioner_path, "-Remove"))  # idempotent; fresh provision
    pout, _perr, _prc = _ssh(a.windows_ssh, _win_provision_cmd(a.provisioner_path, "-KeyFromStdin"),
                             key_stdin="%s\n%s" % (CONFIRM_PHRASE, key))
    prov = _last_json(pout)
    if not prov or not prov.get("ok"):
        return False, False
    vout, _verr, _vrc = _ssh(a.windows_ssh, _win_provision_cmd(a.provisioner_path, "-Verify"))
    ver = _last_json(vout)
    if not ver or not ver.get("ok") or not ver.get("acl_verified"):
        _ssh(a.windows_ssh, _win_provision_cmd(a.provisioner_path, "-Remove"))   # shred unverified
        return False, False
    return True, bool(ver.get("acl_verified"))


def _open_window(a, key):
    """Open exactly one UNCAPPED (--max-uses 0) INGEST window by piping the SAME key bytes to the backend
    command over STDIN. Returns (parsed_result_or_None, raw_stdout, raw_stderr). stderr is surfaced so the caller
    can distinguish a single-flight rejection (a concurrent operator's window) from other open failures."""
    win_args = ["--open", "--purpose", "ingest", "--ttl", str(a.ttl), "--max-uses", "0"]
    if a.opened_by_email:
        win_args += ["--opened-by-email", a.opened_by_email]
    if a.correlation_id:
        win_args += ["--correlation-id", a.correlation_id]
    wout, werr, _wrc = _ssh(a.backend_ssh, _backend_window_cmd(a.backend_container, a.remote_python, *win_args),
                            key_stdin=key)
    return _last_json(wout), wout, werr


def _shred_host_key(a):
    """Idempotent host key shred (provisioner -Remove); returns the parsed result or None. -Remove re-Test-Paths
    the key file AFTER deleting and sets ok = (not key_file_present) WITHOUT any task-contract gate, so its
    ok:true (key_file_present:false) is the AUTHORITATIVE 'the key file is now absent' signal even when the
    emitter task has drifted. Never raises (cleanup must not)."""
    try:
        rout, _rerr, _rrc = _ssh(a.windows_ssh, _win_provision_cmd(a.provisioner_path, "-Remove"))
        return _last_json(rout)
    except Exception:  # noqa: BLE001 - cleanup must never raise
        return None


def _close_window(a):
    """Idempotent verifier-window close (shreds the backend tmpfs key). Safe in guaranteed cleanup."""
    try:
        _ssh(a.backend_ssh, _backend_window_cmd_nostdin(a.backend_container, a.remote_python, "--close"))
    except Exception:  # noqa: BLE001 - cleanup must never raise
        pass


def _verify_cleanup(a, remove_result, *, foreign_window=False):
    """INDEPENDENTLY confirm a clean DARK state after a cleanup. Host-key absence is read from the AUTHORITATIVE
    -Remove result (ok True AND key_file_present False -- task-contract-independent); window closure from
    --status. When a FOREIGN window was already open (a concurrent operator we must NOT clobber) it is left
    intact and is not required to be closed. Returns (clean: bool, detail: dict); any unreadable signal is None
    and NOT clean, so cleanup never claims success it could not prove."""
    detail = {}
    # host key absent -- authoritative, task-contract-independent signal from -Remove
    if remove_result is None:
        detail["host_key_absent"] = None
        key_absent = False
    else:
        key_absent = (remove_result.get("ok") is True and remove_result.get("key_file_present") is False)
        detail["host_key_absent"] = key_absent
    # window closed -- or, for a foreign concurrent window, intentionally left intact
    if foreign_window:
        detail["window"] = "foreign_left_intact"
        window_ok = True
    else:
        wout, _werr, _wrc = _ssh(a.backend_ssh,
                                 _backend_window_cmd_nostdin(a.backend_container, a.remote_python, "--status"))
        st = _last_json(wout)
        if st is None:
            detail["window_closed"] = None
            window_ok = False
        else:
            window_ok = not bool(st.get("status", {}).get("open"))
            detail["window_closed"] = window_ok
    return (key_absent and window_ok), detail


def _fail(reason, **extra):
    print(json.dumps({"ok": False, "reason": reason, **extra}))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="account-46 STANDING ingest-key single-entry orchestration (host key + uncapped INGEST window)")
    ap.add_argument("--windows-ssh", required=True,
                    help="ssh target for the Windows host (e.g. administrator@host or an ssh alias)")
    ap.add_argument("--backend-ssh", required=True, help="ssh target for the backend VPS (e.g. ubuntu@host)")
    ap.add_argument("--backend-container", default="guvfx-backend")
    ap.add_argument("--remote-python", default="python")
    ap.add_argument("--provisioner-path", default=WIN_PROVISIONER)
    ap.add_argument("--ttl", type=int, default=DEFAULT_TTL, help="verifier window TTL seconds (30..900)")
    ap.add_argument("--opened-by-email", default="")
    ap.add_argument("--correlation-id", default="")
    a = ap.parse_args(argv)

    # Fail closed on an out-of-bounds TTL BEFORE any prompt or host/backend contact.
    if not (MIN_TTL <= a.ttl <= MAX_TTL):
        _fail("ttl_out_of_bounds", min=MIN_TTL, max=MAX_TTL, given=a.ttl)
        return 2

    print("account-46 STANDING ingest key - single-entry (key entered ONCE, delivered to host + backend).\n")

    # ---- PREFLIGHT (before the key is ever prompted). Any ambiguity refuses. ----
    sout, _serr, _src = _ssh(a.backend_ssh,
                             _backend_window_cmd_nostdin(a.backend_container, a.remote_python, "--status"))
    st = _last_json(sout)
    if st is None:
        _fail("window_status_unreadable")
        return 2
    if st.get("status", {}).get("open"):
        _fail("window_already_open")
        return 2
    state = _host_key_state(a)
    if state == "present_valid":
        _fail("standing_host_key_already_present")
        return 2
    if state != "absent":
        # present-but-invalid / task-contract drift / unreadable -> refuse (fail closed): never silently clobber.
        _fail("host_key_state_ambiguous")
        return 2

    # ---- One hidden entry (+ confirm). Held only in this process for the bounded operation. ----
    key = getpass.getpass("Enter the account-46 ingest key (hidden): ")
    if len(key) < MIN_LEN:
        key = None
        _fail("key_too_short", min=MIN_LEN)
        return 2
    # Reject control chars AND space (0x20): the backend command stores key.strip() while the host provisioner
    # keeps surrounding spaces verbatim, so a space-padded key would be stored divergently on the two sides
    # (-> every signed event bad_signature). An ingest key is hex/base64/token bytes; it never contains a space.
    if any(ord(c) < 33 or ord(c) > 126 for c in key):
        key = None
        _fail("key_not_printable_ascii_or_has_space")
        return 2
    key2 = getpass.getpass("Re-enter to confirm: ")
    if key != key2:
        key = key2 = None
        _fail("key_mismatch")
        return 2
    key2 = None

    # ---- Provision host + open window, with GUARANTEED cleanup on every non-success path. ----
    staged_host = False
    staged_window = False
    window_result = None
    acl_verified = False
    fail_reason = None
    foreign_window = False
    try:
        ok, acl_verified = _provision_host_key(a, key)
        if not ok:
            fail_reason = "host_provision_or_verify_failed"
        else:
            staged_host = True
            window_result, _raw, werr = _open_window(a, key)
            if (window_result and window_result.get("action") == "open"
                    and window_result.get("status", {}).get("open")):
                # Defence-in-depth: the window the backend actually opened MUST be INGEST + uncapped (the arm
                # refuses a finite cap). The args are hardcoded, so a mismatch means the backend changed/ignored
                # them -> treat as a failure and tear it down rather than leave a wrong window armed.
                if window_result.get("purpose") == "INGEST" and window_result.get("max_uses") is None:
                    staged_window = True
                else:
                    fail_reason = "window_unexpected_shape"
            else:
                fail_reason = "window_open_failed"
                # If the open was rejected because a window was ALREADY open (a concurrent operator opened one in
                # the preflight->open gap), we must NOT close THEIR window in cleanup; detect it on stderr.
                low = (werr or "").lower()
                if "window_already_open" in low or "already open" in low:
                    foreign_window = True
                    fail_reason = "window_already_open_concurrent"
    except KeyboardInterrupt:
        fail_reason = "interrupted"
    except Exception as exc:  # noqa: BLE001 - any ssh/runtime fault fails closed into cleanup, sanitised
        fail_reason = "ssh_or_runtime_error:" + type(exc).__name__
    finally:
        key = None   # never retain the key in this frame on any path

    if fail_reason is None and staged_host and staged_window:
        # SUCCESS: deliberately leave BOTH the standing host key and the OPEN uncapped INGEST window in place.
        print(json.dumps({
            "ok": True,
            "account_id": PINNED_ACCOUNT,
            "purpose": window_result.get("purpose"),
            "expires_at": window_result.get("expires_at"),
            "max_uses": window_result.get("max_uses"),
            "uncapped": window_result.get("max_uses") is None,
            "correlation_id": a.correlation_id or None,
            "host_key_provisioned": True,
            "host_acl_verified": bool(acl_verified),
            "window_open": True,
            "note": "standing host key + uncapped INGEST window left in place for the separately-gated arm; "
                    "no task/flag/arm enabled",
        }))
        return 0

    # ---- FAILURE: guaranteed, idempotent cleanup, then INDEPENDENT verification. A foreign concurrent window
    #      is deliberately NOT closed (we never clobber another operator's window); we always shred our own key. ----
    if not foreign_window:
        _close_window(a)
    remove_result = _shred_host_key(a)
    clean, detail = _verify_cleanup(a, remove_result, foreign_window=foreign_window)
    if clean:
        print(json.dumps({"ok": False, "reason": fail_reason or "unknown_failure",
                          "cleanup": "verified_dark", "detail": detail}))
        return 1
    print(json.dumps({
        "ok": False, "reason": fail_reason or "unknown_failure",
        "cleanup": "CLEANUP_INCOMPLETE", "detail": detail,
        "remediation": ("manually run, then re-verify: "
                        "ssh <backend> docker exec guvfx-backend python manage.py copier_verifier_window --close "
                        "; ssh <windows> powershell -File Provision-GuvfxCopierEmitterKey.ps1 -Remove "
                        "; confirm copier_verifier_window --status shows open:false AND -Verify shows the key "
                        "absent before any retry"),
    }))
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
