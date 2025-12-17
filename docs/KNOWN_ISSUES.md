# Known Issues / Sharp Edges

List active problems with reproduction steps and workarounds.

## Example
- **Tests fail: permission denied to create database**
  - Symptom: `permission denied to create database`
  - Likely cause: DB user lacks CREATE DATABASE privilege
  - Workaround: grant permission or configure tests to use an existing DB
  - Next step: document exact local DB roles and update RUNBOOK

- **`pyenv: python: command not found` when running `make check`**
  - Symptom: backend-test previously failed because `python` shim wasn’t configured.
  - Fix: Makefile now invokes `backend/.venv/bin/python` when available so manual activation isn’t needed.
  - Remaining requirement: ensure `backend/.venv` exists (e.g., `python -m venv backend/.venv` + install deps).

- **Backend tests blocked: PostgreSQL connection to 127.0.0.1:5432 is denied**
  - Symptom: `make check` fails during the Django test setup with `psycopg2.OperationalError: connection to server ... Operation not permitted`.
  - Likely cause: PostgreSQL server is not running locally or sandbox prevents TCP connections on 127.0.0.1:5432.
  - Workaround: Start PostgreSQL (or allow TCP access) so Django can hit the database before rerunning `make check`.
  - Next step: ensure the database is reachable and then rerun `make check`.

- **MT5 mouse input unreliable via Guacamole**
  - Symptom: Mouse clicks inside the MT5 client (served via `https://guac.guvfx.com/guacamole/`) can stop responding even though keyboard navigation continues to work; clicks briefly resume after opening the File menu or restarting the Guacamole/`guacd` stack.
  - Reproduction: Open the Guacamole MT5 desktop, wait until the automation brings up the Login dialog, and try clicking fields/buttons—mouse focus sometimes disappears until a menu hotkey or toggle reactivates it.
  - Workarounds tried: restarting the `guacd`/Guacamole containers, recreating the `mt5free-desktop` service, resetting the VNC resolution and scaling, and relying on keyboard navigation to complete flows.
  - Next steps: stream Guacamole and `guacd` logs while the issue is happening, adjust x11vnc/VNC parameters (cursor mode, forcing hardware/software scaling), inspect window manager focus handling, and consider swapping to an alternate VNC/RDP backend if the current server cannot deliver reliable mouse events.

- **Resolved: pyenv `python` not found**
  - Status: Makefile runs backend tests via `backend/.venv/bin/python`, so `make check` works without activation.
  - Requirement: keep `backend/.venv` in place (see `docs/RUNBOOK.md` for setup steps).

- **`make check` backend tests**
  - Status: runs `backend/.venv/bin/python manage.py test`, so no manual activation is needed.
  - Remaining requirement: ensure `backend/.venv` exists (see `docs/RUNBOOK.md` for how to prepare it).
