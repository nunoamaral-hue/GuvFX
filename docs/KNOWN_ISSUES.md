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

- **MT5 mouse unreliable via Guacamole**
  - Symptom: Mouse clicks inside the MT5 UI served at `https://guac.guvfx.com/guacamole/` sometimes stop working while keyboard controls remain usable; the pointer often wakes up after opening the File menu or reconnecting.
  - Reproduction: Launch Guacamole, start the MT5 desktop, wait for the Login dialog, and repeatedly click fields/buttons—mouse input intermittently freezes even though typing and tabbing succeed.
  - Workarounds tried: restarting `guacd`/Guacamole containers, reconnecting the Guacamole tunnel, reopening the Login dialog, and using keyboard navigation (`Tab`/`Enter`) when the mouse is dead.
  - Next steps: stream Guacamole and `guacd` logs during a failure, inspect x11vnc/VNC cursor mode or scaling options, monitor `mt5free-desktop` output, and consider switching VNC server flags or evaluating alternate remote protocols if hits persist.

- **MT5 Free Desktop quirks**
  - **MT5 Login popup appears on first run**
    - Cause: Broker credentials not yet saved.
    - Resolution: Log in once via XRDP and tick “Save password”.
  - **`Login failed for display 0` messages in logs**
    - Cause: XRDP initial handshake occurs before a proper Xorg display is allocated.
    - Impact: None as long as the session eventually starts.
  - **VNC (:99) does not show MT5**
    - Cause: MT5 intentionally bound to the XRDP display (e.g. `:10`); VNC is kept as a fallback desktop.
  - **Multiple Wine processes on rebuild**
    - Cause: Old wineserver instances still running during restart.
    - Resolution: `autostart-rdp.sh` calls `wineserver -k` to terminate stale processes.

- **Resolved: pyenv `python` not found**
  - Status: Makefile runs backend tests via `backend/.venv/bin/python`, so `make check` works without activation.
  - Requirement: keep `backend/.venv` in place (see `docs/RUNBOOK.md` for setup steps).

- **`make check` backend tests**
  - Status: runs `backend/.venv/bin/python manage.py test`, so no manual activation is needed.
  - Remaining requirement: ensure `backend/.venv` exists (see `docs/RUNBOOK.md` for how to prepare it).
