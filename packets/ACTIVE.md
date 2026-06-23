# Active Packet

- **Packet ID:** GFX-PKT-004A
- **Title:** Documentation and Current-State Convergence v0.1
- **Branch:** `chore/guvfx-documentation-convergence`
- **Status:** Executed — CI verified; PM review pending. **No merge is authorised.**

## Scope

A documentation-only convergence built on a dedicated branch from `origin/main`
(which includes governance merge `c17b7b8`). Repair stale/duplicated current-state
docs and document the actual implemented architecture and data contracts:

- repair `docs/STATUS.md` (collapse duplication, distinct verified/active/known
  sections, preserve dated historical green checks);
- document implemented architecture in `docs/ARCHITECTURE.md` with explicit
  Implemented/Partial/Proposed/Unknown labels and a separate Target evolution
  section;
- create `docs/DATA_CONTRACTS.md` separating Current/Candidate/Proposed/Unknown
  contracts, every current claim citing a repository path or ADR;
- enrich `docs/ADRs/template.md` (no new ADRs, no parallel directory);
- update `docs/NOTION_MAP.md` (titles only) and this pointer;
- record machine-readable evidence under `evidence/manifests/`.

## Prohibited in this packet

- No application/backend/frontend/MT5 code edits.
- No Makefile, CI, secret-scanner, scoped-rule, or infrastructure change.
- No local package installation, Docker, database, or production access.
- No NAS, broker, market-data, Notion, or trading action.
- No edits outside the seven authorised files.
- No merge, rebase, reset, or force push.
- No change to `flow-a-shadow` or preservation branches.

## Evidence

- Expected path: `evidence/manifests/GFX-EVD-004A-documentation-convergence.json`

## Notion record

- Title: **GFX-PKT-004A — Documentation and Current-State Convergence v0.1**
  (Notion is authoritative for full text and lifecycle status.)
