# Evidence

Concise, machine-readable evidence for GuvFX packets.

## Layout

- `schema/evidence-manifest.schema.json` — JSON Schema every manifest must validate against.
- `EVIDENCE_TEMPLATE.md` — human-readable guide to filling a manifest.
- `manifests/` — one manifest per packet handoff, named
  `GFX-EVD-<packet>-<slug>.json`.

## Principles

- Evidence is **machine-readable first** (the manifest), with prose only as support.
- Record the **exact commands** run and their **actual results** — not intentions.
- State **limitations** explicitly; silence is not coverage.
- Include **checksums** for key files whose integrity matters to the claim.
- Mark `status: PASS` only when the applicable acceptance criteria actually ran and passed;
  otherwise `PARTIAL` or `FAIL` with a reason.
- **No secrets, personal paths, usernames, hostnames, or remote URLs** in evidence.
