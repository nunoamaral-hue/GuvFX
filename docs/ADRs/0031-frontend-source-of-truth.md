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
- **Build-layer var (documented, inert):** `frontend/Dockerfile` declares `ARG/ENV
  NEXT_PUBLIC_API_BASE_URL`, but **no source file consumes it** (the base is the hardcoded
  `api.guvfx.com` above). It is a build-layer no-op today; the parity guard scans the *source tree*
  (where the count is genuinely zero) and does not police Dockerfile ARGs. Left in place (WP4.1 changes
  no build behaviour); noted here for completeness.
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
- **Out of scope (noted for a follow-up):** `backend/` and `mt5_worker/` also carry tracked `.bak`
  files (e.g. `backend/*/views.py.bak*`). WP4.1 is frontend-only; the same source-of-truth discipline
  and a backend parity guard should be extended there in a later packet.

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

## WP4.2 — Broker Accounts (Broker Connections) frontend journey (2026-08-04)

The first customer-facing Broker Connectivity UI, on the now-authoritative frontend. UI integration only
against the merged WP1A backend (`trading.views` `bc_*` actions on TradingAccountViewSet); **no backend
change, no new endpoint**. Flag-OFF / DARK; no deployment.

- **Feature flag `NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED` (default OFF, build-time, `lib/flags.ts`).**
  When OFF the UI does not exist: no nav entry (`AppShell` shows the "Broker Connections" item only when
  on), the routes `/broker-accounts` and `/broker-accounts/[id]` call `notFound()` **before any fetch**,
  and no API call is made. Existing behaviour is byte-identical. Arming = a rebuild with the flag on (a
  separate, Sponsor-gated step). The flag is the sole entry in `parity/env-allowlist.json` and is
  policed by the WP4.1 guard.
- **UI/backend contract.** List `/api/trading/accounts/` + per-account `broker/status/`; details +
  `broker/validation-history/`; actions `broker/{test-connection,retry-validation,replace-credentials,
  disconnect}`. One client (`lib/broker-api.ts`) owns every URL; one mapping (`lib/broker-status.ts`)
  turns backend enums/reason codes into customer-safe views — components never hardcode a backend string,
  and unknown reason codes fall back to a generic message (no operator diagnostics). Passwords are
  write-only (submitted, never stored/echoed); the account number is masked to the last 4.
- **Component ownership (`components/broker/`):** `StatusBadge`, `AccountCard`, `ValidationHistoryTable`,
  `Dialog` (accessible base: role=dialog, aria-modal, ESC/backdrop close, focus trap), `BrokerAccountWizard`
  (Add + validate), `ReplaceCredentialsDialog`, `DisconnectDialog`, `States` (Loading/Empty/Error). Types
  in `types/broker.ts`.
- **Tests.** vitest + @testing-library (jsdom) added as devDeps; run via the `prelint` npm hook (so CI's
  frontend lint job and `make check frontend-lint` execute them; the Docker image build runs only
  `next build`, so tests stay out of the image). 27 tests: status/reason mapping, flag semantics,
  component render + a11y (dialog roles/ESC), and the load-bearing flag-gate test (OFF → `notFound` +
  **zero** API calls; ON → renders + fetches).
