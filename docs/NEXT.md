# NEXT — Priorities (keep this list short)

## Current next action (single)
- [ ] **Broker-server timezone determination probe (Red, needs Nuno's approval):**
  verify the TradersWay-Demo server timezone before any normalisation or broad
  backfill. MT5 bar times are broker-server time, not guaranteed UTC; no offset may
  be hardcoded. This touches real data, so it is a Nuno-gated Red step.

> ✅ Done 2026-06-28: **S1** (approved `GuvFXData` storage root provisioned) and
> **GFX-PKT-006D-A2-P5** (first durable immutable raw object + provenance manifest,
> SHA-256-verified in GuvFXData; idempotent). This is the first real GuvFX
> market-data object.

> The synthetic 006C foundation arc is fully merged (PR #36, `main` `148437a`). The
> live frontier is the **006D** real-data acquisition workstream in the dedicated
> `guvfx-windows-history-agent` repo + governed VPS probes — see
> `docs/PROGRAMME_STATE.md`. Notion (*Current State v0.52*) is authoritative.

## PM improvement backlog (in progress, Claude-as-PM)
Green/Amber items proceed autonomously; Red items are flagged for Nuno's approval.
- [x] **A — reconcile these stale handoff docs** to the true 006D/S1 state.
- [x] **B — `docs/PROGRAMME_STATE.md`** consolidated packet→repo→status→evidence index.
- [x] **C — `GUVFX_DATA_ROOT` preflight validator** — `scripts/check_data_root.py`
  + `tests/test_data_root.py`, wired into `make governance-check` + CI.
- [ ] **D — evidence-factuality linter** (file/test counts, clean-tree, checksums).
- [ ] **E — enforce read-only MT5 boundary** (verify/added CI AST guard).
- [ ] **F — broker-server timezone probe** — **NEEDS NUNO APPROVAL (Red, data)** — *next gate*.
- [ ] **G — live Trading path standing risk-watch** (kill-switch, failure modes).
- [ ] **H — ratify the Blueprint** (Proposed → Approved) — **NEEDS NUNO SIGN-OFF**.
- [ ] **I — reconcile role vocab + ADR-009 numbering collision**.
- [x] **J — backup & DR** — decided: Phase-1 NAS-local (RAID); offsite deferred.
- [x] **K — record PM governance state in Notion** (operating model + S1/P5 records).

## P0 (historical)
1. [x] Resolve local docs diffs cleanly: either (a) commit `docs/HANDOFF.md` + `docs/STATUS.md` on a small `docs/...` branch and open a PR to `main`, or (b) restore them if they are outdated. — done 2025-12-16
2. [x] Confirm repo health: run `make check` on `main` and on the active feature branch. — done 2025-12-16
3. [x] Broker autocomplete MVP: define acceptance criteria and implement debounced broker search + selection flow. — done 2025-12-16
4. [x] Add tests/guardrails for broker autocomplete (minimum: type-safe API response handling + basic UI state tests if available). — done 2025-12-16

## P1
1. [ ] Cleanup follow-ups: ensure `.trash_duplicates/` stays ignored and remove any remaining duplicate “(1)” / “ 2” files if they reappear.
2. [x] Switch login reason parsing to a lazy `useState` initializer so the client-only `window` lookup happens safely.
3. [x] Silence the remaining frontend ESLint warnings in `accounts`, `backtests`, and `profile` so `make check` stops failing because of lint.
4. [x] Track keyboard navigation edge cases (wrap, visibility, focus) as follow-up work before the next release; `fix/broker-autocomplete-edgecases` re-applied the debounce/keyboard nav/abort flow for broker suggestions and now needs verification on real data. — done 2025-12-16
5. [x] VPS deployment + domains + Traefik + Guacamole routing completed and serving production traffic (live 2025-12-16).
6. [ ] Verify MT5 handoff automation end-to-end (multiple accounts) using the shared `/srv/guvfx/mt5_handoff` configs.
7. [ ] Investigate/fix MT5 mouse input reliability through Guacamole (mouse clicks freeze until File menu is toggled).
8. [ ] Harden MT5 automation (secure password handling, per-account JSON, and optional `SUBMIT=1` gating for `apply-account-config`).
9. [ ] Bake the `apply-account-config` automation + Openbox autostart into the `mt5free-desktop` image once the workflow stabilizes.
10. [x] Decision: continue using host bind mounts for MT5 automation scripts rather than baking them into the container images. — done 2025-12-16

## Parking lot (later)
- Ideas/notes that are **not** committed work
