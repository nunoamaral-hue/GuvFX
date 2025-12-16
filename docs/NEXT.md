# NEXT — Priorities (keep this list short)

## P0 (do next)
1. [ ] Patch CI backend detection to look for `backend/manage.py` and point the path to `backend`.
2. [ ] Verify CI (lint/build/test) passes after the backend detection change.
3. [ ] Commit the continuity activation work and open the PR so reviewers can assess it.
4. [ ] Bootstrap Clive using `docs/CODEX_PROMPTS.md` as the starting guide.
5. [x] Upgrade the broker server autocomplete flow with keyboard navigation, debounce/cancellation, error messaging, and a dedicated "No matches" UI state so it meets the acceptance checklist. — done 2025-12-15

## P1
1. [ ] Fill in P1 ticket(s)
2. [x] Switch login reason parsing to a lazy `useState` initializer so the client-only `window` lookup happens safely.
3. [x] Silence the remaining frontend ESLint warnings in `accounts`, `backtests`, and `profile` so `make check` stops failing because of lint.
4. [ ] Track keyboard navigation edge cases (wrap, visibility, focus) as follow-up work before the next release.

## Parking lot (later)
- Ideas/notes that are **not** committed work
