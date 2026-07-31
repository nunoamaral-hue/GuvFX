# Golden Image Blocker — Provenance Investigation (2026-07-30)

**Question:** can the drifted beta golden at `C:\GuvFX\golden\newMT5` be safely **re-pinned**, or must it be
**re-staged** from a clean dedicated source?

**Posture:** READ-ONLY. No mutation. All probes were `Get-*` / `Test-Path` / `Get-FileHash` /
`Get-AuthenticodeSignature` / `Get-CimInstance` / `fsutil behavior query`. The running golden terminal
(pid 5912), the firewall rule, and the binaries were observed, not touched. No signing keys, no CZ change,
no ProvisioningJob re-drive, no re-pin/re-stage.

## VERDICT: **B — RE-STAGE REQUIRED**

The golden was run interactively, updated in place by MT5 LiveUpdate, and is registered/used as the live
Strategy Tester Agent. Its binaries are officially MetaQuotes-signed (not tampered), but the RULE-10
dedication / pristine / immutability invariants are broken and the image is a **live, auto-updating**
install — its digest is a moving target and must not be pinned.

## Findings (6 points)

1. **References to the golden path:** firewall rule `MetaTrader 5 Strategy Tester Agent` →
   `C:\GuvFX\golden\newMT5\metatester64.exe` (Enabled, Inbound, Allow — auto-registered on first listen);
   process `terminal64.exe` **pid 5912 running now** from the golden dir. No service, no scheduled task.
2. **Executed from golden since staging? YES.** Golden `terminal64.exe` launched **2026-07-27 11:48:21**
   (parent `explorer.exe` = human double-click; cmdline `"C:\GuvFX\golden\newMT5\terminal64.exe"` — **no
   `/portable`**, no broker login) and still running 3 days later.
3. **What changed the two binaries on 07-27? MT5 LiveUpdate.** The interactively-launched terminal connected
   to MetaQuotes at 11:48 and at **11:54:20** the update replaced `metatester64.exe` + `MetaEditor64.exe` in
   place with build **5.0.0.6061**. Only those two files changed (all other 582 files dated 07-23 staging).
4. **Binary comparison:**
   - `terminal64.exe` = **5.0.0.6036** (07-23 staged, unchanged); `metatester64.exe` + `MetaEditor64.exe` =
     **5.0.0.6061** (07-27) — a version *increment* = an update. Result: a **mixed 6036/6061** tree (the
     staged coherent-6036 image was digest `3a7fa663`; on-disk is now `8a6480f4`).
   - Authenticode: all three **Valid, `CN=MetaQuotes Ltd., O=MetaQuotes Ltd., S=Lemesos, C=CY`** → the 6061
     binaries are genuine official MetaQuotes binaries, not tampered — but a newer, mismatched build.
   - Separate install `C:\GuvFX\mt5` = build **5.0.0.5698** (March, portable, `Config\accounts.dat` absent) —
     an older, different install; not the golden's source, not build-matched.
   - A `C:\_dr_backup\2026-07-25_pre_reboot_snapshot` exists (pre-07-27) for corroboration; not required —
     versions/mtimes/signatures are conclusive.
5. **Shared with / used by:** update process (LiveUpdate 6036→6061) — YES; strategy tester (golden
   `metatester64.exe` = registered Strategy Tester Agent) — YES; live runtime (golden `terminal64.exe`
   running interactively now) — YES. (Non-portable launch → runtime state went to `%APPDATA%`, not the
   golden dir, so no broker creds landed in the golden.)
6. **`terminal64.exe` + cleanliness:** `terminal64.exe` 5.0.0.6036, MetaQuotes-Valid, unchanged; `-VerifyOnly`
   + probes confirm **no** `accounts.dat` / broker `bases` / EA. **Credential/history/config cleanliness =
   INTACT.** Dedication/immutability = BROKEN.

## Why not re-pin (Option A)
- Moving target: the golden is live and connected; MT5 will LiveUpdate again → any pin re-breaks.
- Mixed-build (6036 terminal + 6061 tester/editor) — not a coherent single-build install.
- Used + shared (running terminal + registered strategy-tester agent) — RULE-10 forbids promoting a used
  install. Valid signatures prove no malware, not pristine provenance.

## Minimum clean re-stage plan (gated host mutations — Red/Sponsor; not performed)
1. Sever the golden from runtime/tester use: stop golden `terminal64.exe` (pid 5912); remove the Strategy
   Tester Agent firewall rule targeting `…\newMT5\metatester64.exe`; ensure nothing launches the golden.
   Point any strategy-tester/backtest need at a separate tester install.
2. Stage a fresh dedicated never-launched golden from a clean MetaQuotes install of a **single pinned build**
   (decision: adopt **6061** [latest coherent, recommended] or **6036**), no broker account / no bases
   history / no EA / correct portable markers. Do NOT promote `…\golden\newMT5`; do NOT reuse `C:\GuvFX\mt5`
   (5698) without its own RULE-10 validation.
3. Guarantee immutability: golden is only ever copied from (agent `MATERIALISE`), never run; deny LiveUpdate.
4. Re-pin `BETA_AGENT_GOLDEN_DIGEST` to the fresh digest; re-run `install_pool.ps1` golden-stage +
   `-VerifyOnly`.
5. Verify isolation: no service/task/firewall/process references the golden path except the agent's read-only
   copy.

**Open decision for the Sponsor:** which build to pin the fresh golden to (6061 recommended vs 6036) — it sets
the MT5 build every beta slot runtime materialises from.
