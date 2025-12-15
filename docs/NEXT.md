# NEXT — Priorities (keep this list short)

## P0 (do next)
1. [ ] Resolve local docs diffs cleanly: either (a) commit `docs/HANDOFF.md` + `docs/STATUS.md` on a small `docs/...` branch and open a PR to `main`, or (b) restore them if they are outdated.
2. [ ] Confirm repo health: run `make check` on `main` and on the active feature branch.
3. [ ] Broker autocomplete MVP: define acceptance criteria and implement debounced broker search + selection flow.
4. [ ] Add tests/guardrails for broker autocomplete (minimum: type-safe API response handling + basic UI state tests if available).

## P1
1. [ ] Cleanup follow-ups: ensure `.trash_duplicates/` stays ignored and remove any remaining duplicate “(1)” / “ 2” files if they reappear.
2. [x] Switch login reason parsing to a lazy `useState` initializer so the client-only `window` lookup happens safely.
3. [x] Silence the remaining frontend ESLint warnings in `accounts`, `backtests`, and `profile` so `make check` stops failing because of lint.

## Parking lot (later)
- Ideas/notes that are **not** committed work
