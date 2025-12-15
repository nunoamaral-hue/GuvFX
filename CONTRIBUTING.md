# Contributing to GuyFX / GuvFX

This repo is designed to support **coder replacements** (new devs or new Codex chats) without losing project context.

## Golden rules
- **Docs are mandatory**: update `docs/STATUS.md`, `docs/HANDOFF.md`, `docs/NEXT.md`, `docs/KNOWN_ISSUES.md` as you work.
- **Small diffs**: avoid rewriting whole files.
- **No silent deletions**: if you remove code, call it out and explain why.

## Standard workflow
1) Create a branch:
- `feat/<ticket-slug>` or `fix/<ticket-slug>` or `chore/<ticket-slug>`

2) Read project state:
- `docs/STATUS.md`
- `docs/HANDOFF.md`
- `docs/NEXT.md`
- `docs/RUNBOOK.md`
- `docs/KNOWN_ISSUES.md`

3) Implement one ticket at a time.

4) Verify (see `docs/RUNBOOK.md`).

5) Update handoff docs.

## Commit messages (recommended)
- `feat: ...`
- `fix: ...`
- `docs: ...`
- `chore: ...`
- `refactor: ...` (rare; only with explicit ticket)

## Pull Requests
- Use the PR template.
- Include: motivation, summary, how to test, risks/rollout notes.
