# Known Issues / Sharp Edges

List active problems with reproduction steps and workarounds.

## Example
- **Tests fail: permission denied to create database**
  - Symptom: `permission denied to create database`
  - Likely cause: DB user lacks CREATE DATABASE privilege
  - Workaround: grant permission or configure tests to use an existing DB
  - Next step: document exact local DB roles and update RUNBOOK

- **`make check` fails: connection to 127.0.0.1:5432 (Operation not permitted)**
  - Symptom: backend-test hangs while creating the database and raises `psycopg2.OperationalError: connection to server at "127.0.0.1", port 5432 failed: Operation not permitted`.
  - Likely cause: Postgres server is not running or the sandbox blocks connecting to TCP port 5432.
  - Workaround: start Postgres locally or adjust the database settings before rerunning `make check`.
  - Additional note: rerunning `make check` after this UI work still hits the same port 5432 permission error because no Postgres instance is reachable.

- **`npm run build` fails: Google Fonts download blocked**
  - Symptom: `next build` returns Turbopack errors because it cannot download the Geist/Geist Mono CSS streams from `fonts.googleapis.com`.
  - Likely cause: the sandbox restricts outbound HTTPS to Google Fonts, so the CDN fetch is blocked.
  - Workaround: rerun `npm run build` from an environment with internet access to fonts.googleapis.com (or vendor the fonts locally outside this sandbox) when verification is required.

- **`pyenv: python: command not found` when running `make check`**
  - Symptom: backend-test previously failed because `python` shim wasn’t configured.
  - Fix: Makefile now invokes `backend/.venv/bin/python` when available so manual activation isn’t needed.
  - Remaining requirement: ensure `backend/.venv` exists (e.g., `python -m venv backend/.venv` + install deps).

- **Resolved: pyenv `python` not found**
  - Status: Makefile runs backend tests via `backend/.venv/bin/python`, so `make check` works without activation.
  - Requirement: keep `backend/.venv` in place (see `docs/RUNBOOK.md` for setup steps).

- **`make check` backend tests**
  - Status: runs `backend/.venv/bin/python manage.py test`, so no manual activation is needed.
  - Remaining requirement: ensure `backend/.venv` exists (see `docs/RUNBOOK.md` for how to prepare it).

- **Resolved: login reason parsing only hits `window` on the client**
  - Status: the login client now uses a lazy `useState` initializer, so `window.location.search` is only read when `window` exists.
  - Next step: keep the guard if we extend this component so SSR builds stay stable.
