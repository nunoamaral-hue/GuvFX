# HANDOFF — GFX-PKT-006C-R4-R2 (UTC constructor invariant & acceptance reconciliation)

> Concise packet pointer. **Notion and GitHub are authoritative** for lifecycle and
> merge state; the repository holds implementation, tests and concise evidence. This
> document does not assert a point-in-time pull-request status.

- Active workstream: **GFX-PKT-006C-R4-R2 — UTC Constructor Invariant and Acceptance
  Reconciliation v0.1** (continuation of GFX-PKT-006C / R1 / R2 / R3 / R4 / R4-R1).
  R4-R2 closes the final constructor-domain and acceptance-factuality defects: direct
  `UtcInstant` construction admits only normalized ASCII `[0-9]+` fractional digits
  (non-ASCII Unicode digits rejected) and epochs within the canonical year 0001–9999
  parser domain; arbitrary-length parsing, exact ordering, immutability and
  unhashability are unchanged. The prior R4-R1 incremental file count is corrected to
  12.
- Base: **GFX-PKT-006C** + **R1** + **R2** + **R3** + **R4** + **R4-R1**, merged to
  `main` at/after `7cb6192…` (PR #35).
- Branch: `fix/utc-instant-constructor-invariant` (base `main` `7cb6192…`).
- Scope: synthetic-only edits to `research/market_data/contracts.py`, the foundation
  test module and docs. No real data, NAS, broker, agent, execution or deployment
  action.
- Evidence: prior manifests through
  `evidence/manifests/GFX-EVD-006C-R4-R1-exact-instant-factuality.json`, plus the new
  `evidence/manifests/GFX-EVD-006C-R4-R2-constructor-acceptance.json` (recorded after
  C13 push/PR CI is green); the R4-R1 manifest gains only an additive supersession
  pointer.

---

# HANDOFF (2025-12-16)

> Outgoing coder updates this at the end of **every** session.

## What we were trying to achieve
- [x] Move all work to **GuvFX** (and prevent accidental pushes to GuvPay).
- [x] Merge continuity workflow PR (handoff system + docs/process).
- [x] Merge broker autocomplete edgecases PR with green CI + green `make check`.
- [x] Keep repo health green on `main` (`make check` passes).

## Current state (source of truth)
- Repo: **GuvFX**
- Default branch: `main`
- Remote safety:
  - `origin` must be `https://github.com/nunoamaral-hue/GuvFX.git`
  - No GuvPay remote should exist in this repo (or it must be push-disabled)
- Last commit on `main`: `818ac4c`
- CI status (latest): ✅ backend + ✅ frontend
- Backend: tests ✅ (2 tests passing via `make check`)
- Frontend: lint ✅, build ✅ (both via `make check`)

## What changed this session
- Repo hygiene / safety:
  - Ensured `origin` points to GuvFX (not GuvPay).
  - Removed accidental nested folder `GuvPay-pr/` and added `GuvPay-pr/` to `.gitignore`.
- Continuity workflow:
  - Continuity PR (v2) merged into `main` (handoff workflow, docs system).
- Broker autocomplete:
  - Edgecases branch fixed/cleaned (removed merge marker fallout) and merged into `main` with checks passing.
- Broker autocomplete verification:
  - Seeded `BrokerServer` entries (IS6FX, IC Markets, TradersWay, XM, Exness) and confirmed `/accounts` autocomplete (demo/live selections plus ↑/↓/Enter/Esc navigation) behaves as expected.

## VPS + MT5 handoff session (2025-12-16)

### What changed
- Production VPS is live: Traefik routes `https://guvfx.com`, `https://api.guvfx.com`, and `https://guac.guvfx.com/guacamole/` with Let’s Encrypt certificates on ports 80/443.
- Stacks run from `/home/ubuntu/guvfx-prod` (Traefik + backend + frontend + Postgres) and `/home/ubuntu/guacamole-stack` (Guacamole UI, `guacd`, `guac-db`, `mt5free-desktop`).
- Shared handoff directory `/srv/guvfx/mt5_handoff` (owner `10001`, group `1000`, mode `2770`) is mounted into `/app/.guvfx_handoff` (backend) and `/home/mt5free/.guvfx` (MT5); JSON configs like `account_1.json` sync between the services.
- MT5 automation runs via Openbox autostart: wallpaper + MT5 start + maximize + `$HOME/bin/apply-account-config` (fills login/server, optional password, no default submit).

### How to verify
- `docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Ports}}"`
- `docker compose -f /home/ubuntu/guvfx-prod/docker-compose.yml ps`
- `docker compose -f /home/ubuntu/guacamole-stack/docker-compose.yml ps`
- `docker logs --tail 80 traefik | egrep -i "acme|letsencrypt|certificate|error" || true`
- `curl -Ik https://guvfx.com --max-time 10 || true`
- `curl -Ik https://api.guvfx.com --max-time 10 || true`
- `curl -Ik https://guac.guvfx.com/guacamole/ --max-time 10 || true`
- `docker exec -it guvfx-backend sh -lc 'ls -la /app/.guvfx_handoff | tail'`
- `docker exec -it mt5free-desktop bash -lc 'ls -la $HOME/.guvfx | tail'`

### Known issues
- MT5 mouse input via Guacamole is still flaky; refer to `docs/KNOWN_ISSUES.md` for symptoms and log-based next steps.

### Next steps
- Fix the MT5 mouse input issue so automation clicks consistently reach the app.
- Harden `apply-account-config` (secure password handling and add optional `SUBMIT=1` flag to press OK when desired) and document the gating behavior.
- Consider baking the `apply-account-config` automation into the `mt5free-desktop` image so the pipeline can be versioned with the container.

## How to verify
- From repo root:
  - `git remote -v` (confirm `origin` is GuvFX)
  - `make check` (backend tests + frontend lint/build all green)

## Known issues / blockers
- Local git corruption risk (historical):
  - Some machines previously had a broken local ref named `master 2` which caused fetch/pull errors.
  - Fix (local only): remove `.git/refs/heads/master 2`, then `git pack-refs --all --prune`, then `git fetch origin --prune`.
- Product verification pending:
  - Broker autocomplete/keyboard navigation needs **verification with real broker server data** (see next steps).

## Exactly what to do next (in order)
1) **Clive start-of-session sanity check**
   - `git checkout main && git pull`
   - `git remote -v` (origin must be GuvFX)
   - `make check`
2) **Verify broker autocomplete on real data**
   - Go to `/accounts`
   - Type 2+ chars in “Broker server name”
   - Confirm: debounce, cancellation, correct suggestions, ↑/↓ highlight, Enter selects, Esc closes, mouse click selects, “No matches” state, error state.
   - If issues found: open a small `fix/...` branch, keep diff minimal, ensure `make check` green, PR into `main`.
3) **P1 cleanup follow-ups**
   - Confirm `.trash_duplicates/` is ignored and no duplicate “(1)” / “ 2” files are reintroduced.
4) **Retire risky old branches**
   - Avoid resurrecting old `feat/broker-autocomplete-flow` if it causes rebase conflicts; create fresh branches off `main`.

## Notes for the next coder (Clive)
- Follow `docs/CLIVE_RUNBOOK.md` and `AGENTS.md` rules.
- No unrelated refactors; no editing build output (`frontend/.next/`).
- Every session ends by updating: `docs/HANDOFF.md`, `docs/STATUS.md`, `docs/NEXT.md`, `docs/KNOWN_ISSUES.md` (if needed).

## VPS / MT5 handoff update (2025-12-18)

### What changed
- Production VPS is live with Traefik on `traefik-public` routing `https://guvfx.com`, `https://api.guvfx.com`, and `https://guac.guvfx.com/guacamole/` over Let’s Encrypt.
- Stacks live in `/home/ubuntu/guvfx-prod` (Traefik + GuvFX backend + GuvFX frontend + guvfx-postgres) and `/home/ubuntu/guacamole-stack` (`guacd`, Guacamole, `guac-db`, `mt5-free-vnc`).
- Shared mount `/srv/guvfx/mt5_handoff` (owner 10001, group 1000, mode 2770) is bind-mounted into `/app/.guvfx_handoff` and `/home/mt5free/.guvfx`; files are 660 so both containers share configs.
- Openbox autostart now draws the wallpaper, launches/maximizes MT5, and runs `$HOME/bin/apply-account-config` (uses `xdotool`/`wmctrl` on `$HOME/.guvfx/account_1.json`) to pre-fill the Login dialog without pressing OK.

### How to verify
- `docker ps`
- `docker logs --tail 200 traefik | egrep -i "acme|certificate|error" || true`
- `curl -Ik https://guvfx.com --max-time 10 || true`
- `curl -Ik https://api.guvfx.com --max-time 10 || true`
- `curl -Ik https://guac.guvfx.com/guacamole/ --max-time 10 || true`
- `stat /srv/guvfx/mt5_handoff`
- `docker exec -it guvfx-backend sh -lc 'ls -la /app/.guvfx_handoff | tail'`
- `docker exec -it mt5free-desktop bash -lc 'ls -la $HOME/.guvfx | tail'`

### Known issues
- MT5 mouse input via Guacamole remains flaky; see `docs/KNOWN_ISSUES.md` for the latest observations and log-based troubleshooting.

### Next steps
- Investigate the Guacamole mouse issue (logs, VNC flags, focus) so automation clicks can be trusted.
- Harden `apply-account-config` (per-account JSON flows, secure passwords, optional `SUBMIT=1` gate) and cook it into the `mt5free-desktop` image if that proves stable.
