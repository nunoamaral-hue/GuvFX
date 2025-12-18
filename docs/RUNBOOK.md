# Runbook (Local Dev + Verification)

> Keep this current. If paths or commands differ, update this file.

## Prereqs
- Python 3.11+
- Node.js (LTS recommended)
- PostgreSQL
- (Optional) Redis for Celery/background tasks

## Repo structure (edit to match reality)
- Backend path: `backend`
- Frontend path: `frontend`
- Note: `frontend/.next/` is a build artifact (do **not** edit; do **not** treat as source)

---

## Backend (Django)

### Setup
```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> One-time setup: you only need to create the venv and install requirements once. After that, `make check` runs tests using `backend/.venv/bin/python` and does **not** require manually activating the venv.

### Migrations
```bash
python manage.py migrate
```

### Run server
```bash
python manage.py runserver
```

### Tests
```bash
python manage.py test
```

> If you see “permission denied to create database” during tests, ensure your test DB user has CREATE DATABASE rights, or configure Django to use an existing test DB.

---

## Frontend (Next.js)

### Install
```bash
cd frontend
npm install  # or: npm ci (if you have a lockfile and want reproducible installs)
```

### Dev
```bash
npm run dev
```

### Lint + Build (must be green before handoff)
```bash
npm run lint
npm run build
```

---

## One-command checks (recommended)
This repo includes a `Makefile`. After one-time setup (venv + npm deps), run checks from the repo root:

```bash
make check
```

### One-time setup (required before the first `make check`)

```bash
# Backend deps
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cd ..

# Frontend deps
cd frontend
npm install
cd ..
```

If `make check` fails, record the error and workaround in `docs/KNOWN_ISSUES.md` and keep this runbook updated.

## RUNBOOK — MT5 Free Desktop (XRDP + VNC)

### Start / Rebuild
```bash
docker compose build --no-cache mt5-free-vnc
docker compose up -d --force-recreate mt5-free-vnc
```

### Health checks
```bash
# Ports
ss -lntp | egrep "(:3389|:3350|:5901)"

# XRDP processes
ps aux | egrep "xrdp|sesman" | grep -v grep

# MT5 processes
ps aux | egrep "terminal64.exe|wineserver" | grep -v grep
```

### XRDP session verification
```bash
su - mt5free -c 'export DISPLAY=:10; wmctrl -l'
```
Expected windows:
```
MetaTrader 5 - Netting
Login
```

### Logs
```bash
# MT5 launch log
tail -200 /home/mt5free/.xrdp-mt5.log

# XRDP logs
tail -200 /var/log/xrdp.log
tail -200 /var/log/xrdp-sesman.log
```

### Reset XRDP cleanly (inside container)
```bash
pkill xrdp || true
pkill xrdp-sesman || true
xrdp-sesman --nodaemon &
sleep 1
xrdp --nodaemon &
```

### Manual MT5 start (debug only)
```bash
su - mt5free -c '
export DISPLAY=:10
wine "$HOME/.wine/drive_c/Program Files/MetaTrader 5/terminal64.exe"
'
```

## VPS Production (GuvFX)

- Production stacks:
  - `/home/ubuntu/guvfx-prod` — Traefik on `traefik-public`, GuvFX backend, GuvFX frontend, guvfx-postgres.
  - `/home/ubuntu/guacamole-stack` — `guacd`, Guacamole UI, `guac-db`, `mt5-free-vnc` desktop.

- Safe restart (run from host):
  ```bash
  cd /home/ubuntu/guvfx-prod && docker compose up -d --build
  cd /home/ubuntu/guacamole-stack && docker compose up -d --force-recreate guacd guacamole mt5-free-vnc
  ```

- Verification commands:
  ```bash
  docker ps
  docker logs --tail 200 traefik | egrep -i "acme|certificate|error" || true
  curl -Ik https://guvfx.com --max-time 10 || true
  curl -Ik https://api.guvfx.com --max-time 10 || true
  curl -Ik https://guac.guvfx.com/guacamole/ --max-time 10 || true
  ```

- Shared handoff mount:
  ```bash
  stat /srv/guvfx/mt5_handoff  # owner 10001, group 1000, mode 2770
  docker exec -it guvfx-backend sh -lc 'ls -la /app/.guvfx_handoff'
  docker exec -it mt5free-desktop bash -lc 'ls -la $HOME/.guvfx'
  ```
  Files inside are mode 660 so backend (`uid 10001`, member of group 1000) and MT5 container can share JSON configs.

- Traefik routes Guacamole at `https://guac.guvfx.com/guacamole/` via the `traefik-public` network so all TLS is handled centrally.
- MT5 automation: Openbox autostart runs wallpaper, starts/maximizes MT5, and runs `$HOME/bin/apply-account-config` which reads `$HOME/.guvfx/account_1.json`, uses `xdotool`/`wmctrl` to fill the login dialog, and does not press OK unless `SUBMIT=1` is enabled.

- Reminder: do not touch the legacy `metatrader5-linux-django-docker` stack; it is discontinued for GuvFX.
