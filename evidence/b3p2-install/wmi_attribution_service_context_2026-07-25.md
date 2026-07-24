# B3P-2 — WMI full-attribution under the service identity: DECISION GATE = STOP (fields unavailable)

- Date: 2026-07-25
- Host: `WIN-RD8VDS93DK7`, Windows Server 2025.
- Task: before implementing WMI-based full attribution (to resolve the 4th blocker — the least-privilege
  service cannot `OpenProcess` a slot process owned by another account), PROVE the mandatory attribution
  fields (exact path, owner **SID**, session, creation time) are reliable under `NT SERVICE\GuvFXBetaAgent`
  for one controlled slot-1 MT5 process (Nuno, 2026-07-25, evidence-first).
- Method: launched the preserved slot-1 runtime via its approved launch task; queried WMI
  `Win32_Process` (bounded `WHERE Name='terminal64.exe'`) under BOTH Administrator (corroboration) and the
  actual service identity (a temporary reversible scheduled task registered to the service virtual account;
  `whoami` inside it confirmed `nt service\guvfxbetaagent`). Then terminated the launched process via the
  approved path-scoped terminate task. No privilege/ACL change; production and slot 1 untouched.

## Result — mandatory path + owner-SID are DENIED to the service

Two live `terminal64.exe`: production **pid 4336** (`C:\Program Files\IS6…`, Session 3, Administrator) and the
controlled slot-1 runtime **pid 15312** (`C:\GuvFX\beta\slots\1\terminal\terminal64.exe`, Session 0,
`guvfx_b_slot1`, `/portable`).

| WMI field | Administrator | **`NT SERVICE\GuvFXBetaAgent` (authoritative)** |
|---|---|---|
| `ProcessId` | ✓ | ✓ |
| `SessionId` | 3 / 0 | **3 / 0 ✓** |
| `CreationDate` | present | **present ✓** |
| `ParentProcessId` | present | **present ✓** |
| **`ExecutablePath`** | correct | **`null` ✗** |
| **`GetOwner`** | `WIN-…\Administrator` / `…\guvfx_b_slot1` | **`ReturnValue=2` (access denied), owner empty ✗** |
| **`GetOwnerSid`** | `…-500` / `…-1004` | **`ReturnValue=2` (access denied), SID `null` ✗** |
| `CommandLine` | present (`… /portable`) | **`null` ✗** |

`SessionId`, `CreationDate` and `ParentProcessId` are **system-table** reads and work unprivileged.
`ExecutablePath`, `GetOwner`, `GetOwnerSid` and `CommandLine` require **per-process access to the target**,
which WMI performs under the caller's token — so they are **denied to the least-privilege service for a
process owned by another account**, exactly as `OpenProcess` is (`process_attribution_incomplete`).

## Decision-gate outcome

**STOP — do not implement WMI full attribution.** The mandatory fields that FAILED under the service:
`ExecutablePath` (null), `GetOwner` (rc=2), `GetOwnerSid` (rc=2). WMI supplies only session + creation +
parent — insufficient to attribute the slot's own process by exact path + owner SID, and those must NOT be
combined with a name-based assumption.

## Conclusive ruling on unprivileged PRESENT attribution

Both documented unprivileged mechanisms are now **conclusively ruled out on the real host** for the exact
path + owner SID of a running cross-account slot process:

1. `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION/…_INFORMATION)` → denied (a low-priv account cannot open a
   process owned by another low-priv account).
2. WMI `Win32_Process` `ExecutablePath`/`GetOwner`/`GetOwnerSid` → denied (`rc=2`).

The root cause is the per-slot **least-privilege isolation itself**: the beta service (`GuvFXBetaAgent`) and
each slot identity (`guvfx_b_slot1`) are DISTINCT low-privilege accounts, and one cannot inspect the other's
process image/owner without access the default process DACL does not grant. `ABSENT` observation is
unaffected (an empty slot has no process to open; production is excluded by the WMI **session** — which does
work). Only **PRESENT** attribution of a running slot runtime is blocked.

## Fallback options (Red — architecture/security decision, Nuno's call)

Per the fallback gate (undocumented/privileged paths only after documented options are ruled out — now met):

1. **Narrowly-scoped process-access grant at launch (recommended).** Have the slot launch (a small wrapper
   running AS the slot identity, which owns the process) grant `NT SERVICE\GuvFXBetaAgent`
   `PROCESS_QUERY_LIMITED_INFORMATION` (+ token query for the SID) on the slot's OWN runtime process. This is
   symmetric with the existing filesystem ACL (the service already has `Modify` on the slot dirs): the
   manager gains **query-only** access to exactly the runtimes it manages, and nothing else system-wide. It
   is a launch-artefact change (wrapper + task/install update + re-APPLY), NOT a new Windows privilege.
2. **A narrowly-scoped privileged observation broker** the low-priv service calls to attribute a candidate.
   Heavier (new component + IPC + review; larger surface).
3. **Redefine the PRESENT mandatory evidence** to fields the service CAN obtain (session + name + expected
   parent PID + creation window) — weaker than exact-path + owner-SID and risks mis-attribution; requires an
   explicit governance decision to relax the isolation invariant. Not recommended.

## Boundary / safety

No ACL, privilege, production, slot-identity or slot-1 change was made. Production MT5 (pid 4336) and the
bridge (pid 13292) were untouched; the controlled slot-1 process was terminated via the approved path-scoped
terminate task; the diagnostic task and temp directory were removed; all 8 tasks returned to Disabled; slot 1
remains preserved (1212 files). Beta service Running the merged `2026-07-25.3` code.
