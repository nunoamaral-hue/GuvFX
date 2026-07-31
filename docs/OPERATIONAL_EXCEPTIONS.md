# Operational Exceptions

Time-bounded, Sponsor-approved deviations from the target-state hygiene, each with an explicit closure
path. An exception is not a permanent decision — it records *why* a known-imperfect state is tolerated
now and *what* must happen to close it. PM owns the lifecycle status.

---

## OE-1 — Residual global pywin32 DLLs on the beta host (accept & document, temporary)

- **Status:** OPEN — accepted, time-bounded (Sponsor programme decision, 2026-07-30).
- **Host:** `WIN-RD8VDS93DK7` @ `100.79.101.19`.
- **What:** three residual global pywin32 DLLs left by the 2026-07-24 failed pywin32 service install —
  `C:\Windows\System32\pywintypes311.dll`, `C:\Windows\System32\pythoncom311.dll` (created 05:38, pywin32
  3.11.312.0, owner Administrators), and `C:\Program Files\Python311\pywintypes311.dll` (created 06:35).
  (`C:\Program Files\Python311\pythoncom311.dll` is absent.)
- **Why it is safe to accept now (read-only evidence, 2026-07-30):**
  - Loaded by **zero** of 215 running processes (full scan, 0 access-denied, with a working positive control —
    the probe *did* find pywin32 loaded elsewhere). The only pywin32 loader, the beta-agent (pid 13532), loads
    it **venv-local** (`…\agent-venv\Lib\site-packages\pywin32_system32\…`), never the global copies.
  - The live bridge (pid 14604, base `C:\Program Files\Python311\python.exe`, `:8788`) loads **no** pywin32.
  - No service or scheduled task depends on the base Python that contains them.
  - They are residual contamination, not the cause of the Customer-Zero provisioning stall, and are **not**
    required for Phase-2/3 service re-certification (the remediated installer neither creates nor needs them,
    and measures that its own run adds none).
- **Do NOT** remove, quarantine, or otherwise mutate these files during the Customer-Zero critical path.
- **Closure path (all reversible, scheduled estate-hygiene window, gated):**
  1. Confirm the backtest agent (`:8787`, `metatester64.exe`) dependency **when it is next running** — verify
     no base-interpreter pywin32 import (the one case not observable at audit time, since `:8787` was idle).
  2. Quarantine the three files reversibly (move to a timestamped backup, not delete).
  3. Verify host health (bridge `:8788`, MT5, beta-agent unaffected).
  4. Delete only after an agreed soak period.
- **Evidence:** session read-only investigation 2026-07-30; see `evidence/beta-agent-phase3-cert/` and the
  auto-memory `project-b3p2-install-gate`.
