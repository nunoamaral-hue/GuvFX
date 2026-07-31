# Beta ProvisioningJob worker (TB-5)

Managed, restart-safe VPS-side service that runs the existing `run_beta_provisioning_worker` loop —
it claims QUEUED beta `ProvisioningJob`s and advances them through the signed management channel
(materialise → configure → start → verify → RUNNING). Without it a QUEUED beta runtime never
provisions.

**Dark by default.** The worker is a no-op unless `BETA_RUNTIMES_ENABLED` is on (default OFF). It
holds NO MT5/broker credentials of its own and reuses the backend service's image + env via
`extends`.

**Provisioner-only secret scope (Phase B).** The HMAC signing keyring (`BETA_AGENT_KEYRING` +
`BETA_AGENT_KEY_ID`) and the agent URL come from a dedicated `env_file` referenced **only** by this
service — never the public `api.guvfx.com` backend — so the signing secret never enters the
internet-facing backend's environment (RULE 3, least privilege). The real secret file is git-ignored and
created on the VPS by the operator; only the `.example` template is committed. See
`DEPLOY_SECRET_SCOPE.md` for the exact deploy + the single Sponsor-only secret-insertion step.

## Files
- `docker-compose.beta-provisioner.yml` — the service (extends `guvfx-backend`, `traefik.enable=false`,
  + provisioner-only `env_file`).
- `beta-provisioner.secret.env.example` — committed template for the provisioner-only secret scope
  (no real values). Copy to `beta-provisioner.secret.env` (git-ignored) on the VPS and fill.
- `DEPLOY_SECRET_SCOPE.md` — controlled deploy of the secret scope + no-reveal verification.
- `verify_beta_provisioner.sh` — confirms the service is up and DARK (claims nothing) while the flag
  is OFF.
- `Provision-BetaRuntime.ps1` — (pre-existing) the host-side provisioning helper.

## Deploy — CLASS-B, Sponsor-gated
This is a repository artefact. Deploying/enabling it is a **Class-B Sponsor gate** (it must not run
against the beta VPS, and the flag must not be armed, without explicit Sponsor approval and the
B3P-2 host APPLY + beta agent in place).

```bash
# from the prod compose directory
docker compose -f docker-compose.yml \
  -f /home/ubuntu/guvfx-app/deploy/beta-provisioner/docker-compose.beta-provisioner.yml \
  up -d --no-deps guvfx-beta-provisioner
```

1. Bring the service up while `BETA_RUNTIMES_ENABLED` is OFF and run `verify_beta_provisioner.sh` —
   it must be running and claiming nothing (dark).
2. Companion cron (optional): schedule `python manage.py reconcile_beta_provisioning` to re-enqueue
   any beta runtime left `NOT_PROVISIONED` (e.g. an account created while the flag was OFF). Also
   dark unless `BETA_RUNTIMES_ENABLED`.
3. **Arm** (only after the host APPLY + beta agent are reachable): set `BETA_RUNTIMES_ENABLED=1` in
   the backend env_file and recreate this service.

## Rollback / disarm
```bash
docker compose ... stop guvfx-beta-provisioner
docker compose ... rm -f guvfx-beta-provisioner
```
Or set `BETA_RUNTIMES_ENABLED=0`. Stopping the worker tears down no runtime — QUEUED jobs simply wait.

Makes **no** change to the backend, worker, or listener services.
