# Hosted AutoTrading — Certification Evidence

Packet: **Beta Readiness Stream 1 — Hosted AutoTrading Certification**. Compiled 2026-08-10.
Objective: make `terminal_info().trade_allowed == true` on the hosted MT5 runtime so the hosted order path
can be accepted — **execution stays DARK throughout** (`HOSTED_MT5_EXECUTION_ENABLED` unset; ASN #7 & #8
`AUTO_SHADOW`). No order placed/closed/modified; no ASN mode changed; no execution armed.

Interlocks verified at start (read-only, prod): `HOSTED_MT5_EXECUTION_ENABLED` **unset** (DARK); ASN #7
(wayond) & #8 (ti_signals) both **AUTO_SHADOW**, active. CZ terminal (pid 10540) confirmed running from
`C:\GuvFX\accounts\1\terminal` throughout and **never touched**.

---

## Phase 1 — Disposable validation (empirical)

A disposable portable runtime was built from CZ's binaries **excluding `accounts.dat`** (credentials never
copied; `accounts_dat_in_disposable=False`, terminal64 present, 286 MB) at `C:\GuvFX\_disposable\…`, launched
via a temporary scheduled task in Administrator's interactive session, then fully removed. CZ verified
untouched before/after.

**Result — two findings, both empirical (RULE 11):**

1. **PROVEN: MT5 reads `config\common.ini` at startup.** For both `[Experts] AllowLiveTrading=0` and `=1`,
   terminal64 (build 5833) launched and wrote its journal with the disposable's portable data-dir path
   confirmed. The config-file mechanism is real and is the correct lever.
2. **NOT OBTAINABLE from a credential-free disposable: `terminal_info().trade_allowed`.** Three independent
   attempts (session-0 launch; interactive-session `Popen`+journal; interactive `initialize`-owns-launch with
   a 120 s timeout) all returned `-10005 IPC timeout`. A **no-account** MT5 blocks on its first-run wizard and
   never establishes IPC; and `TERMINAL_TRADE_ALLOWED` is only meaningful with a trade context. This matches
   the prior `project_mt5_demo_validation` finding. It is an **MT5 limitation, not a config failure** — and it
   means the packet's "no accounts.dat / no credentials" constraint is *fundamentally incompatible* with
   reading `trade_allowed` off a disposable.

**Consequence.** The authoritative negative/positive `trade_allowed` control must run on an **account-connected
terminal — Customer Zero** (Phase 3/4), which has account 1302561 connected. That is where the packet directs
the `trade_allowed==true` proof anyway.

**Minimum effective configuration (documented):** `config\common.ini`:
```
[Common]
Login=0
...
[Experts]
AllowLiveTrading=1
Enabled=1
```
`AllowLiveTrading=1` is the AutoTrading toggle MT5 reads at startup; combined with a connected account it
yields `terminal_info().trade_allowed = true`. No EA is attached, so the terminal auto-trades nothing on its
own — the setting only *permits* an order the bridge would later send, which stays gated by the DARK execution
flag + AUTO_SHADOW.

---

## Phase 2 — Implementation (repo, merged path)

Injection point (from the repo map): the beta slot-pool MATERIALISE seam. `common.ini` must be written
**per-runtime after `copy_golden`** — the golden image itself must NOT contain it (`install_pool.ps1`
`Test-GoldenImage` refuses a golden with `config\common.ini`, RULE 10). The view-only populate
(`Populate-GuvfxViewerRuntime.ps1`, `AllowLiveTrading=0`) is correct for viewers and is left unchanged.

**Two coordinated edits (the second is required, not optional):**

1. `deploy/beta-agent/win_slot_ops.py` — new primitive `RealSlotWindowsOps.write_runtime_common_ini(slot_path)`
   writes `config\common.ini` with `[Experts] AllowLiveTrading=1` (ASCII, CRLF), `Login=0`, **no
   `accounts.dat`, no credentials** (a fixed global terminal setting on a fixed slot dir — a legal Windows
   primitive). Idempotent (byte-identical overwrite). Abstract method added to `win_ops.SlotWindowsOps`.
2. `deploy/beta-agent/win_slot_ops.py::_tree_digest` — **excludes `config\common.ini`** from the tree digest,
   exactly like the existing `OWNER_FILE` exclusion. Without this, writing `common.ini` after `copy_golden`
   makes the destination digest differ from the golden, so `stage_copy`'s `destination_digest_matches`
   post-check fails on any idempotent re-materialise. With it, safe-rerun holds.
3. `deploy/beta-agent/pool_op_impls.py::materialise` — calls `write_runtime_common_ini(si.slot_path)` **after**
   the stage passes its integrity post-check (never on a failed/incomplete stage) and before evidence.

Regenerated `deploy/beta-agent/manifest.json` (bundle integrity) for the 3 edited modules.

**Tests (all green):**
- `tests_win_slot_ops.py::RuntimeCommonIniTests` — config content (`AllowLiveTrading=1`, `Login=0`, no
  `Password`, no `accounts.dat`, CRLF), idempotent byte-identical rewrite; **digest exclusion** with a
  RULE-11 positive control (a *different* `config/` file DOES change the digest).
- `tests_pool_ops.py::MaterialiseTests` — materialise now asserts the config after copy+owner-tag; the failed
  stage does NOT write config; idempotent re-materialise re-asserts config but copies nothing.
- `terminal_provisioning`: **1315 tests OK**. Full `make check`: see Phase 5 status below.

Behaviour change called out (AGENTS.md): two existing behavioural tests were updated — the `win.calls`
sequence now includes `write_runtime_common_ini`, and idempotent re-materialise now re-asserts the config
(previously "copies nothing / empty calls"). Both changes are the intended new behaviour.

---

## Phases 3–5 — status

- **Phase 5 (tests / make check):** focused suites green (1315); full `make check` — see log.
- **Phase 3/4 (Customer Zero live flip + empirical `trade_allowed==true` proof): NOT YET DONE. Gated on a
  working CZ `trade_allowed` reader.** The production reader `GET /mt5/supervision` on the bridge returns
  **404** (the `bridge_supervision_patch.py` additive endpoint is not applied to the running bridge; `/health`
  is 200). Reading CZ's `trade_allowed` therefore requires either (a) applying the supervision patch — a
  production **bridge** change + restart, outside this packet's scope and higher-risk — or (b) a read-only
  `mt5.initialize`+`terminal_info` attach run **as `guvfx_u_1` in session 3** (the same in-session attach the
  bridge performs; a fresh no-account attach fails, but CZ's terminal is already account-connected and past
  the wizard, so the attach is expected to succeed). Path (b) is the intended Phase-4 verifier.
- The CZ config flip (`AllowLiveTrading 0→1` in the existing `common.ini`, preserving `Login=1302561` +
  `accounts.dat`) + least-disruptive MT5 app restart is authorised by the packet and reversible (rollback the
  ini, relaunch), but should be executed **only alongside a proven Phase-4 reader** so the result is actually
  provable — emitting the certification without the `trade_allowed==true` measurement would violate the
  packet's "emit ONLY when proven".

## Certification marker

`HOSTED_AUTOTRADING_CONFIGURATION_CERTIFIED` — **WITHHELD** (Phase 4 empirical `trade_allowed==true` proof not
yet produced; blocked on the reader above). Phase 1 config-mechanism proof and Phase 2 implementation are
complete and green.
