# Known Issues / Sharp Edges

List active problems with reproduction steps and workarounds.

## Example
- **Tests fail: permission denied to create database**
  - Symptom: `permission denied to create database`
  - Likely cause: DB user lacks CREATE DATABASE privilege
  - Workaround: grant permission or configure tests to use an existing DB
  - Next step: document exact local DB roles and update RUNBOOK

- **`make check` fails: connection to 127.0.0.1:5432 (Operation not permitted)** *(Intermittent — environment dependent; green earlier but failing again as of Tue 16 Dec 2025 06:34:15 UTC)*
  - Symptom: backend-test currently cannot create the database because `psycopg2` cannot connect to `127.0.0.1:5432`.
  - Likely cause: Postgres server is not reachable or TCP port 5432 is blocked in this environment.
  - Workaround: start Postgres locally or adjust the database settings before rerunning `make check`.
  - Note: Was green at Tue 16 Dec 2025 06:26:44 UTC but the success is not stable across environments.
  - Latest run (Tue 16 Dec 2025 06:34:15 UTC) output snippet:
    ```
    Creating test database for alias 'default'...
    /Users/.../base.py:512: RuntimeWarning: Normally Django will use a connection to the 'postgres' database to avoid running initialization queries against the production database when it's not needed
      warnings.warn(
    Found 2 test(s).
    Traceback (most recent call last):
      File "/Users/.../base.py", line 279, in ensure_connection
        self.connect()
      File "/Users/.../asyncio.py", line 26, in inner
        return func(*args, **kwargs)
              ^^^^^^^^^^^^^^^^^^^^^
    ```

- **`npm run build` fails: Google Fonts download blocked** *(Intermittent; previously resolved but still occasionally seen in restricted environments)*
  - Symptom: `next build` sometimes returns Turbopack errors because it cannot download the Geist/Geist Mono CSS streams from `fonts.googleapis.com`.
  - Likely cause: the sandbox restricts outbound HTTPS to Google Fonts, so the CDN fetch is blocked here.
  - Workaround: rerun `npm run build` from an environment with internet access to fonts.googleapis.com (or vendor the fonts locally outside this sandbox) when verification is required.
  - Note: Build succeeded at Tue 16 Dec 2025 06:26:44 UTC, but the failure can reappear when the CDN is blocked.

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
