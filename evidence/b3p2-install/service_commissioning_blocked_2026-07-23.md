# B3P — service commissioning STOPPED before Phase A completes

`WIN-RD8VDS93DK7`, `2026-07-23T21:2x Z`, main `873cf126`. **Read-only throughout. No service was installed,
no firewall rule added, nothing started.** Four preconditions fail. Each needs a sponsor decision because
each requires a host change outside this packet's authorised scope.

## Repository and artefact gate — PASS

| Check | Result |
|---|---|
| main SHA | `873cf1269df4016ddcb975f7c4ee07a3c99357db` |
| main CI | 5/5 green |
| staged bundle == main | 26/26 exact |
| manifest | `2026-07-23.2`, 15 modules, 0 problems |
| PowerShell 5.1 parse | 4/4, 0 errors, 0 non-ASCII |
| duplicate installer files | none (two stale `.next` build artefacts removed; not installer files) |
| commissioning changes outside main | none |

---

## BLOCKER 1 — `C:\GuvFX\python311.exe` is the Python *installer*, not an interpreter

`install_service.ps1` line 12 defaults `-Python` to it and installs the service to run it.

```
size=26216840  FileDescription = [Python 3.11.9 (64-bit)]
OriginalFilename= [python-3.11.9-amd64.exe]      <- the redistributable INSTALLER
signature=Valid  signer=CN=Python Software Foundation
```

`--version` produces no output and leaves `$LASTEXITCODE` unset, because the bootstrapper detaches.

## BLOCKER 2 — the preflight EXECUTES the candidate, before any `-Apply` gate

`install_service.ps1` lines 32-35 run unconditionally, in PLAN as well as APPLY:

```powershell
if (-not (Test-Path $Python)) { throw "interpreter not found: $Python" }
& $Python -c "import win32serviceutil" 2>$null
if ($LASTEXITCODE -ne 0) { throw "pywin32 not importable by $Python - ..." }
```

Against an installer binary that means **launching an installer on the operator's production host** — from
a dry run. `Test-Path` cannot tell an interpreter from an installer, and the exit-code check cannot either:
`$LASTEXITCODE` stays `$null`, and `$null -ne 0` is true, so the throw fires — with a misleading message,
*after* the installer has already been launched.

**Deviation, mine.** My probes ran that binary twice while identifying it, spawning two `python311.exe`
processes. Evidence captured before remediation, and no remediation proved necessary — they exited on
their own:

```
python311.exe processes now: none
C:\Program Files\Python311 LastWrite = 03/21/2026 10:30:04   (unchanged - nothing installed)
msiexec running: 0
estate: terminal64 pid=4336 Session 3 | bridge python pid=13292 owns 8788   (unchanged)
```

A preflight must not be able to launch an installer. Identify the interpreter by metadata
(`OriginalFilename`, `FileDescription`) *before* executing it.

## BLOCKER 3 — no interpreter on the host has pywin32, and the only 3.11 is the live bridge's

```
C:\Program Files\Python311\python.exe                  3.11.9    pywin32=no   <- runs the LIVE BRIDGE pid 13292
C:\Users\...\Python313\python.exe                      3.13.14   pywin32=no
```

The pywin32 service wrapper cannot be installed. Installing pywin32 into `C:\Program Files\Python311` would
modify **the interpreter the live trading bridge is running on** — production mutation, and not authorised.

**Recommended:** a dedicated virtual environment, e.g. `C:\GuvFX\beta\agent-venv`, created from the 3.11.9
interpreter and given its own `pywin32`. `python -m venv` does not modify the base interpreter and `pip`
writes only inside the venv, so the bridge is untouched. `install_service.ps1` already accepts `-Python`.
Needs: a sponsor decision, network access or an offline wheel, and a repo fix to the wrong default.

## BLOCKER 4 — firewall: no default-deny inbound, so the scoped allow would be meaningless

`firewall.ps1` PLAN **refused, correctly**:

```
firewall.ps1: profile 'Private' DefaultInboundAction is 'NotConfigured', expected 'Block'
- the scoped allow is not safe without default-deny inbound
```

```
Domain   Enabled=True  DefaultInboundAction=NotConfigured
Private  Enabled=True  DefaultInboundAction=NotConfigured     <- Tailscale is Private
Public   Enabled=True  DefaultInboundAction=NotConfigured     <- Ethernet is Public
114 enabled inbound allow rules, incl. RDP 3389 (profile Any), Tailscale, winvnc.exe
GuvFX-Beta-Agent-In: ABSENT (as expected)
```

Setting `Private -> Block` is a machine-wide posture change affecting the whole estate — explicitly
**not authorised** by this packet.

### And it carries a live-outage risk that is currently UNPROVEN

**No enabled inbound allow rule covers the bridge.** Searching every enabled inbound allow rule for port
`8788` or a python/GuvFX program returned exactly one match, and it is not the bridge:

```
MetaTrader 5 Strategy Tester Agent | ports=Any | program=C:\GuvFX\golden\newMT5\metatester64.exe
```

The bridge listens on `0.0.0.0:8788` (pid 13292) with **no rule of its own**, yet the VPS reaches it today.
The likely explanation is that Tailscale delivers over loopback, which Windows Firewall does not filter —
**but that is a hypothesis, not a measurement.** If it is wrong, switching Private to default-deny would
cut the live trading bridge.

Per RULE 11 this must not be assumed in either direction. The safe order is: prove the delivery path, or
add an explicit allow for the bridge **first**, and only then change the profile default.

### Incidental finding

`MetaTrader 5 Strategy Tester Agent` is an inbound allow (Domain+Private, any port) scoped to
`C:\GuvFX\golden\newMT5\metatester64.exe` — inside the approved **golden image**, created when that MT5 was
installed. The golden image is never executed (slots run copies at different paths, which the rule would
not match), so exposure is not active. Worth removing or narrowing as hygiene.

## BLOCKER 5 — the service has no configuration, and one value is a secret

All twelve `BETA_AGENT_*` machine environment variables are unset, so `load_config` would raise and the
service could not start:

```
BETA_AGENT_BIND_HOST / EXPECTED_BIND_HOST / BIND_PORT          <NOT SET>
BETA_AGENT_EXECUTION_MODEL / SLOT_POOL_SIZE                    <NOT SET>
BETA_AGENT_GOLDEN_DIR / GOLDEN_DIGEST / GOLDEN_MANIFEST_VERSION <NOT SET>
BETA_AGENT_APPROVED_TASKS / DRAIN_TIMEOUT_S                    <NOT SET>
BETA_AGENT_KEY_ID / BETA_AGENT_KEYRING                         <NOT SET>
```

Eleven are non-secret configuration whose approved values are already recorded in
`deploy/beta-agent/config.example.json`. **`BETA_AGENT_KEYRING` is a signing secret** — the operator's to
provision, exactly like the four slot passwords. I did not read, request, generate or set it, and the probe
above deliberately reported only whether it is set, never its value.

---

## State on the host — unchanged

```
beta service        ABSENT
GuvFX-Beta-Agent-In ABSENT
beta identities     guvfx_b_slot1..4, Users only
8 beta tasks        Disabled, 0 triggers, never run (267011)
slots staged        0 of 4
beta terminal64     0
production MT5      pid 4336, Session 3      (unchanged)
bridge              pid 13292, owns 8788     (unchanged)
```

## Not done, and deliberately

No service installed. No firewall rule added or profile changed. No package installed. No environment
variable set. No interpreter created. Nothing started. `install_service.ps1` was **not** run in PLAN mode,
because its preflight would execute the installer binary again (BLOCKER 2).
