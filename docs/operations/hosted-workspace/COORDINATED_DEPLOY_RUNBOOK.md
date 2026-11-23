# Coordinated backend + frontend deploy — operator runbook (GFX-167)

`scripts/guarded_coordinated_deploy.py` runs a **coordinated** production release of **both** `guvfx-backend` and
`guvfx-frontend` under **one continuous** governed `KIND_DEPLOY` lease. Use it instead of `guarded_deploy.py` (which is
backend-only) whenever a single release spans both services. See `ADR-0123` for the rationale, and
`RECOVERY_OPERATING_MODEL.md` for the lease model.

**This runbook is repository-only documentation. Running the wrapper against production is a separate, Sponsor-gated
action.** The wrapper itself refuses fail-closed if any precondition is not met.

## What it guarantees

- Acquires **exactly one** `KIND_DEPLOY` lease before any mutation and holds it continuously across both phases. A live
  recovery lease, a live same-kind deploy lease, or a malformed lease **refuses before any mutation** — there is **no**
  emergency override and **no** nested bypass.
- **Rollback-first:** tags both current images before any build/recreate; a failure at **any** step restores **both**
  services to the captured baseline and re-verifies both.
- Recreates **only** `guvfx-backend` and `guvfx-frontend`, each `--no-deps --force-recreate`; **never**
  `--remove-orphans`; preserves every unrelated service and standalone container (Traefik, Guacamole,
  `guvfx-wayond-listener`, …).
- Frontend build **requires** `NEXT_PUBLIC_API_BASE_URL` and passes **no** `NEXT_PUBLIC_*_ENABLED` capability flag
  (DARK). Backend/ frontend identities are verified against the exact target SHA; `/api/health/` and the public
  frontend must return 200.
- Releases the lease only after clean success **or** a fully-verified rollback; otherwise **retains** it fail-closed and
  prints exact recovery steps.

## 1. Always dry-run first (mutation-free)

```bash
python3 scripts/guarded_coordinated_deploy.py \
  --sha f047cf5e6f46dcc2872f04454a13f586b18b08d6 \
  --compose-dir /home/ubuntu/guvfx-prod \
  --app-dir /home/ubuntu/guvfx-app \
  --owner <operator> \
  --lease-file /srv/guvfx/mt5_handoff/recovery_operational_lease.json \
  --frontend-api-base https://api.guvfx.com \
  --dry-run
```

Confirm the printed plan: target SHA, lease path, both service names, both build commands (backend has
`GIT_COMMIT`/revision label; frontend has `NEXT_PUBLIC_API_BASE_URL` and **no** `*_ENABLED`), both
`--no-deps --force-recreate` recreates, the verify oracles, and that `never` lists `--remove-orphans`.

## 2. Execute (drop `--dry-run`) — under a Sponsor deploy GO only

Same command without `--dry-run`. The wrapper acquires the single lease, then: tag both → build backend → build
frontend → recreate backend → recreate frontend → verify both → release (or combined rollback). It prints a JSON
result with `outcome`, `lease_held`, `lease_released`, `rolled_back`, `reason`.

- `outcome: DEPLOYED`, `lease_released: true` → success; both services at the target SHA.
- `outcome: ROLLED_BACK`, `lease_released: true` → a step failed; both services restored to the pre-deploy baseline and
  re-verified healthy; the lease is released. Investigate the failure from the printed `detail` before retrying.
- `outcome: REFUSED` → refused **before any mutation** (a live/malformed lease at the gate, or a preflight fail-closed:
  dirty/untrusted source, SHA mismatch, missing compose/service/image, or missing frontend API-base). Production
  untouched.
- `outcome: INDETERMINATE` → the deploy failed **and** rollback could not restore a healthy baseline. **The lease is
  intentionally RETAINED** and a `!!!` alert with recovery steps is printed. Follow them exactly:

  1. Do **not** start another deploy — the `KIND_DEPLOY` lease is retained on purpose.
  2. Compare `guvfx-backend` + `guvfx-frontend` health/identity against the captured `…:rollback-pre-*` image tags
     (`docker images | grep rollback-pre`).
  3. Manually restore both: `docker tag <rollback-pre-tag> <:latest>` then
     `docker compose --project-directory /home/ubuntu/guvfx-prod up -d --no-deps --force-recreate <service>` for each;
     verify `/api/health/` 200 and the public frontend 200.
  4. Only after both are verified healthy, clear the lease with `recovery_ops lease-reclaim` (or let it self-expire via
     its TTL).

## Notes

- The lease is mutually exclusive with recovery/acceptance and with a backend-only `guarded_deploy.py` run — only one
  may hold the lease at a time, which is the point.
- `--ttl` defaults to the 2-hour ceiling (a coordinated two-service deploy is longer than a backend-only one); the
  lease self-expires if the process dies.
- The wrapper reads only the single non-secret `GUVFX_GIT_COMMIT` env var and the non-secret `/build-info.json`; it
  never dumps the container environment and carries no secret in argv, logs, audit, or alerts.
