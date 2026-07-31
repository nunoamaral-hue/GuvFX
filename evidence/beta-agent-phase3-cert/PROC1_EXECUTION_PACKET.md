# SOP-001 — Golden Creation Standard Operating Procedure — EXECUTION PACKET (operator actions only)

**This is a permanent SOP, not a one-off.** It must be executable by any competent engineer following it
line-by-line, without Sponsor guidance. Actions only; the engineering validation (incl. SOP self-assessment) is
a separate artefact: `PROC1_VALIDATION_PACKET.md`. Do no validation yourself — capture evidence and STOP.

**Objective (Customer Zero First Principle):** produce a clean, never-launched MT5 install at
`C:\GuvFX\golden\staging\` — the raw material for the new Golden — to remove **Blocker A**. Non-destructive:
writes only a new folder; touches no running MT5, bridge, or estate object.

**Version policy:** version-agnostic — install from the approved source; whatever build the installer produces
is recorded and becomes canonical.

---

## Prerequisites the operator MUST have before starting  *(permanent SOP-001 values)*
1. **Approved MT5 installer source (canonical):** the **official MetaTrader 5 installer from MetaQuotes** —
   `https://www.metatrader5.com/en/download` (direct stub:
   `https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe`).
   *Rationale:* the Golden is **broker-login-free** and is copied to slots that later log into whatever broker
   the beta demo provides, so an **open MetaQuotes build — which can connect to any broker server** — is the
   correct, stable, reproducible choice; **never a broker-locked white-label build**. *(Sponsor-ratified
   2026-07-30.)* Download `mt5setup.exe` from that source to the host; record the exact URL + filename + its
   SHA-256.
2. **Approved engineering host access method:**
   - **Interactive GUI (this SOP's install step):** RDP to `WIN-RD8VDS93DK7` (`100.79.101.19`, Tailscale) `:3389`
     as **Administrator** with an RDP client, **or** the Guacamole browser desktop `guac.guvfx.com/guacamole/`.
   - **Engineering CLI (Claude's read-only validation):** `ssh administrator@100.79.101.19` over Tailscale
     (key-based).
   Administrator credentials + the SSH key live in the **secret store** — **never in this SOP.**
3. **Disk:** ≥ 1 GB free on `C:` (check: `Get-PSDrive C`).
4. **Staging path free:** `C:\GuvFX\golden\staging` must not already exist (check:
   `Test-Path C:\GuvFX\golden\staging` → must be `False`; if `True`, use `staging2` and record the name).

## Estate-safety — how to identify what NOT to touch
The host runs live infrastructure that must be left alone. Before starting, open **Task Manager → Details**,
add the **"Image path name"** column, and note every existing `terminal64.exe` / `metatester64.exe` /
`python.exe`. **Leave every already-running process and window untouched** — in particular the old golden
(`C:\GuvFX\golden\newMT5\terminal64.exe`), Nuno's terminals (`C:\Program Files\IS6 Technologies…`), and the
bridge (`python.exe`). This SOP only ever *adds* the new `staging` folder.

## Step 0 — Baseline evidence (BEFORE launching the installer)
Capture and keep: **(A)** File Explorer at the existing Golden `C:\GuvFX\golden\newMT5`; **(B)** any running MT5
windows (each window, or the taskbar showing them).

## Actions (line-by-line)
1. RDP into `100.79.101.19` as Administrator (per prerequisite 2).
2. Double-click the downloaded MT5 installer.
3. Accept the **License Agreement** (tick the box).
4. Click **"Settings"** (the options expander on the first screen).
5. In **Installation folder**, type exactly:  `C:\GuvFX\golden\staging`
6. Leave everything else default → **Next** (it downloads the current build and installs).
7. Wait for **"installation completed"**.
8. **CRITICAL — do NOT launch:** untick **"Launch MetaTrader 5"** → **Finish**. If the terminal auto-opens,
   **close it immediately (title-bar ✕)** — **type no login / password / server; click nothing inside it.**
9. Do not re-open the terminal or MetaEditor; change no settings.
10. **STOP.** Fill in the completion report below and send it to Claude.

> **UI-variant fallback (production robustness).** Installer wording/layout varies by version/branding. Whatever
> the exact UI, the SOP's two required outcomes are: **(i) the installation folder is `C:\GuvFX\golden\staging`**
> and **(ii) the terminal is never launched and no login is entered.** If a screen does not match steps 3–8,
> achieve those two outcomes and record what differed under "Unexpected messages".

## Failure → STOP + report (do not fix, do not improvise)
Wrong install path · terminal launched **and a login entered** · installer errored/incomplete · any estate
MT5/bridge disturbed.

## Rollback (only if instructed by Claude/Sponsor)
The staging folder is new and referenced by nothing → run the install's uninstaller **or** delete
`C:\GuvFX\golden\staging` (estate untouched), then re-run this SOP from the beginning.

## Explicit STOP point
After the report: do **not** launch, promote, create markers, digest, pin, or archive. Hand back to Claude.

---

## Structured completion report (fill in and send)

```
SOP-001 (Procedure 1) complete.

Approved installer source used:
Installation path:

Installer completed successfully:        YES / NO

Did MT5 launch?                          YES / NO
  If YES — Was any login entered?        YES / NO
  If YES — Was it immediately closed?    YES / NO

Unexpected messages / UI differences:

Baseline screenshots captured (A folder, B running windows):  YES / NO
```
