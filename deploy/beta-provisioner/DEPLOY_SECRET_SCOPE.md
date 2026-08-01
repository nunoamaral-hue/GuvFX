# Deploy — provisioner-only secret scope, DARK (Phase B)

Controlled production deployment so the beta HMAC signing keyring reaches **only** `guvfx-beta-provisioner`,
never the public `api.guvfx.com` backend, **and the provisioner is DARK** — `BETA_RUNTIMES_ENABLED=0` in the
provisioner-only file overrides the inherited backend `=1` (env_file appended LAST wins), so the provisioner
claims nothing even with the keyring present. This does **not** arm the worker, advance ProvisioningJob #1, or
touch Customer Zero.

Production fact (2026-08-01): `BETA_RUNTIMES_ENABLED=1` is live in `beta.env` and the existing provisioner is
**armed but keyless** (looping `unknown_key_id`). This runbook stops it, deploys the DARK override, and only
then admits the keyring. `${PROD}` = `/home/ubuntu/guvfx-prod`. `${OVERLAY}` =
`deploy/beta-provisioner/docker-compose.beta-provisioner.yml`. `${SECRET_PATH}` =
`${PROD}/deploy/beta-provisioner/beta-provisioner.secret.env` (env_file resolves against the **compose project
directory** = the dir of the base `docker-compose.yml` = your CWD; overridable via `BETA_PROVISIONER_SECRET_ENV`).

## Forbidden (would reveal or arm)
- **No** `docker compose config` on `guvfx-beta-provisioner` after the secret exists (renders `BETA_AGENT_KEYRING`
  in **CLEARTEXT**), no `cat`/`type`/`Get-Content`/`env`/`printenv`/`set -x` of the secret file or the keyring var.
- **No** change to the shared backend `beta.env` / `BETA_RUNTIMES_ENABLED`. **No** setting the provisioner-only
  flag to `1`. Verification is **presence-only** (`[ -n ... ]` / `[ -z ... ]`).

## 1. Verify Customer Zero baseline (read-only)
`docker exec -i guvfx-backend python manage.py shell` → confirm `ProvisioningJob#1` = `QUEUED`, attempt 0, no
lease; `AccountRuntime#1` = `QUEUED`, attempt 0, not quarantined; 1 RuntimeEvent (baseline `→QUEUED`); no
ProvisioningVerificationReport. Record as the `CZ_BASELINE`.

## 2. Fresh verified backup
```bash
ts=$(date -u +%Y%m%dT%H%M%SZ)
docker exec guvfx-postgres pg_dump -U "$DB_USER" "$DB_NAME" | gzip > ${PROD}/backups/pre-dark-${ts}.sql.gz
sha256sum ${PROD}/backups/pre-dark-${ts}.sql.gz   # record path, size, sha256; gunzip -t to prove integrity
cp -a ${PROD}/docker-compose.yml ${PROD}/docker-compose.yml.bak.${ts}
cp -a ${PROD}/deploy/beta-provisioner ${PROD}/deploy/beta-provisioner.bak.${ts}   # preserve the old overlay
docker inspect guvfx-beta-provisioner > ${PROD}/backups/provisioner-inspect-${ts}.json   # image/config record
```
**Rollback:** `docker compose stop guvfx-beta-provisioner`; restore the old overlay dir; `docker compose … up -d
--no-deps --force-recreate guvfx-beta-provisioner` (back to the prior armed-keyless state); DB restore only if
ever needed. No rollback step reveals the secret. Customer Zero is never modified, so it stays `QUEUED`.

## 3. Stop the existing armed/keyless provisioner
```bash
cd ${PROD} && docker compose stop guvfx-beta-provisioner && docker rm -f guvfx-beta-provisioner
```
From here until Step 8 the only provisioner that runs is the brief **DARK-proof in §5b** — and that instance
is dark (`BETA_RUNTIMES_ENABLED=0`) AND keyless, so it cannot claim ProvisioningJob #1 for two independent
reasons. No armed or keyed provisioner runs until Customer Zero is (separately) authorised to progress.

## 4. Apply the merged overlay + template to the VPS
Sync the reviewed `docker-compose.beta-provisioner.yml` + `beta-provisioner.secret.env.example` from `main`
(≥ commit for this PR) into `${PROD}/deploy/beta-provisioner/`. Confirm the overlay now contains the
provisioner-only `env_file` and the DARK documentation.

## 5. Create the real provisioner-only file (DARK, no secret yet — Claude)
```bash
cp ${PROD}/deploy/beta-provisioner/beta-provisioner.secret.env.example ${SECRET_PATH}
chmod 600 ${SECRET_PATH}; chown <deploy-account> ${SECRET_PATH}
```
The file now holds `BETA_AGENT_BASE_URL` + `BETA_RUNTIMES_ENABLED=0` (both non-secret) and **empty**
`BETA_AGENT_KEYRING` / `BETA_AGENT_KEY_ID`.

### 5b. Prove the DARK override effective BEFORE any secret (Claude)
Start the provisioner with the empty-keyring file and prove it is dark, then stop it again:
```bash
docker compose -f docker-compose.yml -f ${OVERLAY} up -d --no-deps --force-recreate guvfx-beta-provisioner
docker exec guvfx-beta-provisioner sh -c 'echo "DARK=[${BETA_RUNTIMES_ENABLED}]"'   # expect DARK=[0]
docker logs --tail=20 guvfx-beta-provisioner   # expect NO claim attempts / NO unknown_key_id (dark = no claim)
docker compose stop guvfx-beta-provisioner && docker rm -f guvfx-beta-provisioner
```
`DARK=[0]` with **no** `unknown_key_id` proves `BETA_RUNTIMES_ENABLED=0` overrides the inherited `=1` and the
worker claims nothing — independent of the keyring.

## 6. >>> SPONSOR-ONLY STEP — insert the live secret <<<
On the VPS, edit `${SECRET_PATH}` and fill exactly two values from the **beta agent's existing** config
(100.79.101.19 Machine env): `BETA_AGENT_KEY_ID` and `BETA_AGENT_KEYRING`. Leave `BETA_RUNTIMES_ENABLED=0` and
`BETA_AGENT_BASE_URL` as-is. Do **not** paste either value into chat/CI/logs; do **not** copy into `beta.env`.
Report only: `Secret file populated: YES / Keyring entered: YES / Key ID entered: YES / Base URL present: YES /
Dark flag = 0: YES / File permissions 0600: YES / No values displayed: YES`.

## 7. Verify required keys by presence only (Claude)
```bash
awk -F= '{print $1}' ${SECRET_PATH} | grep -E '^(BETA_AGENT_KEYRING|BETA_AGENT_KEY_ID|BETA_AGENT_BASE_URL|BETA_RUNTIMES_ENABLED)$' | sort
grep -Eq '^BETA_AGENT_KEYRING=.'  ${SECRET_PATH} && echo KEYRING_PRESENT   # matches non-empty, prints no value
grep -Eq '^BETA_AGENT_KEY_ID=.'   ${SECRET_PATH} && echo KEYID_PRESENT
grep -Eq '^BETA_RUNTIMES_ENABLED=0$' ${SECRET_PATH} && echo DARK_ZERO
stat -c '%a' ${SECRET_PATH}   # expect 600
```

## 8. Start the provisioner (DARK, with keyring)
```bash
cd ${PROD} && docker compose -f docker-compose.yml -f ${OVERLAY} up -d --no-deps --force-recreate guvfx-beta-provisioner
```

## 9–12. Prove the four properties (Claude, presence-only — never a value)
```bash
# 9. PROVISIONER_SCOPED_DARK: provisioner HAS the keyring AND is dark (=0)
docker exec guvfx-beta-provisioner sh -c '[ -n "$BETA_AGENT_KEYRING" ] && [ -n "$BETA_AGENT_KEY_ID" ] \
  && [ "$BETA_RUNTIMES_ENABLED" = "0" ] && echo PROVISIONER_SCOPED_DARK || echo PROVISIONER_SCOPE_FAIL'
# 10. BACKEND_CLEAN: the public backend has neither secret
docker exec guvfx-backend sh -c '[ -z "$BETA_AGENT_KEYRING" ] && [ -z "$BETA_AGENT_KEY_ID" ] \
  && echo BACKEND_CLEAN || echo BACKEND_LEAK'
# 11. BACKEND_ENV_INHERITED: provisioner still has the inherited backend env (append, not replace)
docker exec guvfx-beta-provisioner sh -c '[ -n "$DB_HOST" ] && [ -n "$DB_NAME" ] \
  && echo BACKEND_ENV_INHERITED || echo BACKEND_ENV_LOST'
# 12. logs: dark = claims nothing (no unknown_key_id, no negotiation)
docker logs --tail=20 guvfx-beta-provisioner
```
Then re-run Step 1 read-only and confirm **CUSTOMER_ZERO_UNCHANGED** (Job#1 `QUEUED`/attempt 0, Runtime#1
`QUEUED`/attempt 0/not quarantined, still 1 RuntimeEvent, no ProvisioningVerificationReport) — identical to the
`CZ_BASELINE`.

## 13. STOP
Report evidence. Do **not** set the provisioner-only flag to 1, run NEGOTIATE, re-drive ProvisioningJob #1, or
touch Customer Zero. Those are later, separately-authorised milestones.
