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
