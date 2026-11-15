#!/usr/bin/env python3
"""GuvFX Copy Trading - account-46 one-shot attestation SINGLE-ENTRY operator wrapper
(GFX-COPIER-ONESHOT-SINGLE-ENTRY, ADR-0104/ADR-0101). Repository tooling: NOT installed, NOT scheduled, NOT run
by any agent. A human operator runs it in their own console during one Sponsor-gated attestation session.

WHY THIS EXISTS
---------------
The one-shot attestation needs the SAME ingest key in TWO places: the host-local key file (signed by the
emitter) and the backend bounded verifier window (which verifies the signature). Entering the key separately
into two interactive prompts is error-prone - a single-character divergence yields ``bad_signature`` and
consumes the one-use key. This wrapper takes the operator's key ONCE (hidden) and delivers the SAME bytes to
both, so they can never diverge.

SECURITY CONTRACT (fail closed)
-------------------------------
* The key is read ONCE from a hidden prompt (``getpass``), re-entered to confirm, and held only in this
  process. It is delivered to each destination over SSH **stdin only** - never argv, env, a file, a log line,
  shell history, chat, Git or Notion.
* Host side: the key is piped to the REVIEWED provisioner ``Provision-GuvfxCopierOneShotKey.ps1 -KeyFromStdin``
  (which performs the full account-46 ACL-restricted atomic write + task-contract validation), then the
  provisioner's own read-only ``-Verify`` must report ``ok:true`` (ACL verified) before proceeding.
* Backend side: the SAME key bytes are piped to ``copier_verifier_window --open --purpose attest --max-uses 1``.
* If provisioning or its ``-Verify`` fails, the window is NEVER opened. If opening the window fails after the
  key was provisioned, the wrapper shreds the host key (provisioner ``-Remove``) so nothing is left behind.
* Output is redacted JSON only (window metadata + provisioner ``ok`` booleans) - never the key.

The agent never runs this wrapper and never sees the key. After it reports success the window is OPEN and the
host key is in place; the agent (Role A) then triggers the one-shot attestation once and closes the window.
"""
from __future__ import annotations

import argparse
import getpass
import json
import subprocess
import sys

CONFIRM_PHRASE = "provision-key-account-46"   # the provisioner's fixed confirmation phrase (non-secret)
MIN_LEN = 32
WIN_PROVISIONER = r"C:\GuvFX\copier-emitter\operator\Provision-GuvfxCopierOneShotKey.ps1"


def _ssh(target, remote_argv, *, key_stdin=None, timeout=90):
    """Run `ssh <target> <remote_argv...>`; feed key_stdin (str) over stdin only. Returns (stdout, stderr, rc)."""
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", target] + remote_argv
    inp = key_stdin.encode("utf-8") if key_stdin is not None else None
    p = subprocess.run(cmd, input=inp, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    return (p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace"), p.returncode)


def _last_json(text):
    for line in reversed([l for l in text.splitlines() if l.strip()]):
        try:
            return json.loads(line)
        except Exception:
            continue
    return None


def _win_provision_cmd(provisioner_path, *args):
    return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", provisioner_path, *args]


def _backend_window_cmd(container, python_exe, *args):
    return ["docker", "exec", "-i", container, python_exe, "manage.py", "copier_verifier_window", *args]


def _backend_window_cmd_nostdin(container, python_exe, *args):
    return ["docker", "exec", container, python_exe, "manage.py", "copier_verifier_window", *args]


def main(argv=None):
    ap = argparse.ArgumentParser(description="account-46 one-shot attestation single-entry wrapper")
    ap.add_argument("--windows-ssh", required=True, help="ssh target for the Windows host (e.g. administrator@host or an ssh alias)")
    ap.add_argument("--backend-ssh", required=True, help="ssh target for the backend VPS (e.g. ubuntu@host)")
    ap.add_argument("--backend-container", default="guvfx-backend")
    ap.add_argument("--remote-python", default="python")
    ap.add_argument("--provisioner-path", default=WIN_PROVISIONER)
    ap.add_argument("--ttl", type=int, default=600, help="verifier window TTL seconds (<=900)")
    ap.add_argument("--opened-by-email", default="")
    ap.add_argument("--correlation-id", default="")
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

    # One hidden entry (+ confirm).
    key = getpass.getpass("Enter the account-46 ingest key (hidden): ")
    if len(key) < MIN_LEN:
        print(f"key too short (< {MIN_LEN}); aborting. Nothing changed.")
        return 2
    if any(ord(c) < 32 or ord(c) > 126 for c in key):
        # ASCII-only: a non-ASCII byte could be decoded differently by the host console vs the backend (UTF-8),
        # yielding a silent key divergence (bad_signature). Attestation keys are hex/base64 (ASCII) anyway.
        print("key must be printable ASCII (hex/base64 recommended); aborting.")
        return 2
    key2 = getpass.getpass("Re-enter to confirm: ")
    if key != key2:
        print("mismatch; aborting. Nothing changed.")
        key = key2 = None
        return 2
    key2 = None

    # --- Host: provision the key via the reviewed provisioner (-KeyFromStdin), removing any stale key first. ---
    _ssh(a.windows_ssh, _win_provision_cmd(a.provisioner_path, "-Remove"))  # idempotent; ignore result (fresh provision)
    pout, perr, prc = _ssh(a.windows_ssh, _win_provision_cmd(a.provisioner_path, "-KeyFromStdin"),
                           key_stdin=f"{CONFIRM_PHRASE}\n{key}")
    prov = _last_json(pout)
    if not prov or not prov.get("ok"):
        key = None
        print("PROVISION FAILED (redacted):", json.dumps(prov) if prov else (perr or pout).strip()[:200])
        print("Window NOT opened; nothing left behind.")
        return 1

    # Host: independent read-only ACL verification (no key read).
    vout, verr, vrc = _ssh(a.windows_ssh, _win_provision_cmd(a.provisioner_path, "-Verify"))
    ver = _last_json(vout)
    if not ver or not ver.get("ok") or not ver.get("acl_verified"):
        key = None
        _ssh(a.windows_ssh, _win_provision_cmd(a.provisioner_path, "-Remove"))  # shred - never leave an unverified key
        print("HOST -Verify FAILED (redacted):", json.dumps(ver) if ver else (verr or vout).strip()[:200])
        print("Host key shredded; window NOT opened.")
        return 1

    # --- Backend: open the bounded window with the SAME key bytes. ---
    win_args = ["--open", "--purpose", "attest", "--ttl", str(a.ttl), "--max-uses", "1"]
    if a.opened_by_email:
        win_args += ["--opened-by-email", a.opened_by_email]
    if a.correlation_id:
        win_args += ["--correlation-id", a.correlation_id]
    wout, werr, wrc = _ssh(a.backend_ssh, _backend_window_cmd(a.backend_container, a.remote_python, *win_args),
                           key_stdin=key)
    key = None
    win = _last_json(wout)
    if not win or win.get("action") != "open" or not win.get("status", {}).get("open"):
        # window failed after the key was provisioned - shred the host key so nothing is left behind.
        _ssh(a.windows_ssh, _win_provision_cmd(a.provisioner_path, "-Remove"))
        print("WINDOW OPEN FAILED (redacted):", json.dumps(win) if win else (werr or wout).strip()[:200])
        print("Host key shredded (nothing left open).")
        return 1

    print("\nSINGLE-ENTRY PROVISION + WINDOW OPEN OK (redacted):")
    print(json.dumps({
        "host_key_provisioned": True,
        "host_acl_verified": bool(ver.get("acl_verified")),
        "window_id": win.get("window_id"),
        "purpose": win.get("purpose"),
        "expires_at": win.get("expires_at"),
        "max_uses": win.get("max_uses"),
    }))
    print("\nReply:  DONE  (host key + window are in place; the agent triggers + verifies + closes).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
