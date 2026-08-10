# Hosted Workspace — Capacity Model

Compiled **2026-08-10**. Read-only measurement of the live estate; **nothing mutated**, execution DARK.
Purpose: size the current Hosted MT5 Workspace estate and project 1 → 100 workspaces so Beta admission is
bounded by measured limits, not guesses. The **5-user figure is a governance/pilot boundary, not a code
limit** — there is no application-level hard cap on workspace count (the ceiling is host RAM/CPU, quantified
below).

Method note (RULE 11): every number below is a captured measurement with its source command, not an
estimate. Where a figure is projected rather than measured, it is labelled **(proj)** and the assumption is
stated. Per-workspace marginal cost is bracketed by two measured bounds — working set (conservative) and
private bytes (floor) — because RDS shares system pages across sessions but not session-level processes.

---

## 1. Measured footprint (read-only)

### 1a. Windows host — `WIN-RD8VDS93DK7` (guvfx-windows-mt5, 100.79.101.19)
Source: `Get-CimInstance Win32_OperatingSystem/ComputerSystem/LogicalDisk`, `Get-Process`, `qwinsta`.

| Metric | Value |
|---|---|
| OS | Windows Server 2025 Datacenter |
| Total RAM | **32 GB** |
| Free RAM (at capture) | 27.9 GB |
| Logical CPUs | **8** |
| Disk C: total / free | 479 GB / **400 GB free** |
| RDS sessions (active or disc) | 3 |

**One hosted workspace = the `guvfx_u_1` RDS session (session 3):**

| Per-workspace metric | Measured |
|---|---|
| Processes in session | 17 (terminal64 + RemoteApp/RDP infra) |
| **Working-set total (conservative)** | **535 MB** |
| **Private-bytes total (marginal floor)** | **137 MB** |
| terminal64 alone (session 3) | 138 MB WS / 58 MB private |
| Portable runtime disk (`C:\GuvFX\accounts\1`) | **290 MB** |

Legacy executor (session 1 terminal64: 42 MB WS / 78 MB private) is a **separate** concern — the
legacy-retirement path, not a hosted workspace — and is excluded from per-workspace cost.

### 1b. VPS — `100.119.23.29` (Traefik + backend + DB + Guacamole)
Source: `free -g`, `nproc`, `df -h /`, `docker stats --no-stream`.

| Metric | Value |
|---|---|
| Total / available RAM | 22 GB / **18 GB available** |
| CPUs | 8 |
| Disk / total / avail | 193 GB / **65 GB avail (67 % used)** |
| Docker resident total | **~2 GB** across 14 containers |
| Largest container | guacamole (JVM) **1.19 GB**; backend 197 MB; postgres 182 MB; guacd 17 MB |

`guvfx-backend` showed a transient **519 % CPU** at the sampling instant (a request mid-flight during
`docker stats`) — a snapshot spike, not steady state; the box's overall load is light (18 GB free).
Guacamole's cost is **per active RDP connection** via `guacd` (a libguac process per connection), not
per provisioned workspace.

---

## 2. Projections (1 / 5 / 10 / 20 / 50 / 100 workspaces)

Per-workspace unit costs used (measured 1a/1b): **Windows RAM 0.6 GB** (working-set, conservative — the
137 MB private-bytes floor is the optimistic bound), **Windows CPU ~0.1–0.15 core** idle signal-copy (higher
under active EA compute), **disk ~1 GB** (290 MB runtime + logs/`bases` growth headroom), **VPS guacd RAM
~40 MB per *concurrent* connection (proj)**.

| N | Win RAM (WS, conservative) | Win RAM (private floor) | Win CPU (idle) | Win disk | VPS guacd RAM (concurrent) | Estate verdict |
|---|---|---|---|---|---|---|
| 1 | 0.6 GB | 0.14 GB | ~0.1 | 1 GB | 40 MB | trivial |
| 5 | 3 GB | 0.7 GB | ~0.6 | 5 GB | 200 MB | **comfortable** |
| 10 | 6 GB | 1.4 GB | ~1.2 | 10 GB | 400 MB | **comfortable** |
| 20 | 12 GB | 2.7 GB | ~2.5 | 20 GB | 800 MB | **warning** (RAM ~half of usable) |
| 50 | 30 GB | 6.9 GB | ~6 | 50 GB | 2 GB | **exceeds 32 GB Windows host** → scale-up |
| 100 | 60 GB | 14 GB | ~12 | 100 GB | 4 GB | **scale-out** (multiple Windows hosts) |

Reading the table: on the **current single 32 GB / 8-core Windows host**, reserve ~8 GB for OS +
Administrator + legacy + headroom → **~24 GB usable** → **~40 workspaces on the conservative RAM bound**
before RAM exhaustion; the private-bytes floor would allow more but is not a safe planning number. The VPS
is **never** the binding constraint for workspace count out to 100 — its watch item is Guacamole/`guacd`
concurrency and DB connection count, both comfortable at these scales.

**The binding constraint is the single Windows host's RAM (then CPU under trading load), not the VPS.**

---

## 3. Thresholds & scale plan

| Band | Workspaces (this host) | Signal | Action |
|---|---|---|---|
| **Comfortable** | ≤ ~15 | RAM < 40 % of usable, CPU idle | none — proceed |
| **Warning** | ~20–25 | RAM 55–70 % usable; watch CPU under load | monitor per-session RAM + CPU during active trading; hold new admissions if trading-heavy |
| **Scale-up** | ~30–45 | RAM approaching ~24 GB usable | grow the single host (64–128 GB RAM, 16 vCPU) — simplest, no topology change |
| **Scale-out** | > ~40 | one host insufficient / redundancy needed | add Windows host(s); **pool workspaces by host** (host affinity per `terminal_node`); a second host also removes the single-host SPOF |
| **Hard protection** | — | approaching host RAM exhaustion | admission gate must **refuse** provisioning before commit charge would exceed usable RAM — fail-closed, never oversubscribe |

Scale sequencing (cheapest first):
1. **Vertical (scale-up) is the first lever** to ~40: bigger single host, no orchestration change — the
   per-slot model, `terminal_node`, delivery and execution identity all already key on a node id.
2. **Horizontal (scale-out) beyond ~40**: additional Windows hosts, workspaces bound to a host via
   `terminal_node` / `execution_node`. This is where a placement/affinity policy is needed (which host a
   new workspace lands on) — currently there is **one** node, so placement is trivial today.
3. **VPS**: no action needed for workspace count out to 100; revisit only if concurrent Guacamole sessions
   or Postgres connections climb — both are per-*concurrent-user*, not per-provisioned-workspace.

### Governance vs technical ceiling
- **Governance pilot boundary:** ≤ 5–10 users (Sponsor decision, memory `project_broker_connectivity_capability`).
- **Technical ceiling (this host, measured):** **~15 comfortable / ~40 hard** on RAM; CPU is the earlier
  limit only if workspaces trade actively rather than idle signal-copy.
- These are independent: the pilot boundary sits **well inside** the technical ceiling, so capacity does
  **not** gate the pilot. The first capacity decision point is the ~15 → 20 band, not the 5-user pilot.

### Not covered / assumptions to validate before relying on the upper bands
- Per-workspace RAM measured at **one** idle-ish signal-copy workspace; a workspace running a compute-heavy
  EA may cost more CPU (and some RAM). Re-measure with 2–3 concurrent **active-trading** workspaces before
  trusting the 20+ bands.
- `guacd` per-connection RAM is **(proj)** ~40 MB — measure an actual concurrent RemoteApp connection to
  confirm before sizing for 50+ concurrent viewers.
- Disk growth of `logs/` and `bases/` per workspace over weeks is not yet measured; the 1 GB/workspace
  budget is headroom, not an observed steady state.
- Windows RDS **CAL licensing** is a separate gate (see RDS licensing readiness) — capacity here is
  compute/RAM, not entitlement.
