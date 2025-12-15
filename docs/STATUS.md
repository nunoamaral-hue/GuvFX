# GuyFX / GuvFX — Project Status

> Update this file **whenever** project state changes.

## TL;DR
- Current focus: GuvFX codebase imported into GitHub repo + CI stabilized (backend + frontend green)
- Current branch: `main` (work branches: `feat/import-guvfx-code`)
- Next milestone: Cleanup import noise (.trash_duplicates/ + duplicate files) + resume broker-autocomplete-flow on a fresh branch off main

## Repo layout (confirm paths)
- Backend: Django — `backend/`
- Frontend: Next.js — `frontend/`
- Docs: `docs/`

## Last known green checks
- Backend: 2025-12-15 — GitHub Actions CI ✅ (Django tests) + `make check` local ✅
- Frontend: 2025-12-15 — GitHub Actions CI ✅ (lint + build) + `make check` local ✅

## Active blockers
- Cleanup needed: remove `.trash_duplicates/` and duplicate “(1)” / “ 2” files from the import (follow-up PR)

## Owners
- PM: Nuno Amaral
- Active coder: Nuno (current) → Clive (next)
