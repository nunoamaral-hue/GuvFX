# Isolated Wayond Listener Deploy — WITHOUT disrupting live trading

Deploys `guvfx-wayond-listener` on the prod VPS as a **fully isolated** service:
- the **trading** backend/worker image (`guvfx-prod-guvfx-backend:latest`) is **never
  rebuilt or restarted**;
- prod's trading source dir (`/home/ubuntu/guvfx-prod/backend/`) is **never overwritten**
  (the listener image is built from a separate dir);
- only the **additive** `signal_intake` migrations (0004–0006) are applied;
- the listener is **read-only**, the provider stays **UN-ARMED** — no intake, no order,
  no E3, no execution change.

Run it **one phase at a time** on the VPS; paste each output for verification before the
next. SSH: `ssh ubuntu@100.119.23.29` (Tailscale IP — public IP is firewalled).

Fill these from Phase 0: `NETWORK` (prod compose network), `BACKEND` (running backend
container name). `PROD=/home/ubuntu/guvfx-prod`, `SRC=/home/ubuntu/guvfx-listener-src`.

## Phase 0 — discover (read-only)
```bash
ssh ubuntu@100.119.23.29
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'   # → note the backend container name
docker network ls | grep guvfx                                   # → NETWORK (e.g. guvfx-prod_default)
ls -l /home/ubuntu/guvfx-prod/.env                               # → env file present, 600
docker exec <BACKEND> python manage.py showmigrations signal_intake   # current DB state
```

## Phase 1 — sync new code to a SEPARATE dir + build the isolated image
On your **Mac** (does NOT touch the trading source dir):
```bash
cd ~/Documents/Programming/Python/trading/guvfx
rsync -av --exclude '.venv' --exclude '__pycache__' --exclude '*.pyc' \
  backend/ ubuntu@100.119.23.29:/home/ubuntu/guvfx-listener-src/backend/
rsync -av deploy/wayond-listener/ \
  ubuntu@100.119.23.29:/home/ubuntu/guvfx-listener-src/deploy/wayond-listener/
```
On the **VPS** (build from the separate dir → a SEPARATE image tag; trading tag untouched):
```bash
cd /home/ubuntu/guvfx-listener-src
docker build -t guvfx-wayond-base:latest backend/                    # new code, NOT the trading tag
docker build -f deploy/wayond-listener/Dockerfile \
  --build-arg BACKEND_IMAGE=guvfx-wayond-base:latest \
  -t guvfx-wayond-listener:latest deploy/wayond-listener/            # + Telethon
docker images | grep -E "guvfx-wayond|guvfx-prod-guvfx-backend"      # confirm trading tag unchanged
```

## Phase 2 — apply ONLY the additive signal_intake migrations
Preview, then apply (using the listener image + the prod DB env/network):
```bash
docker run --rm --network NETWORK --env-file /home/ubuntu/guvfx-prod/.env \
  guvfx-wayond-listener:latest python manage.py showmigrations signal_intake
docker run --rm --network NETWORK --env-file /home/ubuntu/guvfx-prod/.env \
  guvfx-wayond-listener:latest python manage.py migrate signal_intake
```
Expect only `0004/0005/0006` (SignalProvider/ParserProfile/AcquiredMessage/SignalUpdate,
`source_edited`, `MessageAmendment`). **If it tries to apply migrations from OTHER apps
(execution/trading/mt5), STOP and tell me** — we only want additive acquisition schema.

## Phase 3 — secret + UN-ARMED provider
Add to `/home/ubuntu/guvfx-prod/.env` (600, never committed):
```
TELEGRAM_API_ID=<personal app id>
TELEGRAM_API_HASH=<personal app hash>
TELEGRAM_STRING_SESSION=<the ~/.guvfx/prod.session string>
TELEGRAM_DEVICE_MODEL=Desktop
TELEGRAM_SYSTEM_VERSION=Windows 10
TELEGRAM_APP_VERSION=4.16.8
```
Create the provider **UN-ARMED**:
```bash
docker run --rm --network NETWORK --env-file /home/ubuntu/guvfx-prod/.env \
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
  --network NETWORK --env-file /home/ubuntu/guvfx-prod/.env \
  --health-cmd "python manage.py check_wayond_listener --health-file /tmp/wayond_health --max-age 90" \
  --health-interval 30s --health-timeout 15s --health-retries 3 --health-start-period 120s \
  guvfx-wayond-listener:latest \
  python manage.py run_wayond_listener --live --health-file /tmp/wayond_health
```

## Phase 5 — verify (paste back)
```bash
docker ps --filter name=guvfx-wayond-listener
docker logs guvfx-wayond-listener 2>&1 | grep -Ei "connected|catch-up|state=listening" | tail
docker exec guvfx-wayond-listener python manage.py check_wayond_listener --health-file /tmp/wayond_health
docker run --rm --network NETWORK --env-file /home/ubuntu/guvfx-prod/.env \
  guvfx-wayond-listener:latest python manage.py shell -c "
from signal_intake.models import SignalProvider, AcquiredMessage, PendingSignalApproval
from execution.models import ExecutionJob
from django.db.models import Count
print('providers:', list(SignalProvider.objects.values_list('slug','status')))
print('acquired:', dict(AcquiredMessage.objects.values('outcome').annotate(n=Count('id')).values_list('outcome','n')))
print('approvals:', PendingSignalApproval.objects.count())   # expect 0 (un-armed)
print('exec jobs:', ExecutionJob.objects.count())            # expect unchanged
"
```
Expected: `Up (healthy)`; logs `connected` → catch-up → `state=listening`; providers
`[('wayond','ONBOARDING')]`; acquired = mostly `DROPPED_NOT_ARMED`; approvals 0; exec jobs
unchanged.

## Rollback
```bash
docker rm -f guvfx-wayond-listener        # stop + remove the listener (nothing else affected)
```
Or keep it running but drop everything: set the provider `PAUSED` via a shell. The
additive migrations are safe to leave (acquisition-only tables; trading unaffected).

## NOT touched by this procedure
`guvfx-prod-guvfx-backend:latest` (trading backend + worker) — not rebuilt, not restarted.
execution / trading / mt5 code + services. No order_send, no E3, no provider arming.
