# Active Packet

- **Packet ID:** GFX-PKT-003I-B (continuation of GFX-PKT-003I-A)
- **Title:** Commit and Remote CI Validation v0.1
- **Branch:** `chore/guvfx-governance-convergence`
- **Status:** In progress — governance foundation locally validated; remote CI validation
  pending before PM acceptance. **No merge is authorised.**

## Scope

Establish the smallest useful repository-control foundation on a dedicated branch built
from `origin/main`, plus incorporation of the preserved PX-2.4 dashboard-drafts archive:

- scoped Claude rules under `.claude/rules/`;
- authority and packet boundaries added additively to `CLAUDE.md`;
- exact ignore rules for local `.claude/` material (rules remain tracked);
- a Notion map (titles only);
- hybrid active-packet pointer (this file) and `packets/README.md`;
- handoff and evidence conventions (`docs/HANDOFF_TEMPLATE.md`, `evidence/`);
- standard-library secret scanner and tests;
- integration of the secret scan into the Make and CI gates.

## Prohibited in this packet

- No current-state cleanup, architecture rewrite, or feature changes.
- No fetch/pull/push/PR/remote browsing or CI execution.
- No Notion/NAS/broker/MT5/Docker/package-manager/market-data access.
- No merge or rebase of Flow A (`flow-a-shadow`).
- No deletion/modification of the original local `.claude/` files.

## Evidence

- Expected path: `evidence/manifests/GFX-EVD-003I-A-governance-foundation.json`

## Notion record

- Title: **GFX-PKT-003I-A — Governance Convergence Foundation v0.1**
  (Notion is authoritative for full text and lifecycle status.)
