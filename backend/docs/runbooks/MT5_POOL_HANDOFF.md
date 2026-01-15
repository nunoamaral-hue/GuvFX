# MT5 Pool Handoff Runbook

## Goal
Provide deterministic, least-privilege MT5 desktop access via Guacamole, with ephemeral credential handoff into a leased MT5 instance.

## Key Properties (Security)
- `launch_account.json` and `launch_request.json` are **ephemeral** and must not persist at rest.
- Files are created with **0600** permissions (owner read/write only).
- Files are deleted by the pool watcher after processing.
- Backend returns Guacamole deep-link under `/mt5` and does not expose Guacamole “home UI” to users.

## Paths
- Backend writes handoff files to:
  - `/srv/guvfx/mt5_pool/<instance>/launch_account.json`
  - `/srv/guvfx/mt5_pool/<instance>/launch_request.json`
- Pool watcher runs inside each instance container and reads from:
  - `/home/mt5free/.guvfx` (bind-mounted from `/srv/guvfx/mt5_pool/<instance>`)

## Normal Flow
1. User validates MT5 creds via `/api/mt5/validate/` (stored encrypted in DB).
2. User requests desktop link via `/api/mt5/desktop-link/`.
3. Backend:
   - leases an instance
   - writes `launch_account.json` + `launch_request.json` in that instance dir (0600)
   - returns Guacamole `/mt5/#/client/c/mt5-rdp?data=...`
4. Pool watcher detects `launch_request.json`, runs `launch-apply`, then deletes both files.

## “Good” Evidence Checklist
- Backend returns `/mt5` URL:
  - `https://guac.guvfx.com/mt5/#/client/c/mt5-rdp?data=...`
- Pool watcher shows:
  - `launch_request.json detected`
  - `running launch-apply as mt5free...`
  - `launch files cleaned`
- No plaintext creds remain:
  - `ls -la /srv/guvfx/mt5_pool/<instance> | egrep 'launch_(account|request)\.json'` returns nothing.

## Manual Verification Commands

### Get JWT + request MT5 link
```bash
cd ~/guvfx-prod

curl -sS -o /tmp/jwt_body.txt \
  -X POST "https://api.guvfx.com/api/auth/token/" \
  -H "Content-Type: application/json" \
  -d '{"email":"a@a.com","password":"cadete1980"}'

export JWT="$(jq -r '.access // empty' /tmp/jwt_body.txt)"

URL="$(curl -sS -X POST "https://api.guvfx.com/api/mt5/desktop-link/" \
  -H "Authorization: Bearer $JWT" | jq -r .url)"

echo "URL=$URL"
