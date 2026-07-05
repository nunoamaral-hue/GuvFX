# Isolated Wayond Listener Deploy — WITHOUT disrupting live trading

Deploys `guvfx-wayond-listener` on the prod VPS as a **fully isolated** service:
- the **trading** image (`guvfx-prod-guvfx-backend:latest`, shared by `guvfx-backend`,
  `guvfx-mt5-trade-ingest-worker`, `guvfx-mt5-shadow-worker`) is **never rebuilt or
  restarted**;
- prod's trading source dir (`/home/ubuntu/guvfx-prod/backend/`) is **never overwritten**
  (the listener image is built from a separate dir + a separate tag);
- only the **additive** `signal_intake` migrations are applied;
- the listener is **read-only**, the provider stays **UN-ARMED** — no intake, no order,
  no E3, no execution change.

Run it **one phase at a time**; paste each output before the next. SSH:
`ssh ubuntu@100.119.23.29` (Tailscale IP — public IP is firewalled).

**As-executed values (2026-07-05, personal-account go-live):** `BACKEND=guvfx-backend`,
`NETWORK=guvfx-prod_default`, DB container `guvfx-postgres`, `PROD=/home/ubuntu/guvfx-prod`,
`SRC=/home/ubuntu/guvfx-listener-src`.

## Env-handling gotcha (READ THIS)
The backend's DB creds come from the **container environment** (docker-compose resolves
`/home/ubuntu/guvfx-prod/.env` correctly). Two things that do NOT work:
- `docker run --env-file /home/ubuntu/guvfx-prod/.env` — docker's `--env-file` does **not**
  strip quotes/handle specials like compose/`python-dotenv` do → the DB password arrives
  mangled → `password authentication failed`.
- mounting that `.env` at `/app/.env` — the container runs as `appuser` (uid 10001) and the
  `.env` is `600 ubuntu` → `PermissionError`.

**What works:** capture the already-resolved values from the running backend and feed those
(`docker exec guvfx-backend env | grep …`). Those are literal, unquoted, correct.

## Phase 0 — discover (read-only)
```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'   # backend container name + shared image
docker network ls | grep guvfx                                   # NETWORK
docker exec guvfx-backend python manage.py showmigrations signal_intake   # current DB state
```

## Phase 1 — sync new code to a SEPARATE dir + build the isolated image
On your **Mac** (does NOT touch the trading source dir):
```bash
cd ~/Documents/Programming/Python/trading/guvfx
ssh ubuntu@100.119.23.29 'mkdir -p /home/ubuntu/guvfx-listener-src/backend /home/ubuntu/guvfx-listener-src/deploy/wayond-listener'
rsync -av --exclude '.venv' --exclude '__pycache__' --exclude '*.pyc' \
  --exclude '.env' --exclude '*.sqlite3' \
  backend/ ubuntu@100.119.23.29:/home/ubuntu/guvfx-listener-src/backend/
rsync -av deploy/wayond-listener/ \
  ubuntu@100.119.23.29:/home/ubuntu/guvfx-listener-src/deploy/wayond-listener/
```
On the **VPS** (separate tag; trading tag untouched):
```bash
cd /home/ubuntu/guvfx-listener-src
docker build -t guvfx-wayond-base:latest backend/
docker build -f deploy/wayond-listener/Dockerfile \
  --build-arg BACKEND_IMAGE=guvfx-wayond-base:latest \
  -t guvfx-wayond-listener:latest deploy/wayond-listener/
docker images | grep -E "guvfx-wayond|guvfx-prod-guvfx-backend"   # confirm trading tag ID unchanged
```

## Phase 2 — apply ONLY the additive signal_intake migrations
Capture the working DB creds (redirected to a file — nothing prints); `SECRET_KEY` isn't
used by a migrate, so a throwaway value is fine. Preview with `--plan`, then apply:
```bash
docker exec guvfx-backend env | grep -E '^(DB_NAME|DB_USER|DB_PASSWORD|DB_HOST|DB_PORT)=' > /tmp/mig.env
docker run --rm --network guvfx-prod_default --env-file /tmp/mig.env -e DJANGO_SECRET_KEY=migrate-dummy \
  guvfx-wayond-listener:latest python manage.py migrate signal_intake --plan
# confirm ONLY signal_intake 0003-0006 (nothing from other apps), then apply:
docker run --rm --network guvfx-prod_default --env-file /tmp/mig.env -e DJANGO_SECRET_KEY=migrate-dummy \
  guvfx-wayond-listener:latest python manage.py migrate signal_intake
rm -f /tmp/mig.env
```
**If `--plan` lists any non-signal_intake migration, STOP.**

## Phase 3 — build the listener secret env + UN-ARMED provider
Build a dedicated `wayond-listener.env` (600) from the resolved backend creds + the Telegram
secrets. Nothing prints secrets.
```bash
docker exec guvfx-backend env | grep -E '^(DB_NAME|DB_USER|DB_PASSWORD|DB_HOST|DB_PORT|DJANGO_SECRET_KEY)=' > /home/ubuntu/guvfx-prod/wayond-listener.env
chmod 600 /home/ubuntu/guvfx-prod/wayond-listener.env
printf 'TELEGRAM_DEVICE_MODEL=Desktop\nTELEGRAM_SYSTEM_VERSION=Windows 10\nTELEGRAM_APP_VERSION=4.16.8\n' >> /home/ubuntu/guvfx-prod/wayond-listener.env
```
Add `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` with `nano` (pasting into `read -s` breaks on
bracketed paste), then add the session by scp (Mac → VPS), never by hand:
```bash
# Mac:
scp ~/.guvfx/prod.session ubuntu@100.119.23.29:/tmp/wayond.session
# VPS:
printf 'TELEGRAM_STRING_SESSION=%s\n' "$(cat /tmp/wayond.session)" >> /home/ubuntu/guvfx-prod/wayond-listener.env
rm -f /tmp/wayond.session
# sanity (names + value lengths only): api_hash=32, string_session≈353
awk -F= '{print $1" ("length(substr($0,length($1)+2))" chars)"}' /home/ubuntu/guvfx-prod/wayond-listener.env
```
Create the provider **UN-ARMED**:
```bash
docker run --rm --network guvfx-prod_default --env-file /home/ubuntu/guvfx-prod/wayond-listener.env \
  guvfx-wayond-listener:latest python manage.py shell -c "
from signal_intake.models import ParserProfile, SignalProvider
p,_=ParserProfile.objects.get_or_create(slug='wayond_v1')
SignalProvider.objects.update_or_create(slug='wayond', defaults=dict(name='Wayond', telegram_chat_id='-1003842321905', parser_profile=p, status=SignalProvider.Status.ONBOARDING))
print('provider ready — ONBOARDING (un-armed)')
"
```

## Phase 4 — run the listener (isolated image, restart policy, healthcheck)
```bash
docker run -d --name guvfx-wayond-listener --restart unless-stopped \
  --network guvfx-prod_default --env-file /home/ubuntu/guvfx-prod/wayond-listener.env \
  --health-cmd "python manage.py check_wayond_listener --health-file /tmp/wayond_health --max-age 90" \
  --health-interval 30s --health-timeout 15s --health-retries 3 --health-start-period 120s \
  guvfx-wayond-listener:latest \
  python manage.py run_wayond_listener --live --health-file /tmp/wayond_health
```

## Phase 5 — verify
```bash
docker ps --filter name=guvfx-wayond-listener --format 'table {{.Names}}\t{{.Status}}'
docker logs guvfx-wayond-listener 2>&1 | tail -30    # connected → catch-up → state=listening
docker run --rm --network guvfx-prod_default --env-file /home/ubuntu/guvfx-prod/wayond-listener.env \
  guvfx-wayond-listener:latest python manage.py shell -c "
from signal_intake.models import SignalProvider, AcquiredMessage, PendingSignalApproval
from execution.models import ExecutionJob
from django.db.models import Count
print('providers:', list(SignalProvider.objects.values_list('slug','status')))
print('acquired:', dict(AcquiredMessage.objects.values('outcome').annotate(n=Count('id')).values_list('outcome','n')))
print('approvals:', PendingSignalApproval.objects.count())   # expect 0
print('exec jobs:', ExecutionJob.objects.count())            # pre-existing baseline; listener adds 0
"
```
Expected: `Up (healthy)`; providers `[('wayond','ONBOARDING')]`; acquired mostly/only
`DROPPED_NOT_ARMED`; approvals 0.

## Rollback / caveats
- **Stop + remove (single isolated unit):** `docker rm -f guvfx-wayond-listener` — nothing
  else is affected. Pause without removing: set the provider `PAUSED` via a shell.
- The additive migrations are safe to leave (acquisition-only tables; trading unaffected).
- **Snapshot caveat:** `wayond-listener.env` holds a *snapshot* of the DB creds. If prod DB
  creds are rotated, re-run the Phase 3 capture and restart the listener.

## NOT touched by this procedure
`guvfx-prod-guvfx-backend:latest` (trading backend + workers) — not rebuilt, not restarted.
execution / trading / mt5 code + services. No order_send, no E3, no provider arming.
