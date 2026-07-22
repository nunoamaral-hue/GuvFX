# GuvFX Operations Runbook

**Last updated:** 2026-06-09
**Platform version:** Stability S1–S8 validated

---

## Infrastructure Map

| Component | Host | Address | Access |
|-----------|------|---------|--------|
| VPS (Ubuntu) | guvfx-ubuntu | 100.119.23.29 (Tailscale) / guvfx.com | `ssh ubuntu@guvfx.com` |
| Windows MT5 | guvfx-windows-mt5 | 100.79.101.19 (Tailscale) | `ssh administrator@100.79.101.19` or RDP |
| Mac Control | mac-control-node | 100.118.252.13 | Local |

### VPS Containers

| Container | Purpose | Auto-restart |
|-----------|---------|-------------|
| `guvfx-backend` | Django API server | Yes |
| `guvfx-frontend` | Next.js web app | Yes |
| `guvfx-postgres` | PostgreSQL 15 database | Yes |
| `guvfx-mt5-trade-ingest-worker` | Execution job worker | Yes |
| `guvfx-mt5-validate-worker` | MT5 credential validator | Yes |
| `traefik` | Reverse proxy + SSL | Yes |
| `guacamole` | Browser terminal gateway | Yes |
| `guacd` | Guacamole RDP/VNC daemon | Yes |
| `guac-db` | Guacamole PostgreSQL | Yes |

### Windows Services

| Service | Purpose | Auto-start |
|---------|---------|------------|
| UltraVNC (`uvnc_service`) | VNC server for terminal access | Yes (Windows service) |
| MT5 Terminal | MetaTrader 5 trading platform | **Manual** |
| Signal Bridge (`start_signal_bridge.bat`) | Execution bridge (port 8788) | **Manual** |

---

## 1. VPS Restart

**When:** After VPS reboot, unresponsive containers, or major updates.

```bash
ssh ubuntu@guvfx.com

# Check all containers
docker ps --format "table {{.Names}}\t{{.Status}}"

# If containers are down, start them
cd /home/ubuntu/guvfx-prod
docker compose up -d

# Verify all 9 containers are running
docker ps | wc -l  # Should show 10 (9 containers + header)
```

**After VPS restart, also verify:**
- Schedulers: `crontab -l | grep scheduler`
- Routes: `curl -s -o /dev/null -w "%{http_code}" https://guvfx.com/`
- API: `curl -s -o /dev/null -w "%{http_code}" https://api.guvfx.com/api/auth/cookie/csrf/`

---

## 2. Docker Container Restart

**Individual container restart:**

```bash
cd /home/ubuntu/guvfx-prod

# Restart specific container
docker compose restart guvfx-backend
docker compose restart guvfx-frontend
docker compose restart guvfx-mt5-trade-ingest-worker

# Force recreate (picks up env changes)
docker compose up -d --force-recreate guvfx-backend

# Full rebuild (picks up code changes)
docker build -t guvfx-prod-guvfx-backend /home/ubuntu/guvfx-prod/backend/
docker compose up -d --force-recreate guvfx-backend
```

**Restart all:**

```bash
cd /home/ubuntu/guvfx-prod
docker compose down
docker compose up -d
```

---

## 3. Worker Restart

**Trade ingest worker** (handles execution jobs):

```bash
# Check status
docker logs --tail 5 guvfx-mt5-trade-ingest-worker

# Restart
docker restart guvfx-mt5-trade-ingest-worker

# Verify running
docker logs --tail 3 guvfx-mt5-trade-ingest-worker
# Should show: "worker: mt5-trade-ingest-1 agent_order_base: http://100.79.101.19:8788"
```

**If worker container is missing** (removed during rebuild):

```bash
cd /home/ubuntu/guvfx-prod
docker compose up -d guvfx-mt5-trade-ingest-worker
```

---

## 4. Bridge Restart

The signal bridge runs on the **Windows MT5 machine** and requires an interactive desktop session.

**Option A: Via RDP**

1. Connect to `100.79.101.19` via RDP (or Guacamole VNC)
2. Open `C:\GuvFX` in File Explorer
3. Double-click `start_signal_bridge.bat`
4. Wait for "PREFLIGHT PASSED" and "HTTP server started on port 8788"

**Option B: If bridge is stuck/crashed**

```bash
# Kill from SSH
ssh administrator@100.79.101.19 "taskkill /F /IM python.exe"

# Then restart via RDP (Option A above)
```

**Verify bridge is running:**

> **Credentials.** `$GUVFX_AGENT_TOKEN` is **never** written into this runbook or any tracked file. It lives
> only in the deployment environment: on the VPS in the service env files (e.g. `telegram.env` /
> `wayond-listener.env`, surfaced to Django as `GUVFX_WINDOWS_AGENT_TOKEN`), and on the Windows host as the
> `GUVFX_AGENT_TOKEN` machine environment variable the bridge reads at start-up. Source it before running the
> commands below (`set -a; . /home/ubuntu/guvfx-prod/telegram.env; set +a`). The repository secret scanner
> (`scripts/check_no_secrets.py`, categories `guvfx-agent-token-header` / `guvfx-token-assignment`) fails CI if
> a literal token is ever committed again.

```bash
ssh ubuntu@guvfx.com 'curl -s -H "X-GuvFX-Agent-Token: $GUVFX_AGENT_TOKEN" http://100.79.101.19:8788/health'
# Expected: {"ok": true, "status": "healthy"}
```

**Important:** The bridge **cannot** be started from SSH. It must run in the interactive Windows desktop session because MT5's Python API requires IPC access to the running terminal.

---

## 5. MT5 Terminal Restart

**Via RDP to Windows (100.79.101.19):**

1. Check if terminal is running: look for MetaTrader 5 window
2. If not running, navigate to `C:\GuvFX\terminals\account_001\instance\`
3. Double-click `terminal64.exe`
4. Wait for terminal to connect to broker
5. Check Navigator panel — account should show with balance
6. Enable **Algo Trading** (click the button in toolbar — should show green play icon)

**If terminal shows "Invalid account":**
- Demo account has expired
- Right-click broker in Navigator > "Open an Account"
- Create new demo account
- Update `TradingAccount.account_number` in the GuvFX database
- See section 12 "Demo Account Replacement"

**After MT5 restart, also restart the bridge** (section 4).

---

## 6. Guacamole Restart

```bash
# Restart Guacamole web app
docker restart guacamole

# Restart Guacamole daemon (RDP/VNC proxy)
docker restart guacd

# Verify
curl -s -o /dev/null -w "%{http_code}" http://localhost:8081/guacamole/
# Expected: 200

# Verify auth-json extension loaded
docker logs guacamole 2>&1 | grep "Encrypted JSON Authentication"
# Expected: Extension "Encrypted JSON Authentication" (json) loaded.
```

**If auth-json extension is missing after restart:**

```bash
# Copy JAR to persistent extensions directory
sudo cp /srv/guvfx/guac_home/extensions.disabled/guacamole-auth-json-1.5.5.jar \
  /srv/guvfx/guac_home/extensions/
docker restart guacamole
```

---

## 7. VNC Recovery

VNC runs as a Windows service on the MT5 machine.

**Check from VPS:**

```bash
ssh administrator@100.79.101.19 "sc query uvnc_service | findstr STATE"
# Expected: STATE : 4  RUNNING
```

**Restart VNC:**

```bash
ssh administrator@100.79.101.19 "sc stop uvnc_service && timeout /t 3 >nul && sc start uvnc_service"
```

**Verify accessibility from VPS:**

```bash
ssh ubuntu@guvfx.com 'timeout 3 docker exec guacd nc -zv 100.79.101.19 5900'
# Expected: Connection ... succeeded!
```

---

## 8. Scheduler Recovery

Three cron schedulers run every minute:

```bash
# Check scheduler cron entries
crontab -l | grep scheduler

# Check recent scheduler activity
tail -5 /var/log/guvfx/h1_scheduler.log
tail -5 /var/log/guvfx/m5_scheduler.log
tail -5 /var/log/guvfx/h4_scheduler.log
```

**If schedulers are not running:**

```bash
# Verify crontab has entries (not commented out)
crontab -l | grep -v "^#" | grep scheduler

# If missing, restore from backup
cat << 'EOF' | crontab -
* * * * * cd /home/ubuntu/guvfx-prod && docker compose exec -T guvfx-backend python manage.py run_h4_scheduler --grace-seconds 70 >> /var/log/guvfx/h4_scheduler.log 2>&1
* * * * * cd /home/ubuntu/guvfx-prod && docker compose exec -T guvfx-backend python manage.py run_m5_scheduler --grace-seconds 30 >> /var/log/guvfx/m5_scheduler.log 2>&1
* * * * * cd /home/ubuntu/guvfx-prod && docker compose exec -T guvfx-backend python manage.py run_h1_scheduler --grace-seconds 45 >> /var/log/guvfx/h1_scheduler.log 2>&1
EOF
```

**Manual scheduler run (for testing):**

```bash
docker exec guvfx-backend python manage.py run_h1_scheduler \
  --force-once \
  --force-bar-close-iso "$(date -u +%Y-%m-%dT%H:00:00Z)" \
  --account-id 1 --strategy-id 1
```

---

## 9. Execution Recovery

**Check for stuck jobs:**

```bash
docker exec guvfx-backend python manage.py shell -c "
from execution.models import ExecutionJob
print('PENDING:', ExecutionJob.objects.filter(status='PENDING').count())
print('RUNNING:', ExecutionJob.objects.filter(status='RUNNING').count())
for j in ExecutionJob.objects.filter(status__in=['PENDING','RUNNING']):
    print(f'  JOB {j.id} {j.job_type} {j.status} created={j.created_at}')
"
```

**Force-fail stuck RUNNING jobs** (if worker crashed mid-execution):

```bash
docker exec guvfx-backend python manage.py shell -c "
from execution.models import ExecutionJob
from django.utils import timezone
stuck = ExecutionJob.objects.filter(status='RUNNING')
for j in stuck:
    j.status = 'FAILED'
    j.error_message = 'Manual recovery: worker crashed'
    j.finished_at = timezone.now()
    j.save(update_fields=['status', 'error_message', 'finished_at'])
    print(f'Failed job {j.id}')
"
```

**Check open MT5 positions:**

```bash
curl -s -H "X-GuvFX-Agent-Token: $GUVFX_AGENT_TOKEN" \
  "http://100.79.101.19:8788/mt5/positions"
```

**Close a specific position:**

```bash
curl -s -X POST \
  -H "X-GuvFX-Agent-Token: $GUVFX_AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"ticket": TICKET_NUMBER}' \
  "http://100.79.101.19:8788/mt5/close-position"
```

---

## 10. Emergency Stop

**Level 1 — Stop new jobs from executing:**

```bash
# Stop the worker (jobs stay PENDING, nothing executes)
docker stop guvfx-mt5-trade-ingest-worker
```

**Level 2 — Stop schedulers from creating jobs:**

```bash
# Comment out all schedulers
crontab -l | sed 's/^/# EMERGENCY: /' | crontab -
```

**Level 3 — Kill switch via environment:**

```bash
# Edit docker-compose to add kill switch
# Add to guvfx-backend environment:
#   GUVFX_EXECUTION_DISABLED: "true"
# Then restart backend
cd /home/ubuntu/guvfx-prod
docker compose up -d --force-recreate guvfx-backend
```

**Level 4 — Stop everything:**

```bash
cd /home/ubuntu/guvfx-prod
docker compose down
```

**Recovery from emergency stop:**

1. Remove kill switch env var (if set)
2. `docker compose up -d`
3. Restore crontab (section 8)
4. Verify bridge is running (section 4)
5. Check for stuck jobs (section 9)

---

## 11. Database Backup & Restore

### Create Backup

```bash
# Manual backup
docker exec guvfx-postgres pg_dump -U guvfx guvfx | gzip > /var/backups/guvfx_$(date +%Y%m%d_%H%M%S).sql.gz

# Verify
ls -lh /var/backups/guvfx_*.sql.gz
```

### Automated Daily Backup (recommended)

Add to crontab:

```bash
0 3 * * * docker exec guvfx-postgres pg_dump -U guvfx guvfx | gzip > /var/backups/guvfx_$(date +\%Y\%m\%d).sql.gz && find /var/backups/ -name "guvfx_*.sql.gz" -mtime +7 -delete
```

### Restore from Backup

```bash
# Stop backend first
docker stop guvfx-backend guvfx-mt5-trade-ingest-worker

# Restore
gunzip -c /var/backups/guvfx_YYYYMMDD.sql.gz | docker exec -i guvfx-postgres psql -U guvfx -d guvfx

# Restart
docker start guvfx-backend guvfx-mt5-trade-ingest-worker
```

---

## 12. Demo Account Replacement

TradersWay demo accounts expire after extended inactivity.

**When account expires:**

1. RDP into Windows MT5 machine
2. In MT5 terminal, right-click "TradersWay-Demo" > "Open an Account"
3. Select "Open a demo account"
4. Note new account number and password
5. Log into the new account in MT5

**Update GuvFX database:**

```bash
docker exec guvfx-backend python manage.py shell -c "
from trading.models import TradingAccount
from trading.crypto import encrypt_password

a = TradingAccount.objects.get(id=1)
a.account_number = 'NEW_ACCOUNT_NUMBER'
a.password_enc = encrypt_password('NEW_PASSWORD')
a.save(update_fields=['account_number', 'password_enc'])
print('Updated account:', a.account_number)
"
```

**Then restart the signal bridge** (section 4).

---

## 13. Health Checks

### Quick Health Check (30 seconds)

```bash
ssh ubuntu@guvfx.com '
echo "--- WEB ---"
curl -s -o /dev/null -w "guvfx.com: %{http_code}\n" https://guvfx.com/
curl -s -o /dev/null -w "API CSRF: %{http_code}\n" https://api.guvfx.com/api/auth/cookie/csrf/

echo "--- CONTAINERS ---"
docker ps --format "{{.Names}}: {{.Status}}" | sort

echo "--- BRIDGE ---"
curl -s -m 5 -H "X-GuvFX-Agent-Token: $GUVFX_AGENT_TOKEN" http://100.79.101.19:8788/health 2>&1 || echo "BRIDGE DOWN"

echo "--- JOBS ---"
docker exec guvfx-backend python manage.py shell -c "
from execution.models import ExecutionJob
p = ExecutionJob.objects.filter(status=\"PENDING\").count()
r = ExecutionJob.objects.filter(status=\"RUNNING\").count()
print(f\"PENDING={p} RUNNING={r}\")
if p > 0 or r > 0: print(\"WARNING: jobs in progress\")
"

echo "--- DISK ---"
df -h / | tail -1
'
```

### Full Health Check (2 minutes)

Run the quick check above, plus:

```bash
# Scheduler activity
tail -1 /var/log/guvfx/h1_scheduler.log
tail -1 /var/log/guvfx/m5_scheduler.log

# MT5 positions
curl -s -H "X-GuvFX-Agent-Token: $GUVFX_AGENT_TOKEN" "http://100.79.101.19:8788/mt5/positions"

# Database size
docker exec guvfx-postgres psql -U guvfx -d guvfx -c "SELECT pg_size_pretty(pg_database_size(current_database()));"

# Worker activity
docker logs --tail 5 guvfx-mt5-trade-ingest-worker

# VNC port
ssh administrator@100.79.101.19 "netstat -an | findstr :5900 | findstr LISTEN"

# SSL expiry
echo | openssl s_client -servername guvfx.com -connect guvfx.com:443 2>/dev/null | openssl x509 -noout -enddate
```

---

## Key Secrets Location

| Secret | Location | Notes |
|--------|----------|-------|
| Docker env vars | `/home/ubuntu/guvfx-prod/docker-compose.yml` | Contains all API keys, tokens, passwords |
| Fernet encryption key | `GUVFX_FERNET_KEY` env var | For TradingAccount password encryption |
| Worker secret | `MT5_WORKER_TOKEN` env var | Worker authentication |
| Agent token | `WINDOWS_AGENT_TOKEN` env var | Bridge authentication |
| Guacamole JSON key | `GUAC_JSON_SECRET_KEY_HEX` env var | Terminal access encryption |
| VNC password | Set via UltraVNC `setpasswd.exe` | On Windows machine |

**Never commit docker-compose.yml with secrets to git.**

---

## Tailscale Network

All VPS-to-Windows communication uses Tailscale VPN.

```bash
# Check Tailscale status
tailscale status

# If disconnected
sudo tailscale up
```

**If Tailscale goes down:** All execution, bridge communication, and VNC/RDP access stops. The VPS web frontend continues to work but cannot reach the Windows machine.
