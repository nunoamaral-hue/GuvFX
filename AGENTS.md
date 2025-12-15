# GuyFX / GuvFX — Codex Working Agreement (AGENTS.md)

Codex reads this file before doing any work. Follow these rules **every time**.

## Non-negotiables (safety + continuity)
1. **Repo is the source of truth.** If something isn’t in code/docs, say so and propose a safe way to confirm.
2. **Small diffs.** Prefer minimal, targeted patches. Do not rewrite whole files.
3. **No silent deletions.** Do not delete code/blocks/functions unless:
   - the task explicitly requires it, and
   - you call it out in your plan and in the final summary.
   If removal is needed, prefer deprecating first (comment + TODO + follow-up ticket).
4. **No drive-by refactors.** Don’t “clean up” unrelated code, formatting, imports, or naming.
5. **Always preserve behavior** unless the ticket says to change it.
6. **Update docs on every meaningful change**:
   - `docs/HANDOFF.md`
   - `docs/STATUS.md`
   - `docs/NEXT.md`
   - `docs/KNOWN_ISSUES.md`

## Workflow (how to work in this repo)
### Start of task
- Read (in this order): `docs/STATUS.md` → `docs/HANDOFF.md` → `docs/NEXT.md` → `docs/RUNBOOK.md` → `docs/KNOWN_ISSUES.md`
- Choose **one** P0/P1 ticket from `docs/NEXT.md`.
- Write a short plan: steps + files you will touch + verification command(s).

### While implementing
- Keep commits small and descriptive.
- Add/adjust tests if behavior changes.
- If paths differ from the docs, update the docs rather than guessing.

### End of task
- Run the verification commands from `docs/RUNBOOK.md`.
- Summarize exactly what changed (files + reason).
- Update the handoff docs.

## Where things live (conventions)
- Backend: Django (likely under `backend/django/` — confirm in repo)
- Frontend: Next.js (likely `frontend/` OR repo root — confirm in repo)
- Shared docs: `docs/`

## Definition of Done (minimum)
- Lint/build/tests relevant to the change are green **or** you documented why they can’t be run and what’s needed.
- Docs updated (STATUS/HANDOFF/NEXT/KNOWN_ISSUES).
