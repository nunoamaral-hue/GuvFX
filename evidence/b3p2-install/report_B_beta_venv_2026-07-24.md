# Report B — dedicated beta-agent Python runtime

`WIN-RD8VDS93DK7`, `2026-07-24T05:37Z`. Created an isolated venv for the beta service so nothing is
installed into the interpreter the live production bridge runs on.

## Interpreter

| Field | Value |
|---|---|
| Path | `C:\GuvFX\beta\agent-venv\Scripts\python.exe` |
| Version | `Python 3.11.9` |
| `sys.executable` | `C:\GuvFX\beta\agent-venv\Scripts\python.exe` |
| `sys.base_prefix` | `C:\Program Files\Python311` |
| is a venv (`prefix != base_prefix`) | **True** |
| OriginalFilename (PE metadata) | `py.exe` (the stdlib-venv redirector shim — see below) |
| pip | `26.1.2` |

## Dependency inventory

`pip freeze` — the **only** dependency:

```
pywin32==312
```

pywin32 service host present: `C:\GuvFX\beta\agent-venv\Lib\site-packages\win32\pythonservice.exe`
(21 504 bytes) — this is the SCM image the service will run under, not a raw `python.exe`.

## The base interpreter was NOT modified — proven

```
base site-packages\win32 present?   False        (pywin32 is only in the venv)
C:\Program Files\Python311 LastWrite 2026-03-21T10:30:04Z  (unchanged, pre-dates this work)
live bridge on :8788                 pid 13292 still listening
```

`python -m venv` copies/redirects; `pip install` writes only inside the venv. The base interpreter — the
one the bridge runs on — is untouched.

## The venv-shim metadata subtlety (and why the installer check accepts it)

A Windows stdlib venv does **not** copy the base `python.exe` into `Scripts\`. It copies the
**venvlauncher redirector**, whose embedded PE `OriginalFilename` is **`py.exe`**, not `python.exe`
(confirmed on the host and against CPython 3.11.9 source). The interpreter-identity check in
`install_service.ps1` therefore accepts `OriginalFilename ∈ {python, pythonw, py, pyw}.exe` — which admits
the venv shim — while still rejecting the installer's `python-3.11.9-amd64.exe`. Verified against every
real binary on the host, executing none:

```
C:\GuvFX\python311.exe (installer)              REJECTED
C:\Program Files\Python311\python.exe           ACCEPTED
C:\GuvFX\beta\agent-venv\Scripts\python.exe     ACCEPTED   (OriginalFilename py.exe)
C:\GuvFX\beta\agent-venv\Scripts\pythonw.exe    ACCEPTED
a directory / a missing path                    REJECTED
```

## Reproducibility

`deploy/beta-agent/provision_beta_venv.ps1` (this commit) reproduces the runtime deterministically:
verifies the base is an interpreter by metadata **before** executing it, `python -m venv`, `pip install
pywin32`, runs the pywin32 post-install, then asserts the interpreter runs, pywin32 imports, the service
host exists, and the base was not package-modified. Idempotent; read-only without `-Apply`.

## Evidence

`scratchpad/venvinv_out.txt` — full inventory capture.
