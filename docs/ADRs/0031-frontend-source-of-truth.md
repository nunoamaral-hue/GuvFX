# ADR-0031 — Frontend Source of Truth

- **Status:** Accepted
- **Date:** 2026-08-04
- **Programme:** Broker Connectivity Capability – Trusted-Beta Integration (Sprint 3, WP4.1)
- **Scope:** Repository engineering only. No deployment, no container rebuild, no production mutation.

## Context
Before customer-facing frontend work (WP4.2+), the programme requires **exactly one authoritative
frontend source**. Three copies existed with unverified relationships: the Git repository
(`frontend/`), the Docker build context, and the VPS deployed copy
(`/home/ubuntu/guvfx-prod/frontend`, a non-git working directory the production image is built from).
WP4.1 reconciled them with evidence (read-only) and installed a permanent guard.

## Decision
1. **The Git repository (`frontend/`) is the single authoritative frontend source.** The deployment
   flow is one-directional: **Git → Docker image → Production**. There is no production-only frontend
   logic; no hidden VPS edit is authoritative.
2. **Manual edits to the deployed copy are prohibited.** Any change reaches production only by
   committing to Git and rebuilding the image. The VPS working copy is a build artefact, not a source.
3. **The build context is exactly the authoritative source.** A new `frontend/.dockerignore` excludes
   dependencies, build output and all backup/junk, so `COPY . .` cannot absorb a stray manual patch and
   the image is reproducible from a commit.
4. **A permanent parity guard enforces this** (`frontend/scripts/verify-frontend-parity.mjs`), wired as
   the npm `prebuild` hook (runs on every `next build`, so it executes in CI's frontend job) and via
   `make frontend-parity` / `npm run verify:parity`. It fails cleanly on: any junk artefact, a missing
   `.dockerignore` exclusion, route drift (vs `parity/routes.json`), component drift (vs
   `parity/components.json`), and any undocumented env var/flag (vs `parity/env-allowlist.json`).

## Investigation evidence (repo working tree vs VPS deployed, sha256; read-only)
Excluding generated artefacts (`node_modules`, `.next`, `*.tsbuildinfo`, `.DS_Store`):

| Classification | Count | Notes |
|---|---|---|
| **IDENTICAL** | 105 | Every real source/config/asset file byte-identical repo ↔ VPS |
| **DIFFERENT (VPS hand-edit, cosmetic)** | 1 | `src/app/(app)/dashboard/page.tsx` — VPS has 5 `useState` decls **relocated** + comments reworded; **functionally identical** (same hooks/logic). A hidden VPS edit not in git; repo is authoritative → discarded on next rebuild. Not fixable here (no deploy). |
| **GENERATED (excluded)** | — | `node_modules`, `.next`, `*.tsbuildinfo`, `.DS_Store`; `next-env.d.ts` (gitignored, regenerated per build) |
| **ORPHANED / VPS-only** | 12 | Backup/junk on the VPS not in the repo (`page.tsx.bak.preCZFS`, `dashboard/*.bak_px/_rx2`, `layout.tsx.bak_px32`, `OnboardingShell.bak`, `AccountConnectionStep.bak` ×2, `onboarding.ts.bak`, `package*.json.bak_px32`, two `._` AppleDouble files). Cannot remove (production boundary); the guard flags them if run on the VPS. |
| **TRACKED JUNK IN REPO — REMOVED** | 9 | `Dockerfile.bak.2025-12-23_145505` + eight `accounts/page.tsx.bak.*` snapshots were **committed to git**, polluting the authoritative source. Removed in this PR (behaviour-preserving — Next.js never compiles a non-`page-extension` file; verified unreferenced). |

## Env vars, feature flags, routes, components
- **Environment variables: NONE.** The frontend references zero `process.env`/`NEXT_PUBLIC_*` in source.
  The API base (`https://api.guvfx.com`) is **hardcoded** in `src/lib/api.ts`, and the same host is
  hardcoded in the `next.config.ts` CSP. Documented, not changed (WP4.1 changes no behaviour). The
  allow-list `parity/env-allowlist.json` is empty; introducing an env var now fails the guard until
  documented here. *(Future: WP4.2 may introduce `NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED` — it does not
  exist yet.)*
- **Feature flags: NONE** in the frontend today.
- **Routes: 37 route files** — 31 `(app)` pages + 4 `(public)` pages (`login`, `register`, `pricing`,
  `how-it-works`) + layouts. Pinned in `parity/routes.json`.
- **Components: 32** — shared, `admin/`, `backtests/`, `onboarding/` (+ `steps/`), `ui/`. Pinned in
  `parity/components.json`.

## Generated artefacts & reproducibility
`.next/`, `out/`, `node_modules/`, `*.tsbuildinfo`, `next-env.d.ts`, `.DS_Store` are generated and
correctly gitignored + dockerignored. The build is reproducible from a commit: the `.dockerignore`
guarantees only the authoritative source enters the context; `package-lock.json` pins dependencies.
*Nondeterminism note:* none identified in the source tree; Next.js build output can embed build-time
metadata, so bundle-hash equality is not asserted — the guard asserts **source-tree** parity, which is
the reproducibility that matters for source-of-truth.

## Technical debt (documented only — NOT cleaned in WP4.1)
- 5 unused default `create-next-app` template SVGs (`public/{next,vercel,window,globe,file}.svg`, 0
  references). `public/brand/logo.png` is used.
- `frontend/README.md` is still the default Next.js template README.
These are functional-tree hygiene items for a later cleanup packet; WP4.1 removes only source-control
junk (backups/manual patches), not functional or template files.

## Production discipline (standing rules)
- The repository is authoritative; production is downstream. **No manual edits to the deployed frontend.**
- Editor/backup artefacts (`*.bak*`, `._*`, `*.orig`, `*.rej`, `*.tmp`) are gitignored and dockerignored
  and rejected by the parity guard — they can never re-enter the source of truth or the image.
- Any route/component change updates the corresponding `parity/*.json` deliberately (the guard makes
  drift visible in review). Any new env var/flag is added to `parity/env-allowlist.json` and documented
  here first.

## Consequences
- One authoritative frontend source; a reproducible, junk-free build context; a permanent, CI-enforced
  parity guard. No functionality, UI, deployment or flag change. Customer Zero and production untouched.
- Residual (documented, resolves on the next repo-built deploy — out of WP4.1 scope): the cosmetic
  `dashboard/page.tsx` VPS drift and the 12 VPS-only backup files. WP4.1 authorises no deployment.
