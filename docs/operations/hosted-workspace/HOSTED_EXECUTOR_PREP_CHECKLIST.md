# Hosted Executor — Sponsor preparation checklist (Stream 7D)

**Owner:** Sponsor provisions all production secrets + host prerequisites (secrets stay in Sponsor control).
**Then Claude runs:** install → service verify → disposable-host cert → Customer-Zero before/after → idempotency
→ rollback. **This doc contains NO secret values** — generation procedures, variable names, the WinSW hash, and
**presence-only** verification. Host = `100.79.101.19` (the Customer-Zero box). Backend = the Django container on
the VPS. Execution stays DARK until the cert step arms one disposable slot.

Key-id convention used below: HMAC key id `hx-1`, envelope key id `enc-1` (any stable ids work — they must MATCH
across host/backend as noted).

---

## 1. Generate the HMAC signing key (one shared secret)

A single random secret, shared by host + backend under the same key id.

```powershell
# any Python 3 with stdlib:
python -c "import secrets; print(secrets.token_hex(32))"
```

- The output (64 hex chars) is the HMAC secret. It goes into **both** `HOSTED_EXECUTOR_KEYRING` values (host and
  backend) under key id `hx-1`. Do not use a word like `changeme`/`example`/`placeholder` — the daemon rejects
  placeholder-looking secrets at boot.

## 2. Generate the envelope X25519 keypair (host = private, backend = public)

```powershell
C:\GuvFX\hosted\executor-venv\Scripts\python -c "from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey; import base64; k=X25519PrivateKey.generate(); print('PRIV', base64.b64encode(k.private_bytes_raw()).decode()); print('PUB', base64.b64encode(k.public_bytes_raw()).decode())"
```

- `PRIV` (base64) → the **host** keyring `HOSTED_EXECUTOR_ENC_PRIVKEYS` under key id `enc-1`.
- `PUB` (base64) → the **backend** keyring `HOSTED_EXECUTOR_ENC_PUBKEYS` under the same key id `enc-1`.
- The backend seals the Windows password to the public key; only the host (private key) can open it. The backend
  can never decrypt what it sealed.

## 3. Host machine environment variables (`100.79.101.19`, scope = Machine)

Set with PowerShell (handles JSON quotes cleanly — do **not** use `setx`, which truncates and mishandles quotes):

```powershell
[Environment]::SetEnvironmentVariable("HOSTED_EXECUTOR_KEYRING",       '{"hx-1":"<HMAC_SECRET>"}',      "Machine")
[Environment]::SetEnvironmentVariable("HOSTED_EXECUTOR_KEY_ID",        "hx-1",                          "Machine")
[Environment]::SetEnvironmentVariable("HOSTED_EXECUTOR_ENC_PRIVKEYS",  '{"enc-1":"<X25519_PRIV_B64>"}', "Machine")
[Environment]::SetEnvironmentVariable("HOSTED_EXECUTOR_BIND_HOST",     "100.79.101.19",                 "Machine")
# optional (defaults already correct): HOSTED_EXECUTOR_EXPECTED_BIND_HOST=100.79.101.19, HOSTED_EXECUTOR_BIND_PORT=8790
```

The WinSW service (`LocalSystem` per ADR-0040) inherits Machine env at start. Do **not** set
`HOSTED_HOST_EXECUTOR_ENABLED` here — arming is the cert step, for one disposable slot only.

## 4. Backend environment variables (VPS Django container env-file)

Add to the backend container's env-file (values typed by the Sponsor, on the VPS), then Claude recreates the
container during deploy:

```
HOSTED_EXECUTOR_KEYRING={"hx-1":"<SAME_HMAC_SECRET_AS_HOST>"}
HOSTED_EXECUTOR_KEY_ID=hx-1
HOSTED_EXECUTOR_ENC_PUBKEYS={"enc-1":"<X25519_PUB_B64>"}
HOSTED_EXECUTOR_ENC_KEY_ID=enc-1
HOSTED_EXECUTOR_BASE_URL=http://100.79.101.19:8790
```

`HOSTED_EXECUTOR_KEYRING`/`_KEY_ID` **must be byte-identical** to the host's (shared HMAC). `HOSTED_HOST_EXECUTOR_ENABLED`
stays unset until the cert arms the disposable slot.

## 5. WinSW binary / version / hash

- **WinSW v2.12.0, NET4 variant** — `WinSW.NET4.exe`.
- **SHA-256:** `923111c7142b3dc783a3c722b19b8a21bcb78222d7a136ac33f0ca8a29f4cb66` (the installer refuses any other).
- Place at: `C:\GuvFX\hosted\winsw-src\WinSW.NET4.exe`. (Same binary the beta agent uses — if already staged on
  the host for `GuvFXBetaAgent`, copy it there.)

## 6. Python venv

- Path: `C:\GuvFX\hosted\executor-venv` (so `C:\GuvFX\hosted\executor-venv\Scripts\python.exe` exists).
- Python **3.11** (match the estate).
- Package: **`cryptography`** (for the X25519 / AES-GCM envelope open). Everything else is stdlib.

```powershell
py -3.11 -m venv C:\GuvFX\hosted\executor-venv
C:\GuvFX\hosted\executor-venv\Scripts\python -m pip install --upgrade pip cryptography
```

## 7. Bundle staging locations (source → host)

Claude can stage these (non-secret code) during deploy; listed here for completeness. Sources are relative to
the repo at the merged commit; the daemon's `deploy/hosted-executor/stage-manifest.json` is authoritative.

| Host destination | Contents (source) |
|---|---|
| `C:\GuvFX\hosted\executor\` | `deploy/hosted-executor/{daemon,daemon_config,nonce_store,primitive_runner,envelope_open}.py` |
| `C:\GuvFX\hosted\executor\winsw\` | `deploy/hosted-executor/winsw/GuvFXHostedExecutor.xml` + `.supervised.xml` |
| `C:\GuvFX\hosted\executor\lib\` | `deploy/hosted-executor/lib/broker_cred_envelope.py` |
| `C:\GuvFX\hosted\executor\lib\hosted_workspace\` | `backend/hosted_workspace/{__init__,host_protocol,host_agent_dispatch}.py` |
| `C:\GuvFX\hosted\scripts\` | the 9 reviewed primitives from `backend/terminal_provisioning/windows/` (Provision-GuvfxAccount, Set-GuvfxWorkspaceAcl, Populate-GuvfxViewerRuntime, Set-GuvfxAutoTradingConfig, Grant-GuvfxRdpAccess, Set-GuvfxSingleSession, Set-GuvfxRemoteApp, Set-GuvfxObserver, Set-GuvfxAppLockerTenant) |

`C:\GuvFX\hosted\executor-winsw\` and `C:\GuvFX\hosted\executor-state\` are created by the installer.

## 8. Verification (presence + structural validity ONLY — never print a value)

**Host** (PowerShell) — prints booleans only:

```powershell
$m="Machine"
foreach($n in "HOSTED_EXECUTOR_KEYRING","HOSTED_EXECUTOR_KEY_ID","HOSTED_EXECUTOR_ENC_PRIVKEYS","HOSTED_EXECUTOR_BIND_HOST"){
  $present = [bool][Environment]::GetEnvironmentVariable($n,$m)
  Write-Host "$n present=$present"
}
# structural validity of the private keyring WITHOUT revealing it:
C:\GuvFX\hosted\executor-venv\Scripts\python -c "import os,json,base64; from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey; d=json.loads(os.environ['HOSTED_EXECUTOR_ENC_PRIVKEYS']); kid=next(iter(d)); X25519PrivateKey.from_private_bytes(base64.b64decode(d[kid])); k=json.loads(os.environ['HOSTED_EXECUTOR_KEYRING']); print('enc_key_id', kid, 'hmac_key_id_present', os.environ['HOSTED_EXECUTOR_KEY_ID'] in k, 'ok', True)"
# WinSW hash + venv + bundle:
(Get-FileHash C:\GuvFX\hosted\winsw-src\WinSW.NET4.exe -Algorithm SHA256).Hash.ToLower() -eq "923111c7142b3dc783a3c722b19b8a21bcb78222d7a136ac33f0ca8a29f4cb66"
C:\GuvFX\hosted\executor-venv\Scripts\python -c "import cryptography,http.server,sqlite3; print('venv ok')"
```

**Backend** (on the VPS, inside the container after recreate — presence only):

```bash
docker exec <backend-container> sh -c 'for n in HOSTED_EXECUTOR_KEYRING HOSTED_EXECUTOR_KEY_ID HOSTED_EXECUTOR_ENC_PUBKEYS HOSTED_EXECUTOR_ENC_KEY_ID HOSTED_EXECUTOR_BASE_URL; do echo "$n present=$([ -n \"$(printenv $n)\" ] && echo true || echo false)"; done'
```

End-to-end correctness (HMAC match + envelope keypair correspondence) is proven, without ever exposing a value,
at the first signed request during the cert: a mismatched HMAC → `bad_signature`; a mismatched envelope key →
the PROVISION step fails closed. That is the authoritative cross-side check.

## 9. What Claude does once you confirm §3–§8 are in place

1. Merge #345 (pre-flight) on green; build the backend image from `main` + recreate the container (picks up the
   per-op timeout + your backend env). Golden-reference STOP-check unaffected (no listener/execution change).
2. Stage the bundle (§7) if not already; `install_service.ps1 -InstallProfile Supervised` dry-run → review →
   `-Apply` (install-only, service STOPPED, identity `LocalSystem` per ADR-0040, rollback on any failure).
3. First-start gate: start the service; confirm `GET /hosted/health` from the backend over Tailscale; confirm
   exclusive bind + venv-python-under-WinSW.
4. Select a disposable **non-CZ** slot; positively prove disposability (read-only).
5. Capture the Customer-Zero BEFORE fingerprint (read-only).
6. Arm `HOSTED_HOST_EXECUTOR_ENABLED` + the slot-prep flags **for the disposable account only**; run the real
   `prepare_hosted_slot`; verify identity/G5-ACL/runtime/AutoTrading-capability/RemoteApp/single-session/AppLocker.
7. RULE-11 positive + negative controls; idempotency re-run; **Customer-Zero before/after STOP-check** (any
   drift → STOP); rollback the disposable slot.

Markers emitted only on evidence: `SIGNED_HOST_EXECUTOR_HOST_CERTIFIED`, `G5_WORKSPACE_ACL_HOST_CERTIFIED`,
`HOSTED_SLOT_PROVISIONING_CERTIFIED`, `HOSTED_SLOT_PROVISIONING_IDEMPOTENCY_CERTIFIED`. Never
`AUTONOMOUS_ONBOARDING_CERTIFIED`. Customer Zero is never provisioned; a trade is never placed.
