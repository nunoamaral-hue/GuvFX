# Rule — Notion

Scope: read before any interaction with the GuvFX Notion workspace.

- **Explicit packet authority required.** Do not read from or write to Notion unless the
  active packet explicitly grants Notion access for that work.
- **Supersede, never silently overwrite.** Approved records (blueprint, decisions, risks)
  are superseded with a new version or clearly marked update — never quietly overwritten or
  deleted.
- **Keep secrets and large artefacts out.** No credentials, tokens, or bulk/raw data in
  Notion. Reference them by location instead.
- **PM owns lifecycle status.** The packet/decision lifecycle status in Notion is owned by
  the PM. Do not advance, approve, or close lifecycle states on your own.
- Notion is authoritative for approved blueprint, decisions, risks, and full packet text;
  Git is authoritative for implementation, tests, and concise evidence.
