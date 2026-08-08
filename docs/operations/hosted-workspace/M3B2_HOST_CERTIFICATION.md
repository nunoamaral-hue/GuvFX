# M3b-2 — Hosted Workspace Agent · Disposable-Host Certification Runbook

> **Status: PREPARED — NOT RUN.** The repository half of M3b-2 (the read-only observation pipeline
> `hosted_workspace/agent.py` + reference adapter `agent_host.py`) is implemented, tested, adversarially
> reviewed, and CI-green. This runbook is the host-certification half. It **has not been executed** and
> **cannot be executed by the engineering agent**: it requires a broker-connected MT5 (a credentialed
> login, which the agent is prohibited from performing) and execution on the live Windows host. It is
> written for **Nuno** to run (or to drive step-by-step, Nuno performing the credentialed/host steps).

## 0. Preconditions (Sponsor / Nuno)

- **A DISPOSABLE Hosted Workspace only.** Never Customer Zero (`#11`), never a production account, never the
  shared production bridge terminal. Use a throwaway demo login on a dedicated slot.
- The disposable workspace's MT5 is **already running and already broker-connected** — Nuno logs it in
  manually. The agent NEVER logs in and NEVER launches it.
- M1 (`#305`, `scripts/mt5_signal_bridge.py` guarded attach) and M3b-1 (`#309`, producer) must be present on
  the host runner used for certification. **Merge sequencing is a Sponsor decision** (see §5).
- `MT5_GUARDED_ATTACH=1` in the host environment (M1's DARK gate) so the guarded attach is enforced.

## 1. Wiring (reference) — the merged operator command under the isolated cert settings

Run the merged `certify_workspace_observation` command from the **isolated cert environment**
(`C:\GuvFX\cert\repo` + `C:\GuvFX\cert\venv`), never the production/agent runtime:

```powershell
$env:DJANGO_SETTINGS_MODULE = "guvfx_backend.cert_settings"   # minimal isolated settings (sqlite, no prod DB)
$env:DJANGO_SECRET_KEY = "cert-only-disposable"               # disposable; never the production secret
$env:MT5_GUARDED_ATTACH = "1"                                 # M1 never-launch guard (code-enforced)
C:\GuvFX\cert\venv\Scripts\python.exe C:\GuvFX\cert\repo\backend\manage.py certify_workspace_observation `
    --workspace-id disposable-1 `
    --expected-login <DEMO_LOGIN> --expected-server <DEMO_SERVER> `      # non-secret; NO password
    --target-path "C:\GuvFX\cert\workspace\...\terminal64.exe" `
    --disposable-prefix "C:\GuvFX\cert\workspace" `
    --tick-symbol EURUSD --previous-state CONNECTED
```

The cert venv needs only **Django + MetaTrader5** (the command runs under `guvfx_backend.cert_settings`, a
minimal isolated settings that lists only the apps needed to import the certified chain, uses in-memory
sqlite, makes no DB query, and skips Django system checks). It prints a SECRET-FREE JSON result.

## 2. Blast-radius protocol — capture BEFORE / DURING / AFTER for every run

For each observation run, record a snapshot at three points and diff them:

| Evidence | Command / source | Expectation |
|---|---|---|
| terminal64.exe PIDs | `Get-Process terminal64 \| Select Id,Path` | **identical** before/during/after — no new PID, no killed PID, no production PID touched |
| Production bridge terminal PID | its known PID | **unchanged** |
| MT5 `config\accounts.dat` mtime+hash | `Get-FileHash`, `(gi ...).LastWriteTime` | **unchanged** (no credential write/replay) |
| Open positions / orders on the disposable account | broker UI / `positions_get` count logged by the run | **unchanged** by the observation |
| MT5 process count | count of terminal64.exe | exactly the expected running set — **no launch** |

Any BEFORE≠AFTER on a mutation-sensitive row is an immediate **FAIL**.

## 3. Certification assertions

| # | Claim | How to prove | Pass |
|---|---|---|---|
| C1 | Correct process selected | `spec.target_path` is the disposable terminal; snapshot `target_pid`/path match | ☐ |
| C2 | Correct path selected | attach reached only the terminal at `target_path` (no foreign dir) | ☐ |
| C3 | Correct login observed | `obs` account_match True with the disposable login; snapshot observed_login (in a redacted log) == expected | ☐ |
| C4 | Correct server observed | account_match True with the disposable server | ☐ |
| C5 | Correct trade_mode observed | DEMO(0) observed; a REAL account would deny (do NOT test with a real account) | ☐ |
| C6 | No duplicate MT5 process | §2 process count == 1 for the target dir throughout | ☐ |
| C7 | No launch | terminal down ⇒ run yields `process_running=False`, **no** terminal64.exe appears (§2) | ☐ |
| C8 | No credential replay | `accounts.dat` hash unchanged (§2); attach received only `{path}` (assert in a wrapped `guarded_initialize`) | ☐ |
| C9 | No production PID touched | production bridge PID unchanged (§2) | ☐ |
| C10 | Read-only | positions/orders unchanged (§2); no order/SL/TP change; `obs` never triggers an action | ☐ |

## 4. Negative controls (RULE 11 — prove the measurement can produce a known result both ways)

- **Down-terminal control:** stop the disposable terminal, run the agent ⇒ expect `process_running=False`,
  all facts False, **and no launch** (C7). Then start it, run again ⇒ expect the positive observation.
  A path that only ever shows the negative is not proven — both directions must be demonstrated.
- **Wrong-account control:** point `expected_login` at a different value ⇒ expect `account_match=False`
  while all other facts stay truthful (proves the deny is real, not a blanket failure).

## 5. Merge-sequencing dependency (Sponsor / Amber)

M1 (`#305`, off `main`) and the M3b-1 stack (`#309`) are **disjoint unmerged branches**. The reference
adapter binds them by **injection** at this host entry-point, so no repository merge is forced by M3b-2. But
running §1 on the host needs both present. Choosing the integration base (merge order of `#305` and the
`#306→#309` stack) is a Sponsor-owned decision, not one this increment takes.

## 6. Outcome

Record PASS/PARTIAL/FAIL per assertion with the actual captured evidence (redact the full login — log only a
`****NNNN` suffix). Per the evidence rule, mark PASS **only** for assertions whose measurement actually ran
and met the criterion. Return the completed table + the three-point blast-radius diffs as the host-cert
evidence for the M3b-2 STOP gate.
