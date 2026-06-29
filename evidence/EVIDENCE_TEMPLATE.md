# Evidence Template

Each packet handoff produces a JSON manifest in `evidence/manifests/` that validates
against `evidence/schema/evidence-manifest.schema.json`. This file documents the fields.

| Field | Meaning |
|-------|---------|
| `schema_version` | Version of the evidence manifest schema used. |
| `handoff_id` | Identifier for this handoff/evidence record. |
| `packet_id` | The packet this evidence is for (e.g. `GFX-PKT-003I-A`). |
| `created_at_utc` | UTC timestamp when the evidence was produced. |
| `branch` | Git branch the work was performed on. |
| `base_commit` | Commit the branch was based on. |
| `head_commit` | Resulting commit, or `null` if self-referential (see limitations). |
| `commands` | Array of exact commands executed. |
| `expected_results` | Array of expected outcomes for those commands. |
| `actual_results` | Array of actual outcomes observed. |
| `status` | One of `PASS`, `PARTIAL`, `FAIL`. |
| `limitations` | Array of what was not covered, skipped, or assumed. |
| `artefact_locations` | Array of paths/locations of produced artefacts. |
| `checksums` | Object mapping file path → checksum for key files. |
| `reviewer` | Reviewer name, or `null` if not yet reviewed. |

## Rules

- Use `PASS` only when applicable acceptance criteria actually ran and passed.
- Keep evidence free of secrets, personal paths, usernames, hostnames, and remote URLs.
- Prefer redacted file/path/category references over reproducing sensitive content.
