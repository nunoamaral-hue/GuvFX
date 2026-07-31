# Deploy — provisioner-only secret scope (Phase B)

Controlled production deployment of the **provisioner-only secret scope** so the beta HMAC signing keyring
reaches **only** `guvfx-beta-provisioner`, never the public `api.guvfx.com` backend. Engineering is merged;
this runbook is the exact sequence. **The one Sponsor-only step is clearly marked (§4).** Nothing here arms
the worker, provisions a runtime, or touches Customer Zero.

Replace `${PROD}` with the prod compose directory on the VPS (the dir holding the base `docker-compose.yml`;
confirm in §1) and `${OVERLAY}` with `deploy/beta-provisioner/docker-compose.beta-provisioner.yml`.

## 1. Pre-deploy (read-only)

```bash
# confirm the prod compose dir + that the base compose defines guvfx-backend and guvfx-beta-provisioner exists
cd ${PROD} && docker compose config --services | sort
# confirm the beta provisioner is NOT currently running (this scope change should precede first bring-up)
docker compose ps guvfx-beta-provisioner
# confirm the CURRENT state of the two least-privilege invariants (both expected CLEAN pre-deploy):
docker exec guvfx-backend sh -c '[ -z "$BETA_AGENT_KEYRING" ] && echo backend_keyring_absent || echo backend_HAS_keyring'
```

## 2. Backup / rollback readiness

```bash
# snapshot the overlay dir + the base compose so the scope change is reversible
cp -a ${PROD}/docker-compose.yml ${PROD}/docker-compose.yml.bak.$(date -u +%Y%m%dT%H%M%SZ)
# record the git SHA carrying the overlay + template (rollback = check out the prior overlay)
git -C ${PROD_REPO:-$PROD} log --oneline -1 -- deploy/beta-provisioner/
```
**Rollback:** `git checkout <prior-sha> -- deploy/beta-provisioner/docker-compose.beta-provisioner.yml`
and, if the provisioner was brought up, `docker compose ... rm -f guvfx-beta-provisioner`. No backend/worker/
listener service is touched by this change, so backend rollback is not required.

## 3. Where the secret file goes (no secret yet; do NOT use `docker compose config`)

The overlay's `env_file` is `${BETA_PROVISIONER_SECRET_ENV:-deploy/beta-provisioner/beta-provisioner.secret.env}`.
A relative env_file resolves against the **compose project directory** = the directory of the base
`docker-compose.yml` = your CWD when you run the deploy (§5). So:

```
${SECRET_PATH} = ${PROD}/deploy/beta-provisioner/beta-provisioner.secret.env
```

If your base compose and this overlay live in different trees, instead set `BETA_PROVISIONER_SECRET_ENV` to
an ABSOLUTE path in the deploy shell and use that as `${SECRET_PATH}`.

> **Do NOT run `docker compose config` on `guvfx-beta-provisioner` to find the path.** It emits no `env_file`
> key (compose folds env_file into an `environment:` block), and once the secret file exists it prints
> `BETA_AGENT_KEYRING` in **CLEARTEXT** to stdout/scrollback/CI. If the file is misplaced, §5 `up` fails
> loudly and names the exact expected path — that is the authoritative resolver, not `config`.

## 4. >>> SPONSOR-ONLY STEP — insert the live secret <<<

**This is the single step Claude does not perform.** On the VPS, as the operator:

```bash
cp ${PROD}/deploy/beta-provisioner/beta-provisioner.secret.env.example ${SECRET_PATH}
chmod 600 ${SECRET_PATH}
# edit ${SECRET_PATH} and fill EXACTLY two secret values from the beta agent (100.79.101.19 Machine env):
#   BETA_AGENT_KEY_ID    = the agent's BETA_AGENT_KEY_ID   (the len-13 active key id)
#   BETA_AGENT_KEYRING   = the agent's BETA_AGENT_KEYRING   (the {"<key_id>":"<secret>"} JSON, len ~64)
# BETA_AGENT_BASE_URL is pre-filled (non-secret). Save. Do NOT paste the values into chat/logs/CI.
```

- **Where the secret belongs:** the file `${SECRET_PATH}` (git-ignored), key `BETA_AGENT_KEYRING` +
  `BETA_AGENT_KEY_ID`. Nowhere else — never the shared backend env_file.
- **What values are required:** exactly the agent's existing `BETA_AGENT_KEY_ID` and `BETA_AGENT_KEYRING`
  (the two sides share one symmetric key). Nothing is generated or rotated here.
- **How to enter them:** edit the 0600 file directly on the VPS. Not via chat, not via CI, not via `docker
  ... -e`, not into the shared backend env_file.

## 5. Bring up the provisioner (Claude — after §4)

```bash
cd ${PROD}
docker compose -f docker-compose.yml -f ${OVERLAY} up -d --no-deps guvfx-beta-provisioner
```
`BETA_RUNTIMES_ENABLED` stays OFF (dark). This starts an idle loop that claims nothing.

## 6. Verify the secret scope WITHOUT revealing the value (Claude)

```bash
# (a) the PUBLIC backend must NOT have the signing secret:
docker exec guvfx-backend sh -c '[ -z "$BETA_AGENT_KEYRING" ] && [ -z "$BETA_AGENT_KEY_ID" ] \
  && echo BACKEND_CLEAN || echo BACKEND_LEAK'                       # expect BACKEND_CLEAN

# (b) the PROVISIONER must HAVE it (presence only, value never printed):
docker exec guvfx-beta-provisioner sh -c '[ -n "$BETA_AGENT_KEYRING" ] && [ -n "$BETA_AGENT_KEY_ID" ] \
  && [ -n "$BETA_AGENT_BASE_URL" ] && echo PROVISIONER_SCOPED || echo PROVISIONER_MISSING'  # expect PROVISIONER_SCOPED

# (c) the provisioner still inherits every backend value (DB creds etc.):
docker exec guvfx-beta-provisioner sh -c '[ -n "$DB_HOST" ] && [ -n "$DB_NAME" ] \
  && echo BACKEND_ENV_INHERITED || echo BACKEND_ENV_LOST'          # expect BACKEND_ENV_INHERITED
# (b)+(c) together prove `extends` APPENDED (not replaced) env_file: the provisioner holds both the
# provisioner-only secret AND the inherited backend env. If the base compose supplies DB creds via an
# `environment:` block (not the env_file), a replace-not-append regression would still pass (c) but drop
# env_file-only backend values -- so §7 must also confirm the worker starts and logs no missing-setting error.

# (d) key-id byte lengths match agent expectations WITHOUT printing them (sanity, not the value):
docker exec guvfx-beta-provisioner sh -c 'printf %s "$BETA_AGENT_KEY_ID" | wc -c'   # expect 13
docker exec guvfx-beta-provisioner sh -c 'printf %s "$BETA_AGENT_KEYRING" | wc -c'  # expect ~64+ (non-zero)

# (e) the worker is DARK (claims nothing while the flag is OFF):
bash ${PROD}/deploy/beta-provisioner/verify_beta_provisioner.sh
```
Only `(a)` proves least-privilege; `(b)/(c)` prove the provisioner is fully configured; `(d)` is a
length-only sanity check that never reveals a byte of the secret. **No command prints a secret value.**

## 7. Health

```bash
docker compose ps                       # backend/worker/listener unchanged + healthy; provisioner Up
docker logs --tail=50 guvfx-beta-provisioner   # idle claim loop, no unknown_key_id storm, no crash
docker logs --tail=20 guvfx-backend            # unchanged; still serving api.guvfx.com
```

## What this does NOT do

Does not arm `BETA_RUNTIMES_ENABLED`, does not re-drive ProvisioningJob #1, does not materialise/launch a
runtime, does not touch the backend/worker/listener/bridge, does not place trades. Those are later,
separately-authorised milestones.
