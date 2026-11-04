# LIVE Read-Only Positions — Host Bundle & Rollback Design (PREPARED, NOT DEPLOYED)

**Packet:** GFX-LIVE-POSITIONS-READONLY-001 · **ADR:** 0072 · **Date:** 2026-11-04
**Status:** PREPARED ONLY. No bundle deployed, no bridge restarted, no flag enabled. Deployment + arming are a
separate Sponsor/operator gate (RED). This document is the artefact for that later, separately-authorised step.

---

## 1. Bundle artefact

The capability ships in the per-tenant bridge script **and** (for future provisioning) the env renderer:

| Artefact | Role | SHA-256 (this branch) |
|---|---|---|
| `scripts/mt5_signal_bridge.py` | host bridge (the deployed artefact) | `15827a6b568aacfa524f88f4ec32163ccee9cb5aa45747d33e876228203e93c0` |
| `backend/execution/bridge_config.py` | per-tenant env renderer (backend; not host-deployed) | `68f98de543a14ba1c8468fa658e4830898a1d82743451850ecc27651be9323ec` |

Re-verify on the branch head before any deploy:
```bash
shasum -a 256 scripts/mt5_signal_bridge.py backend/execution/bridge_config.py
```

## 2. Syntax / parse validation

- Python: `python -c "import ast; ast.parse(open('scripts/mt5_signal_bridge.py').read())"` → **AST parse OK**
  (and the full `execution` suite loads the bridge via `_load_bridge`). The bridge is Python, not PowerShell — RULE 9
  (PowerShell BOM/encoding) does not apply. The renderer's output is a `.bat` body kept **ASCII-only** by `_kv`
  (control-char guard), so it parses identically under any Windows encoding.
- No PowerShell install artefact is added or changed by this packet (RULE 8/9 launcher gate: nothing new to parse).

## 3. Per-tenant configuration delta (the ONLY runtime change, when later authorised)

Add exactly one line to the **target tenant's** bridge env file (e.g. the account's `*_bridge.env.bat`), then
restart **that one** bridge via its supported service/task mechanism (NEVER an interactive `Start-Process` —
RULE 1):
```
set MT5_LIVE_READONLY_POSITIONS_ENABLED=1
```
This is a READ flag only. It is **not** `MT5_ALLOW_LIVE` and never emitted with execution authority implied. It
does not touch `common.ini` / `AllowLiveTrading` / Algo Trading. Absent ⇒ capability OFF (and OFF on restart).

If/when the backend renderer path is used for (re)provisioning, the equivalent is arming the default-OFF authority
`LIVE_READONLY_POSITIONS_MONITORING_ENABLED=1` so `readonly_positions_for_account()` returns True for the LIVE
account, which makes `render_bridge_env(..., readonly_positions=True)` emit the same line.

## 4. Rollback

- **Runtime (per tenant):** remove the `set MT5_LIVE_READONLY_POSITIONS_ENABLED=1` line and restart that bridge
  via its service/task → immediate return to withheld live positions (current production behaviour). No data
  migration to undo.
- **Code:** revert this PR. Branch base `origin/main` @ `85f0430d` (#609); reverting restores #472's unconditional
  de-gate. Redeploying the pre-branch bridge bundle restores the prior host state.
- **Authority:** clear `LIVE_READONLY_POSITIONS_MONITORING_ENABLED` (settings/env) → every future render omits the
  flag.

## 5. One-at-a-time rollout plan (for the separate GO)

1. Deploy the refined bridge bundle to the host with the flag **absent** everywhere → **behaviourally a no-op**
   (every LIVE account still returns `account_not_demo`; equity unchanged). This is the safe landing.
2. **Negative control — #48:** flag absent → `GET /mt5/positions` still `400 account_not_demo`; dashboard still
   withholds live rows. Confirms the default-OFF gate.
3. **Positive control — #58:** set `MT5_LIVE_READONLY_POSITIONS_ENABLED=1` on #58's bridge only, restart that one
   bridge. `GET /mt5/positions` → `200` with #58's own positions (incl. the already-open position) + `observed_at`.
   Dashboard shows #58's live open rows. **#58's open position is READ ONLY — never modified or closed.**
4. Verify isolation: #48 (control) still `400`; no other tenant's positions appear on #58's read (backend firewall
   login-EXACT + server + environment).
5. Expand one account at a time only after each prior account is confirmed read-only + identity-bound.

## 6. Runtime counters — proof of ZERO order operations

During any controlled window, assert from the bridge/host:
- bridge log shows `"[positions] LIVE read-only served: trade_mode=2 …"` for served reads and
  `"[positions] LIVE read refused: MT5_LIVE_READONLY_POSITIONS_ENABLED off …"` for refusals (secret-free);
- **zero** `order_send` / `/mt5/order` / `/mt5/close-position` / `/mt5/modify-position` invocations attributable
  to the read path (there is no such call site in `fetch_positions`; confirm no new order deals on #58);
- `LiveExecutionAuthorization` count unchanged; `MT5_ALLOW_LIVE` absent on the monitored bridge; execution +
  copier flags OFF.

## 7. Acceptance targets (later, separately authorised)

- **#48** — established control (flag OFF ⇒ withheld; proves default-OFF).
- **#58** — new LIVE account with an already-open position (flag ON ⇒ positions visible). **The deployment must
  never modify or close #58's position** (hard boundary).

## 8. Limitation

The **deployed** bridge-bundle hash on #48/#58 was NOT read on the host in this packet (production access is RED /
out of the repo+test scope). The "deployed bundle predates the #472 de-gate / never deployed" finding is inferred
from repo + the observed `400`; a read-only host hash check (separately gated) would confirm it directly.
