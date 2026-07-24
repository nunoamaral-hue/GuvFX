# 0015 — Unprivileged process observation for the beta per-slot agent

- Date: 2026-07-24
- Status: **Accepted** (Nuno, 2026-07-24 — "Choose Option 1 — Unprivileged enumeration in code. Do not
  broaden the GuvFXBetaAgent service account privileges at this stage.")
- Supersedes the observation mechanism established in WS-A (#199) for the slot-pool model. Related:
  [ADR 0014](0014-management-protocol-release-operation.md) (RELEASE depends on `observe_process`).

## Context — a capability proven in the wrong context

The beta agent runs as the **least-privilege** virtual account `NT SERVICE\GuvFXBetaAgent` (Session 0). Its
`observe_process` primitive underpins every lifecycle operation beyond MATERIALISE: VERIFY, STOP, TOMBSTONE
and RELEASE all ask "is a process running in this slot, and is it exactly this slot's runtime?".

WS-A (#199) fixed an `AttributeError` in that primitive and **verified it as an administrator**. On
2026-07-24, driving the ADR-0014 slot-1 RELEASE proof under the **actual service identity** exposed the gap:

| Context | `_enum_processes_with_owner` (WTS) | `observe_process(slot 1)` |
|---|---|---|
| Administrator | 207 processes | `absent` |
| `NT SERVICE\GuvFXBetaAgent` (deployed) | **denied** | **`process_observation_unavailable`** |

`WTSEnumerateProcesses(WTS_CURRENT_SERVER_HANDLE, …)` enumerates **all sessions** and is denied to a
low-privilege service account. Because it returned the owner SID *without opening any process*, the whole
scoping design depended on it — and it does not work in the deployed context. This is the same RULE-11 trap
as the `secedit` baseline: a negative result invisible until measured in the real context with a positive
control (admin observe works, 207 procs).

Confirming the diagnosis, `observe_process` run **as admin against the same on-disk code** returned
`absent`/`process_absent` — the code was correct; only the privilege context was wrong.

## Decision

Replace the WTS enumeration with an **unprivileged** mechanism that works from the least-privilege service
account, **without broadening the account's privileges** and **without a privileged broker**. The service
account remains least-privileged; inability to obtain mandatory evidence remains fail-closed.

### Canonical observation mechanism

1. **Enumerate PIDs with the Toolhelp snapshot** — `CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)` +
   `Process32FirstW` / `Process32NextW`. Unprivileged; opens no process; yields `(pid, name, ppid)` and
   **no owner SID** (identity is resolved per-candidate).
2. **Per-candidate resolution under the weakest access that works** — for each `terminal64.exe` candidate,
   open at `PROCESS_QUERY_LIMITED_INFORMATION` (never `PROCESS_ALL_ACCESS`) and resolve executable path
   (`QueryFullProcessImageNameW`), owner SID (token, `PROCESS_QUERY_INFORMATION`), session
   (`ProcessIdToSessionId`) and start time (`GetProcessTimes`).
3. **Match only when ALL mandatory evidence agrees** — exact slot executable **path** (prefix-safe: slot 1
   never matches slot 10), exact slot **identity SID**, and readable **start time**. **Never match by
   executable name alone** — the golden image is a copy of MT5, so the slot and the operator's terminal
   share the name; name only *scopes which processes are worth inspecting*.
4. **Session is EVIDENCE for openable candidates, and a discriminator ONLY for unopenable ones.** An
   openable candidate is matched (or excluded) on owner SID + path alone, **regardless of its session** — so
   a live slot runtime that lands in a surprise session can never be turned into a false `ABSENT` (the
   fail-open this ordering prevents). Session is used in exactly one place: to exclude an **unopenable**
   same-name candidate whose session is known and differs from the slot's expected (batch-logon = observer
   Session 0) one — i.e. the operator's interactive terminal, which the low-privilege account is denied a
   handle to. This is what lets an empty slot resolve to `ABSENT` (the operator's unopenable terminal is
   excluded by session) rather than `UNAVAILABLE`. A same/unknown-session unopenable candidate stays
   unresolved → `UNAVAILABLE`, never absence.

### Four fail-closed states (never conflated)

- `ABSENT` — no attributed process, and every plausible candidate was **positively** excluded by session or
  by evidence.
- `PRESENT` — exactly one fully-attributed slot process.
- `MULTIPLE_MATCHING_PROCESSES` — several fully-attributed slot processes; a **distinct** state, never a
  silent pick-one, never merged into `UNAVAILABLE`.
- `PROCESS_OBSERVATION_UNAVAILABLE` — a plausible in-slot candidate whose mandatory identity/path/start-time
  evidence could not be resolved (access denied, or token/path/time unreadable), or an enumeration failure.

**Access-denied or incomplete mandatory evidence is never read as absence and never silently skipped.** A
plausible in-slot candidate that cannot be attributed blocks the lifecycle (`process_attribution_incomplete`).
A snapshot that cannot be taken or fully walked raises (`process_snapshot_failed` / `_empty` /
`_iteration_failed`), never "no processes". PID reuse / a changed start time is not accepted as the prior
process (the start time is carried as identity evidence for the upstream birth/terminate comparison).

## What this does NOT authorise

- **No service privilege expansion.** The account is not granted any new right.
- **No privileged observation broker.**
- These remain a **fallback gate**: only if the unprivileged design is *conclusively proven impossible on
  the target host* may a precisely identified minimal Windows right, or a narrowly scoped privileged broker,
  be proposed — under a separate sponsor decision.

## Consequences

- The observe-dependent lifecycle (VERIFY/STOP/TOMBSTONE/RELEASE) can work under the least-privilege service
  identity, unblocking the slot-1 RELEASE proof.
- Owner SID now requires opening the process (Toolhelp carries no SID), so a candidate the account can open
  only at LIMITED — enough for the path — may still be `UNAVAILABLE` for want of the token SID. That is the
  correct fail-closed direction and is proven per-scenario on the host.
- The `PRESENT` path (opening the slot's own process, owned by a *different* low-privilege slot account) is
  the host-specific unknown; the `ABSENT` path (excluding the operator's terminal by session) is the first
  host milestone and does not require opening any slot process.

## Verification

Off-host: focused tests for every predicate separating `present` / `absent` / `unavailable` / `multiple`
(exact path, slot1-vs-slot10, case-insensitive path, production exclusion by session before any open,
denied-not-skipped, token-unreadable → unavailable, unknown-session handling, start-time-unreadable, snapshot
failure branches) plus source pins forbidding the WTS call and `PROCESS_ALL_ACCESS`. 650
`terminal_provisioning` tests + `make check` green.

Host (authoritative, OBS-7): prove `observe_process` under **`NT SERVICE\GuvFXBetaAgent`** (service context
authoritative) and as admin (diagnostic), across the required controls — absent / present / production
excluded / another-slot excluded / wrong-owner / wrong-path / inaccessible → unavailable / multiple → fail
closed / Session-0 observable / terminated → absent / PID-reuse rejected — each corroborated by independent
host observation. RELEASE does not resume until observation is proven under the real service identity.

## Reversal path

Additive to the win-layer primitive. Reverting = restoring the WTS enumeration — which re-introduces the
service-context blind this ADR exists to remove — so reversal is only meaningful together with a privilege
grant (the explicitly-declined Option 2).
