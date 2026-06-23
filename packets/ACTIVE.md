# Active Packet

- **Packet ID:** GFX-PKT-004A-R1 (continuation of GFX-PKT-004A)
- **Title:** Documentation Factuality Remediation v0.1
- **Branch:** `chore/guvfx-documentation-convergence`
- **Status:** Remediated — CI verified; PM review pending. **No merge is authorised.**

## Scope

A Green, documentation-only factuality remediation on the existing PR #32 branch,
correcting six current-state overstatements raised in PM review:

- `docs/STATUS.md` — label the domains as *documented production routes* and state
  that route availability / live production health were not probed.
- `docs/ARCHITECTURE.md` — qualify the PostgreSQL version (CI = 16; production =
  Unknown without a deployment source); change production topology to *Partial*;
  change security posture to *Partial*, separating implemented Git controls from
  policy controls and unknown operational facts.
- `docs/DATA_CONTRACTS.md` — record the fixed current MT5 mount-path constants and
  the `/admin/` + `/health/` API exceptions; mark configurable paths as Proposed.
- `docs/NOTION_MAP.md` — append three record titles (titles only).
- this pointer + a new R1 evidence manifest.

## Prohibited in this packet

- No application, CI, Makefile, scanner, rule, ADR-template, or infrastructure
  edits.
- No production, NAS, broker, MT5 runtime, market-data, or Notion access.
- No branch switch, fetch, merge, rebase, reset, or force push.
- No edit outside the six authorised paths.
- No PR merge.

## Evidence

- Expected path: `evidence/manifests/GFX-EVD-004A-R1-factuality-remediation.json`
- The original `evidence/manifests/GFX-EVD-004A-documentation-convergence.json` is
  not modified.

## Notion record

- Title: **GFX-PKT-004A-R1 — Documentation Factuality Remediation v0.1**
  (Notion is authoritative for full text and lifecycle status.)
