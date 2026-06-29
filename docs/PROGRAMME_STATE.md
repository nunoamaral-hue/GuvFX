# GuvFX Programme State — consolidated index

> **Purpose:** one cold-start map of where the real-data programme actually is, so
> any session (human or AI) can reconstruct truth without walking Notion ancestor
> chains. **Notion is authoritative** for lifecycle (*GuvFX — Current State v0.52*);
> this file mirrors it and must be kept consistent. Last reconciled: **2026-06-28**.

## Repositories

| Logical role | Repository | Pin | Notes |
|---|---|---|---|
| Canonical app + synthetic foundation | `nunoamaral-hue/GuvFX` | `main` `148437ae` | Django/Next app; synthetic 006C arc merged (PR #36). No real data here. |
| Dedicated Windows history agent | `nunoamaral-hue/guvfx-windows-history-agent` (private) | `main` `46c81057` | A0/A1/A2/A2-P1 merged (PR #4). Read-only adapter + lazy loader; no real package import in CI. |

## Real-data acquisition ladder (006D) — status

All probes are **read-only**, run from the Mac controller over SSH/Tailscale against
the Windows VPS MT5 terminal, return privacy-safe evidence only, and were sponsor-gated.

| Step | What it proves | Status | Authority |
|---|---|---|---|
| A2-P0 / P1 | MetaTrader5 package import feasible (5.0.5735) | PASS / accepted | repo packets (merged) |
| A2-P2 | Terminal lifecycle: `initialize()`/`shutdown()` succeed | PASS / accepted | GFX-Q-006D-004 (closed-approved) |
| A2-P2-H0 | Headless check → active console session present → stop | accepted | — |
| A2-P2-H1 | One controlled logoff → autologon re-creates session → `SESSION ACTION BLOCKED` | accepted | GFX-Q-006D-005 + ADR-DATA-016 |
| A2-P2-R0 | Post-maintenance health: MT5 + bridge:8788 recovered | PASS / accepted | ADR-DATA-017 (session-dependent model accepted) |
| A2-P3 | Source identity metadata feasible | PASS / accepted | ADR-DATA-017 |
| A2-P4 | History retrieval feasible (6 EURUSD M1 rows, schema + bounds, digest only) | PASS / accepted | GFX-Q-006D-006 (closed-approved) |
| **S1** | Provision `GuvFXData` / `GUVFX_DATA_ROOT` | **DONE** — storage gate PASS (owner-delegated to Claude; NAS share already mounted, no creds) | GFX-EVD-006D-S1 |
| **A2-P5** | **First durable immutable raw object + manifest** | **DONE / PASS** — published & SHA-verified to GuvFXData; idempotent no-overwrite proven | GFX-Q-006D-007 + GFX-EVD-006D-A2-P5 |

**Accepted source identity (from P3, non-secret):** `account_type` = demo · broker
`TW Corp LLC` · server `TradersWay-Demo` · terminal build `5833` · MT5 version `500`.

**Accepted P4 retrieval:** `copy_rates_range("EURUSD", M1, 2026-06-26T12:00→12:05Z)`
returned 6 rows; row digest `fc06a585…36b3e3`. No raw OHLCV recorded.

**First stored object (P5, 2026-06-28):** `guvfx.raw.mt5.rates.v1` object
`raw/objects/sha256/01/012d60f1…a89b5f76.json` (1082 B) + `guvfx.raw_object_manifest.v1`
`raw/manifests/sha256/31/31691cfb…c4ff139e.json` (1315 B), both SHA-verified on disk in
GuvFXData; `p4_digest_match = true`. Validated by `scripts/check_data_root.py` (storage gate).

## Open gates

| Gate | Type | Owner | Blocks |
|---|---|---|---|
| ~~S1 — provision `GuvFXData` / `GUVFX_DATA_ROOT`~~ | ✅ DONE 2026-06-28 | — | (cleared) |
| Broker-server timezone verification (TradersWay-Demo) | RED, data | Nuno to approve probe | **next** — any normalised dataset publication / broad backfill |
| Windows MT5 headless / service-hardening | future ADR | Nuno | durable unattended acquisition |
| Blueprint ratification (Proposed v0.1 → Approved) | lifecycle | Nuno | stable target for downstream packets |
| Any model promotion to limited-live / production | RED | Nuno | strategy go-live |
| Live Trading ↔ target execution architecture reconciliation | Amber/ADR | PM draft → Nuno | execution-layer packets |

## Capability snapshot (Notion Capability Registry, v0.52)

- **GREEN (1/10):** Trading (production; a real order path exists today via the
  Windows bridge — governed by the legacy programme).
- **AMBER (9/10):** including *Market Data & Research Platform* (weakest; gates
  strategy quality). RED sub-capabilities: Feature Store, Market Regime Intelligence.

## Governance / PM model (current)

- **Claude Code is acting PM** (documentation, authoring, tracking; keeps Notion the
  source of truth). Self-accepts Green/Amber implementation + lifecycle.
- **Reserved to Nuno (out-of-band):** any *new* live-order / broker-credential /
  live-risk-limit / model-promotion authorization, and Notion lifecycle ratification.
- **Hard limits (unchanged):** no LLM live orders, no broker credentials, no
  risk-limit changes, no promotions, no treating generated output as validated
  evidence (Blueprint safety boundary + `.claude/rules/`).
