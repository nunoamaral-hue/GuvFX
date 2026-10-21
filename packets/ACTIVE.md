# Active Packet

- **Status:** No active *canonical-repo* feature packet. The synthetic market-data
  foundation arc (GFX-PKT-006C + R1…**R4-R2**) is **complete and merged** — R4-R2 via
  **PR #36**; `main` is `148437ae8bc651f6eb818e15bd9a16cf9d3a993f`. (The earlier
  R4-R2 active-packet content here is superseded by that merge, not deleted from
  history.)
- **Live programme frontier — real market-data acquisition (006D).** Runs in the
  dedicated private repo `nunoamaral-hue/guvfx-windows-history-agent` (`main`
  `46c81057…`) plus governed read-only VPS probes — **NOT in this repository**.
  Currently **blocked on owner action GFX-PKT-006D-S1** (provision/expose the
  approved `GuvFXData` / `GUVFX_DATA_ROOT` target). **Notion (*GuvFX — Current State
  v0.52*) is authoritative** for the full lifecycle. See
  [`docs/PROGRAMME_STATE.md`](../docs/PROGRAMME_STATE.md).

## Registered workstream — MT5 Master/Follower Copier (DARK, observer‑first)

- **Packet:** `GUVFX_MT5_LIVE_MASTER_FOLLOWER_COPY_TRADING` (P0, Programme Director,
  2026‑10‑20). **Authority:** Programme Director scope GO for **DARK engineering only**;
  LIVE follower execution NOT authorized (separate Sponsor + Security GO). Decision +
  boundaries recorded in [`docs/ADRs/0051-mt5-master-follower-copier.md`](../docs/ADRs/0051-mt5-master-follower-copier.md);
  day‑1 capability map in [`docs/MT5_COPIER_DAY1_CAPABILITY_MAP.md`](../docs/MT5_COPIER_DAY1_CAPABILITY_MAP.md).
- **Scope:** new governed strategy source → existing guarded execution path. PR A master
  observer + ledger (read‑only) · PR B follower config + replication (enqueue‑only
  SHADOW→DEMO) · PR C Copy Trading UX · PR D cross‑broker cert + demo rehearsal.
- **Branch:** isolated worktree `worktree-mt5-copier-foundation` off latest canonical
  `main` (`5b209833`, 2026‑10‑20). New Django app, own migration sequence — no collision
  with the Withdrawal/BI stream.
- **Hard boundaries:** no `order_send` from Django; demo‑only close/SL‑TP bridge
  protections unmodified; the five‑layer LIVE wall + kill switch unchanged; no production
  deploy/migration; DARK flag default OFF. If repository governance requires a separate
  recorded architecture ratification before merge, satisfy it before merging.

## Active work in THIS repository (Claude-as-PM improvement backlog)

Bounded, additive, behaviour-preserving documentation + tooling to make the
programme auditable and its controls mechanical. Green/Amber items proceed
autonomously; Red items require Nuno's explicit approval.

- **A** — reconcile stale handoff docs to the true 006D/S1 state. *(this change)*
- **B** — `docs/PROGRAMME_STATE.md` consolidated state index. *(this change)*
- **C** — `GUVFX_DATA_ROOT` preflight validator (boolean storage-gate check).
- **D** — evidence-factuality linter (file/test counts, clean-tree, checksums).
- **E** — verify/enforce the read-only MT5 boundary (CI AST guard).
- **F** — broker-server timezone determination probe — **Red, needs Nuno's approval**.
- **G** — live Trading path standing risk-watch.
- **H** — Blueprint ratification (Proposed → Approved) — **needs Nuno's sign-off**.
- **I** — role-vocabulary + ADR-009 numbering reconciliation.

## Authority boundaries (unchanged)

- No LLM live orders, broker credentials, live risk-limit changes, model promotion,
  or treating generated output as validated evidence.
- New live-order/credential/risk-limit/promotion authorizations and Notion lifecycle
  ratification remain Nuno's explicit, out-of-band gate.

## Canonical source pin

- Repository: `nunoamaral-hue/GuvFX` — `main` `148437ae8bc651f6eb818e15bd9a16cf9d3a993f`.
