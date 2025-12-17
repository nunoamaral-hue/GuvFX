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

## VPS Production (GuvFX)

- Traefik sits on the `traefik-public` Docker network as the shared TLS entrypoint for `guvfx.com`, `api.guvfx.com`, and `guac.guvfx.com/guacamole/`; Let’s Encrypt certificates are managed via the docker provider, keeping the routing centralized.
- Stack locations on the VPS host:
  - `/home/ubuntu/guvfx-prod` — Traefik + GuvFX backend + GuvFX frontend + GuvFX Postgres.
  - `/home/ubuntu/guacamole-stack` — Guacamole UI, Guacamole database, `guacd`, and `mt5free-desktop`.

- SSH entrypoint:
  - `ssh ubuntu@<ip>` (replace `<ip>` with the production VPS IP before running any commands).

- Safe restart commands (run on the host, not inside containers):
  - `cd /home/ubuntu/guvfx-prod && docker compose up -d --build`
  - `cd /home/ubuntu/guacamole-stack && docker compose up -d --force-recreate guacamole guacd mt5-free-vnc`

- Verification checklist:
  - `docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Ports}}"`
  - `docker compose -f /home/ubuntu/guvfx-prod/docker-compose.yml ps`
  - `docker compose -f /home/ubuntu/guacamole-stack/docker-compose.yml ps`
  - `docker logs --tail 80 traefik | egrep -i "acme|certificate|error" || true`
  - `curl -Ik https://guvfx.com --max-time 10 || true`
  - `curl -Ik https://api.guvfx.com --max-time 10 || true`
  - `curl -Ik https://guac.guvfx.com/guacamole/ --max-time 10 || true`

- Shared handoff directory: `/srv/guvfx/mt5_handoff` (host owner `10001`, group `1000`, mode `2770`/setgid).  
  - Backend container sees it at `/app/.guvfx_handoff` (appuser uid `10001` now in group `1000`).  
  - MT5 container sees it at `/home/mt5free/.guvfx`.  
  - Example sanity checks:
    - `docker exec -it guvfx-backend sh -lc 'ls -la /app/.guvfx_handoff | tail'`
    - `docker exec -it mt5free-desktop bash -lc 'ls -la $HOME/.guvfx | tail'`
  - Permissions sanity: `stat /srv/guvfx/mt5_handoff` and confirm owner `10001`, group `1000`, mode `2770`.
  - Host prep commands: `sudo mkdir -p /srv/guvfx/mt5_handoff && sudo chown 10001:1000 /srv/guvfx/mt5_handoff && sudo chmod 2770 /srv/guvfx/mt5_handoff`

- MT5 automation:
  - JSON config files (e.g., `$HOME/.guvfx/account_1.json`) are shared between backend and MT5 with 660 permissions so both services can read/write.
  - Openbox autostart runs the wallpaper, launches MT5, maximizes the window, and executes the `$HOME/bin/apply-account-config` script inside the MT5 container; the script fills login, server, and (optionally) password fields but does *not* submit the dialog unless `SUBMIT=1` is explicitly enabled.

- Do not reuse the legacy `metatrader5-linux-django-docker` stack; it is discontinued.
