# Internal Pilot Remediation — Staged Deployment & Rollback Package

**Status: PREPARED, NOT EXECUTED.** This package deploys the IPR remediation **code** to production
**DARK** — every arming flag stays OFF and no order can be placed. It does **not** arm anything, invite
users, touch Customer Zero, or start WP6B. Arming is a **separate, Sponsor-gated** step (see
[arming-runbook.md](arming-runbook.md) and the flag matrix in [feature-flags.md](feature-flags.md)).

Execution requires: (1) the remediation PR merged to `main` with CI green, (2) a verified DB backup,
(3) explicit Sponsor authorisation to deploy. **Do not run any stage below until all three hold.**

## What this deploys

- Backend: canonical-runtime beta-path repairs (Area B), customer-safe messaging (Area A), the beta
  flag inventory (Area E), and the staff-only `GET /api/version/` provenance oracle (Area G). All
  behaviour changes are gated behind flags that stay OFF, or are additive/DARK.
- Frontend: the flag-gated `/accounts → /broker-accounts` redirect (Area C, inert while
  `NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED` is OFF), the marketplace self-service Enable-Trading wiring
  (Area D, backed by `BETA_SELF_SERVE_ARM_ENABLED` which stays OFF), and the build-info fingerprint
  (Area G).

## Build model (ground truth — `OPERATIONS_DASHBOARD.md` §deploy)

- Prod is **not a git checkout**: the local worktree is `rsync`'d to `/home/ubuntu/guvfx-prod` (non-git),
  then images are **baked** with `docker build -t <img> …` (compose has **no** `build:`; `:latest` only;
  no registry; migrations run manually).
- `guvfx-backend` + `trade-ingest` + `shadow` **share one backend image** → one commit fingerprint
  covers all three. `guvfx-wayond-listener` is a **separate** image (`deploy/wayond-listener/`) and is
  **not** rebuilt by this package unless a listener change is in scope (this remediation has none).
- **Provenance gotcha (Area G):** the target dir has no `.git`. `GIT_COMMIT` must be captured from the
  **source worktree before rsync** and threaded through the build-args below.

---

## Stage 0 — Pre-deploy safety (BLOCKING)

0.1 **Sponsor authorisation** to deploy recorded (this is a deploy gate, not an arming gate).
0.2 **Verified DB backup** taken and a restore rehearsed in a disposable DB (`OPERATIONS_DASHBOARD.md`
    §6 records no automated backup — this is mandatory before any migration).
0.3 **Golden Reference STOP-check BEFORE** — capture the current golden manifest + a byte-identical
    check, exactly as prior deploys (Deploy P2 pattern). Record order count = expected baseline.
0.4 **Record the currently-deployed commit** for rollback: `git -C <source-worktree> rev-parse HEAD`
    of the *current* prod tree, and the current `:latest` image IDs
    (`docker image inspect guvfx-backend:latest --format '{{.Id}}'`). Tag them:
    `docker tag guvfx-backend:latest guvfx-backend:rollback-preIPR` (and frontend likewise).

## Stage 1 — Build images with provenance build-args

Run from the **source worktree** (which has `.git`) BEFORE rsync, capturing the SHA:

```bash
SRC=<source-worktree>
SHA=$(git -C "$SRC" rev-parse HEAD)
TS=$(date -u +%FT%TZ)

docker build -t guvfx-backend:latest \
  --build-arg GIT_COMMIT="$SHA" \
  --build-arg BUILD_TIMESTAMP="$TS" \
  --build-arg RELEASE_ID="ipr-$SHA" \
  "$SRC/backend"

docker build -t guvfx-frontend:latest \
  --build-arg NEXT_PUBLIC_API_BASE_URL="https://api.guvfx.com" \
  --build-arg GIT_COMMIT="$SHA" \
  --build-arg BUILD_TIMESTAMP="$TS" \
  "$SRC/frontend"
```

> The frontend image is baked with the arming flags **unset** → DARK. `NEXT_PUBLIC_GIT_COMMIT` /
> `NEXT_PUBLIC_BUILD_TIMESTAMP` are non-secret and are the only new build inputs.

## Stage 2 — Migrate first (backend)

The Area B/E/G changes add **no new migrations** (serializer method fields, view branches, a version
view, and docs — no model change). Confirm before deploying:

```bash
docker run --rm --env-file <backend.env> guvfx-backend:latest \
  python manage.py makemigrations --check --dry-run   # expect: "No changes detected"
```

If (and only if) a migration is present, run `migrate` against prod **after** the Stage 0 backup and
**before** recreating the backend container, per the standing MIGRATE-FIRST rule.

## Stage 3 — Recreate services (DARK)

Recreate the shared backend container (covers `guvfx-backend` + `trade-ingest` + `shadow`) and the
frontend, using the existing compose/`docker run` mechanism. **Keep every arming flag OFF** — do not add
any `BROKER_CONNECTIVITY_*`, `OPERATIONS_*`, or `BETA_*` var set to a truthy value in this step.

Listener (`guvfx-wayond-listener`) is **not** touched.

## Stage 4 — Parity verification (the Area G payoff)

4.1 **Backend fingerprint** (staff token required):
```bash
curl -s https://api.guvfx.com/api/version/ -H "Authorization: Bearer <staff-jwt>" | jq
```
Assert `git_commit == $SHA` and every entry under `flags` is `false` (DARK). Because the three services
share the image, this one fingerprint certifies all three.

4.2 **Frontend fingerprint** (static, always reachable — no flag gate):
```bash
curl -s https://guvfx.com/build-info.json | jq
```
Assert `gitCommit == $SHA` and both `flags` `false`. The `prebuild` emitter bakes this from the
`GIT_COMMIT` build-arg, so it certifies exactly the image that was built.

4.3 **Behaviour smoke (DARK):** `/broker-accounts` 404s; `/accounts` renders the legacy page (no
redirect); marketplace signal-copy card shows the account selector + Enable Trading but arming returns
`409 arming_disabled`. All expected while OFF.

## Stage 5 — Golden STOP-check AFTER + sign-off

5.1 **Golden Reference STOP-check AFTER** — byte-identical to Stage 0.3; order count unchanged (0 new).
5.2 Record the deploy in the handoff docs with the fingerprint from Stage 4.1. **STOP.** No arming.

---

## Rollback (per stage — additive/DARK changes are cheaply reversible)

| If it fails at… | Rollback |
|-----------------|----------|
| Stage 2 (migrate) | Restore the Stage 0.2 backup; redeploy the `:rollback-preIPR` images. (No migration is expected — if one ran, its reverse must be verified first.) |
| Stage 3 (recreate) | Re-tag and recreate from `guvfx-backend:rollback-preIPR` / `guvfx-frontend:rollback-preIPR`; the previous commit is recorded in Stage 0.4. Backend changes are flag-gated/additive, so a straight image swap fully reverts. |
| Stage 4 (parity mismatch) | Do NOT arm. Rebuild with the correct source worktree (the SHA gotcha in the build model note is the usual cause) and repeat from Stage 1. |
| Post-deploy behaviour regression while DARK | The remediation is DARK by construction; a regression that appears without any flag change is a deploy fault → swap back to `:rollback-preIPR`. Capture `/api/version/` from both images for the incident record. |

**Flag rollback is orthogonal:** nothing here is armed, so there is no flag to disarm. If a later
Sponsor-gated arming step is rolled back, that is governed by [arming-runbook.md](arming-runbook.md) —
backend flags disarm instantly (read live); the two `NEXT_PUBLIC_*` frontend flags require redeploying
the DARK image built with them unset.

## Non-goals (explicit)

- No arming of any flag. No customer invitations. No Customer Zero involvement. No WP6B.
- No listener rebuild. No schema change expected. No production credential handling by the deployer.
