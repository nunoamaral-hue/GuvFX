# Report D — configuration readiness

`WIN-RD8VDS93DK7`, `2026-07-24T05:5x Z`. Populated all deterministic `BETA_AGENT_*` machine environment
variables from the approved source (`deploy/beta-agent/config.example.json`). **No value was fabricated;
no default was invented; no secret was created, read, or set.**

## Required variables

`config.load_config` requires, in `slot_pool` mode: bind host + expected bind host + port, execution model,
slot-pool size, golden dir + digest + manifest version, approved-tasks path, drain timeout, **and** the
signing keyring + key id. Everything except the last two is deterministic.

## Populated (19, machine scope) — set and read back

| Variable | Value |
|---|---|
| `BETA_AGENT_BIND_HOST` | `100.79.101.19` |
| `BETA_AGENT_EXPECTED_BIND_HOST` | `100.79.101.19` |
| `BETA_AGENT_BIND_PORT` | `8791` |
| `BETA_AGENT_EXECUTION_MODEL` | `slot_pool` |
| `BETA_AGENT_SLOT_POOL_SIZE` | `4` |
| `BETA_AGENT_GOLDEN_DIR` | `C:\GuvFX\golden\newMT5` |
| `BETA_AGENT_GOLDEN_DIGEST` | `3a7fa6638e9eb9a0989edcaaff5b0c9ec93b15a6c62b9ee9b5f5f420d6313f10` |
| `BETA_AGENT_GOLDEN_MANIFEST_VERSION` | `5.0.0.6036` |
| `BETA_AGENT_APPROVED_TASKS` | `C:\GuvFX\beta\agent-state\approved_tasks.json` |
| `BETA_AGENT_TOMBSTONE` | `C:\GuvFX\beta\tombstones` |
| `BETA_AGENT_STATE_DIR` | `C:\GuvFX\beta\agent-state` |
| `BETA_AGENT_STATE_DB` | `C:\GuvFX\beta\agent-state\state.sqlite` |
| `BETA_AGENT_SLOT_DB` | `C:\GuvFX\beta\agent-state\slots.sqlite` |
| `BETA_AGENT_LOG_DIR` | `C:\GuvFX\beta\agent-state\logs` |
| `BETA_AGENT_MANIFEST` | `C:\GuvFX\beta\agent\manifest.json` |
| `BETA_AGENT_MAX_BODY_BYTES` | `16384` |
| `BETA_AGENT_MAX_CONNECTIONS` | `16` |
| `BETA_AGENT_REQUEST_TIMEOUT_S` | `10` |
| `BETA_AGENT_DRAIN_TIMEOUT_S` | `45` |

The golden digest and manifest match the approved image (Report B / `phase2a_verified`); the drain timeout
(45) exceeds the 30 s settle window, which `load_config` requires.

## Excluded (2) — operator to provision, reported by presence only

| Variable | State |
|---|---|
| `BETA_AGENT_KEYRING` | **ABSENT** — signing secret; operator-provisioned via the Windows secret store |
| `BETA_AGENT_KEY_ID` | **ABSENT** — names the key in the keyring, so it is coupled to the secret and left with it |

Their **values were never read, generated, or displayed** — only presence was reported.

## Readiness proven (read-only, no secret persisted)

Ran `config.load_config` on the host with the venv interpreter against the real machine environment:

```
STEP1 without secret     -> ConfigError: missing signing keyring / key id (provision via the Windows secret store)
STEP2 throwaway keyring  -> load_config OK; model=slot_pool pool=4 bind=100.79.101.19:8791 golden=3a7fa6638e9e manifest=5.0.0.6036
```

- **STEP 1** proves the keyring is the *only* remaining gap: with everything else set and no keyring,
  `load_config` fails on exactly that message.
- **STEP 2** proves the 19 populated values are complete and internally consistent: with a **throwaway
  in-memory** keyring (`{"throwaway": "00"×32}`, passed only to the in-process call, never written to the
  machine environment — STEP 1 having already confirmed the store holds no keyring), `load_config`
  succeeds and returns the expected model, pool size, bind, digest, and manifest.

## Remaining manual step

**One:** the operator provisions `BETA_AGENT_KEYRING` (and its matching `BETA_AGENT_KEY_ID`) as machine
environment variables via the Windows secret store, at the post-approval first start — exactly as the four
slot passwords were provisioned. Nothing else in the configuration is outstanding.
