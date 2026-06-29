# Packets

GuvFX work is organised into **packets**. This directory is the lightweight, hybrid view
of that system inside the repository.

## Where things live

- **Notion is authoritative** for full packet text and the complete packet lifecycle
  (draft → ready → executing → review → approved/closed), including approved blueprint,
  decisions, and risks.
- **The repository stores only**:
  - the **active pointer** — `packets/ACTIVE.md`, naming the packet currently in flight;
  - **concise evidence** — under `evidence/` (manifests, schema, templates).

## Conventions

- **Packet prose is not duplicated wholesale** into the repo. `ACTIVE.md` records the ID,
  title, branch, status, scope/prohibitions summary, and the Notion title — not the full
  packet body.
- **Commits and evidence reference the packet ID** (e.g. `GFX-PKT-003I-A`) so work can be
  traced back to its Notion record.
- When a packet completes, update `packets/ACTIVE.md` to reflect the next active packet, or
  clear it; the historical record lives in Notion and in Git history/evidence.
