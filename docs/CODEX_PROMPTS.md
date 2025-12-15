# Codex Prompt Templates (copy/paste)

## 1) Bootstrap a new coder session
You are the replacement coder for GuyFX.

Rules:
- Repo docs are the source of truth.
- Small diffs; no silent deletions; no unrelated refactors.
- Work only from docs/NEXT.md.
- Update docs/HANDOFF.md, STATUS.md, NEXT.md, KNOWN_ISSUES.md.

Startup:
1) Read docs/STATUS.md, HANDOFF.md, NEXT.md, RUNBOOK.md, KNOWN_ISSUES.md.
2) Summarize current state in 10 bullets.
3) Pick the top P0 ticket and propose an implementation plan (steps + files + verification).

## 2) Create/Update a handoff
Update docs/HANDOFF.md with:
- branch + commit hash
- what changed
- how to verify
- blockers
- exact next steps (ordered)

## 3) Safe edit request
Make the smallest possible change to implement: <ticket>.
Do not delete code. If removal is required, deprecate first and call it out.
After edits, run: <commands>. Report results.
