#!/usr/bin/env python3
"""GuvFX Copy Trading - account-46 one-shot attestation SINGLE-ENTRY operator orchestration
(GFX-COPIER-ONESHOT-SINGLE-ENTRY + GFX-COPIER-ATTEST-TRANSPORT-RECONCILIATION-139, ADR-0104/ADR-0101/ADR-0077).
Repository tooling: NOT installed, NOT scheduled, NOT run by any agent. A human operator runs it in their own
console during one Sponsor-gated attestation session.

WHY THIS EXISTS
---------------
The one-shot attestation needs the SAME ingest key in TWO places: the host-local key file (signed by the
emitter) and the backend bounded verifier window (which verifies the signature). Entering the key separately
into two interactive prompts is error-prone - a single-character divergence yields ``bad_signature`` and
consumes the one-use key. This wrapper takes the operator's key ONCE (hidden) and delivers the SAME bytes to
both, so they can never diverge.

TRANSPORT RECONCILIATION (packet 139)
-------------------------------------
The host emitter sends its signed envelope to the attest-verify endpoint EXACTLY ONCE. A transient proxy /
network failure (connection dropped / HTTP 502 / no response) is AMBIGUOUS: the verifier may have processed
the request even though the HTTP response was lost. A blind retry is therefore FORBIDDEN (it could double-
process, and it wastes the one-use key). With ``--drive-attestation`` this orchestration instead, after an
AMBIGUOUS transport outcome, queries AUTHORITATIVE backend state over the existing operator SSH channel
(``copier_attest_reconcile``) and classifies:
  * PROCESSED_VERIFIED / PROCESSED_REJECTED -> report, NEVER retry;
  * NOT_PROCESSED (window still OPEN, uses==0, nonce not burned) -> reprovision the host key from the SAME
    in-memory bytes (no second human prompt) and retry EXACTLY ONCE;
  * AMBIGUOUS -> report unknown, NEVER retry.
A verifier HTTP VERDICT (200/400/401/403/409 with a recognised body) is never retried. At most ONE retry.

SECURITY CONTRACT (fail closed)
-------------------------------
* The key is read ONCE from a hidden prompt (``getpass``), re-entered to confirm, and held only in this
  process' memory for the bounded operation. It is delivered to each destination over SSH **stdin only** -
  never argv, env, a file, a log line, shell history, chat, Git or Notion. The attestation NONCE (used for
  reconciliation) is NOT a secret.
* Host side: the key is piped to the REVIEWED provisioner ``Provision-GuvfxCopierOneShotKey.ps1 -KeyFromStdin``
  (which performs the full account-46 ACL-restricted atomic write + task-contract validation), then the
  provisioner's own read-only ``-Verify`` must report ``ok:true`` (ACL verified) before proceeding.
* Backend side: the SAME key bytes are piped to ``copier_verifier_window --open --purpose attest --max-uses 1``.
* GUARANTEED TEARDOWN on every terminal path (success / rejection / unknown / exception): the in-memory key
  reference is dropped, the host key is shredded (provisioner ``-Remove``), and the verifier window is closed
  (``copier_verifier_window --close``). The one-shot TASK is re-disabled host-side by the signed trigger
  itself. Backend flags and the host emitter config gate are the agent's separate enable/teardown.
* Output is redacted JSON only - never the key, envelope, signature or broker login.
* NEVER contacts account #48 or any broker execution endpoint; attestation is identity-only.

Without ``--drive-attestation`` the wrapper behaves as before: it provisions the host key + opens the window,
then stops ("Reply DONE") for the agent (Role A) to trigger + verify + close.
"""
from __future__ import annotations

import argparse
import getpass
import json
import subprocess

CONFIRM_PHRASE = "provision-key-account-46"   # the provisioner's fixed confirmation phrase (non-secret)
MIN_LEN = 32
WIN_PROVISIONER = r"C:\GuvFX\copier-emitter\operator\Provision-GuvfxCopierOneShotKey.ps1"

OUTCOME_VERIFIED = "VERIFIED"
OUTCOME_REJECTED = "REJECTED"
OUTCOME_UNKNOWN = "UNKNOWN"
MAX_ATTEMPTS = 2   # the initial attempt + AT MOST ONE safe retry (only from a proven NOT_PROCESSED)


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
    Callers still apply their own gate (open-check / ok / acl_verified); a None result fails closed."""
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


def _backend_trigger_cmd(container, python_exe):
    # The one-shot trigger gate (``copier_emitter_oneshot_enabled``) is ANDed with the master gate; the master
    # gate is already on in the container env (the agent's compose override). We inject ONLY the one-shot sub-
    # flag for this single trigger process - never a persistent enablement. No secret is passed.
    return ["docker", "exec", "-e", "COPIER_EMITTER_ONESHOT_ENABLED=1", container, python_exe,
            "manage.py", "request_copier_emitter_oneshot_attest", "--apply"]


def _backend_reconcile_cmd(container, python_exe, nonce, correlation_id=""):
    args = ["copier_attest_reconcile", "--nonce", str(nonce)]
    if correlation_id:
        args += ["--correlation-id", str(correlation_id)]
    return ["docker", "exec", container, python_exe, "manage.py", *args]


def _normalise_trigger_result(top):
    """Extract the sanitised host attestation result from the trigger command's top-level JSON into a
    normalised dict {verified, reason, nonce, transport_ambiguous, http_status}.

    Three non-happy cases, distinguished so a VERIFIED-but-lost result is NEVER misreported as a rejection:
      * the trigger command's OWN output is lost/unparsable -> AMBIGUOUS (we cannot tell whether the host ran);
      * ``host_contacted`` is False (gate off / signed executor unavailable) -> DEFINITIVE setup failure, nothing
        was processed, transport_ambiguous=False (never retried, never reconciled);
      * the host WAS contacted but its ``result`` sub-dict is lost -> AMBIGUOUS (the host ran; reconcile from the
        window's ``uses`` since we have no nonce)."""
    if not isinstance(top, dict):
        return {"verified": False, "reason": "trigger_unparsable", "nonce": "",
                "transport_ambiguous": True, "http_status": None}
    res = top.get("result")
    if not isinstance(res, dict):
        if top.get("host_contacted") is False:
            return {"verified": False, "reason": str(top.get("reason") or "trigger_not_contacted"), "nonce": "",
                    "transport_ambiguous": False, "http_status": None}
        return {"verified": False, "reason": "trigger_result_lost", "nonce": "",
                "transport_ambiguous": True, "http_status": None}
    return {"verified": bool(res.get("verified")), "reason": str(res.get("reason") or ""),
            "nonce": str(res.get("nonce") or ""), "transport_ambiguous": bool(res.get("transport_ambiguous")),
            "http_status": res.get("http_status")}


def drive_attestation(*, trigger, reconcile, reprovision, out=None):
    """Drive the one-shot attestation with AUTHORITATIVE transport reconciliation and AT MOST ONE safe retry.

    All side effects are INJECTED callables so this decision core is pure and deterministically testable:
      * ``trigger() -> dict``        one trigger attempt -> normalised host result (see _normalise_trigger_result);
      * ``reconcile(nonce) -> dict`` authoritative ``copier_attest_reconcile`` classification for an ambiguous POST;
      * ``reprovision() -> bool``    re-write the host key from the SAME in-memory bytes; True on ok.

    Rules (exactly-once preserving, fail-closed):
      * a VERIFIED result -> VERIFIED (no retry);
      * a DEFINITIVE non-verified result (transport_ambiguous=False: a verifier verdict or a setup failure)
        -> REJECTED (NEVER retried - we never retry an HTTP verdict);
      * an AMBIGUOUS transport outcome -> reconcile:
          - PROCESSED_VERIFIED -> VERIFIED;  PROCESSED_REJECTED -> REJECTED  (no retry);
          - NOT_PROCESSED (proven safe) -> reprovision same key + retry EXACTLY once;
          - AMBIGUOUS / NOT_PROCESSED-after-the-retry -> UNKNOWN (no further retry).
    Returns {outcome in {VERIFIED,REJECTED,UNKNOWN}, reason, attempts, classification?}. Never contacts #48."""
    log = out or (lambda *a, **k: None)
    attempt = 0
    last_class = ""
    while attempt < MAX_ATTEMPTS:
        attempt += 1
        res = trigger() or {}
        if res.get("verified"):
            return {"outcome": OUTCOME_VERIFIED, "reason": "verified", "attempts": attempt}
        if not res.get("transport_ambiguous"):
            # a DEFINITIVE backend verdict (rejected / disabled / unavailable / ...) or a setup failure.
            return {"outcome": OUTCOME_REJECTED, "reason": str(res.get("reason") or "rejected"),
                    "attempts": attempt}
        # An AMBIGUOUS transport outcome is ALWAYS reconciled from authoritative backend state (the reconcile
        # command scopes by correlation id, so an empty nonce - a lost RESULT - is handled window-based and is
        # never a blind retry and never a false VERIFIED).
        nonce = str(res.get("nonce") or "")
        rc = reconcile(nonce) or {}
        cls = str(rc.get("classification") or "AMBIGUOUS")
        last_class = cls
        if cls == "PROCESSED_VERIFIED":
            return {"outcome": OUTCOME_VERIFIED, "reason": "verified_reconciled", "attempts": attempt,
                    "classification": cls}
        if cls == "PROCESSED_REJECTED":
            return {"outcome": OUTCOME_REJECTED, "reason": "rejected_reconciled", "attempts": attempt,
                    "classification": cls}
        if cls == "PROCESSED_INDETERMINATE":
            # processed (uses>=1) but the verdict is unreadable (lost result, no nonce): NEVER retry a processed
            # request; report UNKNOWN so the operator checks the backend rather than re-running a fresh session.
            return {"outcome": OUTCOME_UNKNOWN, "reason": "processed_verdict_indeterminate", "attempts": attempt,
                    "classification": cls}
        if cls == "NOT_PROCESSED" and attempt < MAX_ATTEMPTS:
            log("transport ambiguous + NOT_PROCESSED proven -> reprovisioning same key + retrying exactly once.")
            if not reprovision():
                return {"outcome": OUTCOME_UNKNOWN, "reason": "reprovision_failed", "attempts": attempt,
                        "classification": cls}
            continue   # the SINGLE safe retry
        # NOT_PROCESSED on the final attempt, or AMBIGUOUS -> stop; no (further) retry.
        return {"outcome": OUTCOME_UNKNOWN,
                "reason": ("not_processed_exhausted" if cls == "NOT_PROCESSED" else "ambiguous"),
                "attempts": attempt, "classification": cls}
    return {"outcome": OUTCOME_UNKNOWN, "reason": "max_attempts", "attempts": attempt, "classification": last_class}


def _provision_host_key(a, key):
    """Provision the host-local ingest key from the in-memory bytes via the reviewed provisioner, then verify
    its ACL (read-only). Removes any stale key first. Returns (ok: bool, acl_verified: bool). Never prints the
    key. On a verify failure the unverified key is shredded before returning."""
    _ssh(a.windows_ssh, _win_provision_cmd(a.provisioner_path, "-Remove"))  # idempotent; fresh provision
    pout, perr, prc = _ssh(a.windows_ssh, _win_provision_cmd(a.provisioner_path, "-KeyFromStdin"),
                           key_stdin="%s\n%s" % (CONFIRM_PHRASE, key))
    prov = _last_json(pout)
    if not prov or not prov.get("ok"):
        return False, False
    vout, verr, vrc = _ssh(a.windows_ssh, _win_provision_cmd(a.provisioner_path, "-Verify"))
    ver = _last_json(vout)
    if not ver or not ver.get("ok") or not ver.get("acl_verified"):
        _ssh(a.windows_ssh, _win_provision_cmd(a.provisioner_path, "-Remove"))   # shred unverified
        return False, False
    return True, bool(ver.get("acl_verified"))


def _shred_host_key(a):
    """Idempotent host key shred (provisioner -Remove). Safe in a guaranteed-cleanup finally; handles no key."""
    try:
        _ssh(a.windows_ssh, _win_provision_cmd(a.provisioner_path, "-Remove"))
    except Exception:  # noqa: BLE001 - cleanup must never raise
        pass


def _close_window(a):
    """Idempotent verifier-window close (shreds the backend tmpfs key). Safe in a guaranteed-cleanup finally."""
    try:
        _ssh(a.backend_ssh, _backend_window_cmd_nostdin(a.backend_container, a.remote_python, "--close"))
    except Exception:  # noqa: BLE001 - cleanup must never raise
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="account-46 one-shot attestation single-entry orchestration")
    ap.add_argument("--windows-ssh", required=True, help="ssh target for the Windows host (e.g. administrator@host or an ssh alias)")
    ap.add_argument("--backend-ssh", required=True, help="ssh target for the backend VPS (e.g. ubuntu@host)")
    ap.add_argument("--backend-container", default="guvfx-backend")
    ap.add_argument("--remote-python", default="python")
    ap.add_argument("--provisioner-path", default=WIN_PROVISIONER)
    ap.add_argument("--ttl", type=int, default=600, help="verifier window TTL seconds (<=900)")
    ap.add_argument("--opened-by-email", default="")
    ap.add_argument("--correlation-id", default="")
    ap.add_argument("--drive-attestation", action="store_true",
                    help="After opening the window, DRIVE the attestation (trigger + authoritative reconciliation "
                         "+ at most one safe retry) and GUARANTEE teardown. Default: open + stop (agent triggers).")
    a = ap.parse_args(argv)

    print("account-46 one-shot attestation - single-entry (key entered ONCE, delivered to host + backend).\n")

    # Preflight: refuse if a verifier window is already open (single-flight).
    out, err, rc = _ssh(a.backend_ssh, _backend_window_cmd_nostdin(a.backend_container, a.remote_python, "--status"))
    st = _last_json(out)
    if st is None:
        print("REFUSING: could not read backend verifier-window status.", (err or out).strip()[:200])
        return 2
    if st.get("status", {}).get("open"):
        print("REFUSING: a verifier window is already OPEN. Close it first.")
        return 2

    # One hidden entry (+ confirm). Held only in this process for the bounded operation.
    key = getpass.getpass("Enter the account-46 ingest key (hidden): ")
    if len(key) < MIN_LEN:
        print("key too short (< %d); aborting. Nothing changed." % MIN_LEN)
        return 2
    if any(ord(c) < 32 or ord(c) > 126 for c in key):
        print("key must be printable ASCII (hex/base64 recommended); aborting.")
        return 2
    key2 = getpass.getpass("Re-enter to confirm: ")
    if key != key2:
        print("mismatch; aborting. Nothing changed.")
        key = key2 = None
        return 2
    key2 = None

    # The driven flow generates a correlation id when none is given, so reconciliation can scope the window
    # even when the trigger RESULT (and thus the nonce) is lost (see _normalise_trigger_result / reconcile).
    if a.drive_attestation and not a.correlation_id:
        import uuid
        a.correlation_id = "attest-drive-" + uuid.uuid4().hex[:16]

    def _open_window(k):
        win_args = ["--open", "--purpose", "attest", "--ttl", str(a.ttl), "--max-uses", "1"]
        if a.opened_by_email:
            win_args += ["--opened-by-email", a.opened_by_email]
        if a.correlation_id:
            win_args += ["--correlation-id", a.correlation_id]
        wout, werr, wrc = _ssh(a.backend_ssh, _backend_window_cmd(a.backend_container, a.remote_python, *win_args),
                               key_stdin=k)
        return _last_json(wout), (werr or wout)

    if not a.drive_attestation:
        # --- Legacy / agent-triggers mode: provision + open + stop; the agent triggers + verifies + closes. ---
        ok, acl_verified = _provision_host_key(a, key)
        if not ok:
            key = None
            print("PROVISION or -Verify FAILED (redacted). Window NOT opened; host key shredded; nothing left behind.")
            return 1
        win, raw = _open_window(key)
        key = None
        if not win or win.get("action") != "open" or not win.get("status", {}).get("open"):
            _ssh(a.windows_ssh, _win_provision_cmd(a.provisioner_path, "-Remove"))   # shred host key
            print("WINDOW OPEN FAILED (redacted):", json.dumps(win) if win else raw.strip()[:200])
            print("Host key shredded (nothing left open).")
            return 1
        print("\nSINGLE-ENTRY PROVISION + WINDOW OPEN OK (redacted):")
        print(json.dumps({"host_key_provisioned": True, "host_acl_verified": bool(acl_verified),
                          "window_id": win.get("window_id"), "purpose": win.get("purpose"),
                          "expires_at": win.get("expires_at"), "max_uses": win.get("max_uses")}))
        print("\nReply:  DONE  (host key + window are in place; the agent triggers + verifies + closes).")
        return 0

    # --- Driven mode: provision + open + drive, ALL under a GUARANTEED-teardown finally (every path, INCLUDING
    #     an exception during provision or window-open). ---
    outcome = None
    try:
        ok, acl_verified = _provision_host_key(a, key)
        if not ok:
            print("PROVISION or -Verify FAILED (redacted). Window NOT opened.")
            return 1
        win, raw = _open_window(key)
        if not win or win.get("action") != "open" or not win.get("status", {}).get("open"):
            print("WINDOW OPEN FAILED (redacted):", json.dumps(win) if win else raw.strip()[:200])
            return 1
        print("\nSINGLE-ENTRY PROVISION + WINDOW OPEN OK; driving attestation (reconciled, >=1 safe retry) ...")
        outcome = drive_attestation(
            trigger=lambda: _normalise_trigger_result(_last_json(_ssh(
                a.backend_ssh, _backend_trigger_cmd(a.backend_container, a.remote_python), timeout=150)[0])),
            reconcile=lambda nonce: _last_json(_ssh(
                a.backend_ssh, _backend_reconcile_cmd(a.backend_container, a.remote_python, nonce,
                                                      a.correlation_id))[0]) or {},
            reprovision=lambda: _provision_host_key(a, key)[0],
            out=print)
    finally:
        # GUARANTEED teardown on EVERY path (incl. a provision / window-open / drive exception): drop the
        # in-memory key reference, shred the host key, close the verifier window. All idempotent + best-effort.
        key = None
        _shred_host_key(a)
        _close_window(a)

    print("\nATTESTATION OUTCOME (redacted):")
    print(json.dumps(outcome or {"outcome": OUTCOME_UNKNOWN, "reason": "no_outcome"}))
    return 0 if (outcome and outcome.get("outcome") == OUTCOME_VERIFIED) else 1


if __name__ == "__main__":
    raise SystemExit(main())
