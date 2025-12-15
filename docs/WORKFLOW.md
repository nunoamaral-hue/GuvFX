# Workflow — Coder Replacement / Chat Continuity

This workflow ensures any new developer (or new Codex/ChatGPT session) can continue work safely.

## Start-of-session checklist (incoming coder)
1. Pull latest code and checkout the branch in `docs/STATUS.md`
2. Read:
   - `docs/STATUS.md`
   - `docs/HANDOFF.md`
   - `docs/NEXT.md`
   - `docs/RUNBOOK.md`
   - `docs/KNOWN_ISSUES.md`
3. Run verification commands (see RUNBOOK) and paste results into the task notes.
4. Pick **one** ticket from `docs/NEXT.md` and implement.

## End-of-session checklist (outgoing coder)
1. Commit all work (even partial, but keep it buildable if possible).
2. Update:
   - `docs/HANDOFF.md`
   - `docs/STATUS.md`
   - `docs/NEXT.md`
   - `docs/KNOWN_ISSUES.md`
3. Ensure lint/build/tests are green OR document why not and the next step.

## Handoff Block (copy/paste)
Use the template in `docs/HANDOFF.md`.
